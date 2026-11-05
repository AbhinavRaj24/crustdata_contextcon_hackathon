"""Is the buyer who they say they are? Domain check + sender check; finance head found independently."""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

from . import store
from .contracts import Company, Evidence, Request, VerifyResult
from .crustdata import Unavailable, all_of, any_of, client, cond
from .intake import FREE_MAIL, email_domain

BUYING_ROLE = re.compile(
    r"procure|purchas|sourcing|buyer|vendor|supply|finance|account|controller|treasur|operations|\bops\b|admin|facilit|"
    r"founder|\bceo\b|\bcfo\b|\bcoo\b|chief|director|\bhead\b|\bvp\b|vice president|\bit\b|infrastructure|office manager",
    re.IGNORECASE)
FINANCE_HEAD = re.compile(r"\bcfo\b|chief financial|head of (core )?finance|finance head|vp,? finance|vice president,? finance|"
                          r"finance controller|financial controller|director,? finance|finance director|head.{0,12}finance",
                          re.IGNORECASE)
TWO_PART_SUFFIX = ("co.in", "co.uk", "com.au", "co.jp", "com.sg", "net.in", "org.in")


def registrable_label(domain: str) -> str:
    d = domain.lower().removeprefix("www.")
    for suf in TWO_PART_SUFFIX:
        if d.endswith("." + suf):
            return d[: -len(suf) - 1].split(".")[-1]
    parts = d.split(".")
    return parts[-2] if len(parts) >= 2 else parts[0]


def _homoglyph(s: str) -> str:
    return s.replace("rn", "m").replace("vv", "w").replace("0", "o").replace("1", "l").replace("3", "e").replace("5", "s")


def levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def domain_check(sender_domain: Optional[str], primary: Optional[str]) -> tuple[str, str]:
    """Returns (result, why). result: exact | free_mail | lookalike | unrelated | missing."""
    if not sender_domain:
        return "missing", "no sender email domain"
    s = sender_domain.lower().removeprefix("www.")
    if s in FREE_MAIL:
        return "free_mail", f"{s} is a free email provider"
    if not primary:
        return "unrelated", "the company has no known primary domain"
    p = primary.lower().removeprefix("www.")
    if s == p or s.endswith("." + p):
        return "exact", f"{s} matches the company's domain {p}"
    sl, pl = registrable_label(s), registrable_label(p)
    if sl == pl:
        return "lookalike", f"{s} copies {p} with a different ending"
    if len(pl) >= 4 and pl in sl.replace("-", ""):
        return "lookalike", f"{s} wraps the real name '{pl}' in extra words"
    if len(pl) >= 4 and levenshtein(_homoglyph(sl), _homoglyph(pl)) <= 2:
        return "lookalike", f"{s} is 1-2 characters away from {p}"
    return "unrelated", f"{s} is not the company's domain {p} (could be an unknown real domain)"


def _current_entry(profile: dict, company_id: int) -> Optional[dict]:
    for e in (profile.get("experience") or {}).get("employment_details", {}).get("current") or []:
        if e.get("crustdata_company_id") == company_id and not e.get("end_date"):
            return e
    return None


def sender_check(case_id: str, req: Request, company: Company) -> tuple[str, str, list[str]]:
    """Returns (result, why, evidence_ids). result: buying_role | other_role | not_found | not_named | unavailable."""
    if not req.sender_name:
        return "not_named", "the request does not name the sender", []
    try:
        data = client().person_search(all_of(
            cond("experience.employment_details.current.company_id", "=", company.crustdata_id),
            cond("basic_profile.name", "(.)", req.sender_name)), limit=10)
    except Unavailable as e:
        ev = store.add_evidence(Evidence(case_id=case_id, signal="sender_check", value=None, source="person/search",
                                         status="unavailable", note=str(e)))
        return "unavailable", "the people search failed", [ev.id]
    hits = []
    for p in data.get("profiles", []):
        e = _current_entry(p, company.crustdata_id)
        if e:
            hits.append({"name": p.get("basic_profile", {}).get("name"), "title": e.get("title"), "since": (e.get("start_date") or "")[:7],
                         "crustdata_person_id": p.get("crustdata_person_id")})
    buying = [h for h in hits if BUYING_ROLE.search(h["title"] or "")]
    if buying:
        h = buying[0]
        result, why = "buying_role", f"{h['name']} is a current {h['title']} at {company.name}"
    elif hits:
        h = hits[0]
        result, why = "other_role", f"{h['name']} works at {company.name} as {h['title']}, not a buying role"
    else:
        result, why = "not_found", f"no current employee named '{req.sender_name}' at {company.name} in Crustdata"
    ev = store.add_evidence(Evidence(case_id=case_id, signal="sender_check", value={"result": result, "matches": hits[:3]},
                                     source=f"person/search current.company_id={company.crustdata_id} + name", status="verified", note=why))
    return result, why, [ev.id]


def find_finance_head(case_id: str, company: Company) -> Optional[dict]:
    titles = any_of(*[cond("experience.employment_details.current.title", "(.)", t) for t in ("finance", "CFO", "controller")])
    base = cond("experience.employment_details.current.company_id", "=", company.crustdata_id)
    queries = [all_of(base, titles, cond("experience.employment_details.current.seniority_level", "in", ["CXO", "Vice President", "Director"])),
               all_of(base, titles)]
    for q in queries:
        try:
            data = client().person_search(q, limit=10)
        except Unavailable:
            return None
        people = []
        for p in data.get("profiles", []):
            e = _current_entry(p, company.crustdata_id)
            if e and re.search(r"financ|cfo|controller", e.get("title") or "", re.IGNORECASE):
                people.append({"name": p.get("basic_profile", {}).get("name"), "title": e.get("title"),
                               "since": (e.get("start_date") or "")[:7], "crustdata_person_id": p.get("crustdata_person_id"),
                               "is_head": bool(FINANCE_HEAD.search(e.get("title") or ""))})
        if people:
            people.sort(key=lambda x: not x["is_head"])
            head = people[0]
            store.add_evidence(Evidence(case_id=case_id, signal="finance_head", value=head,
                                        source=f"person/search current.company_id={company.crustdata_id} + finance title",
                                        status="verified", note=f"Found independently of the sender: {head['name']}, {head['title']}"))
            return head
    return None


def verify(case_id: str, req: Request, company: Company) -> VerifyResult:
    sender_dom = email_domain(req.sender_email) or (req.company_domain if not req.sender_email else None)
    primary = company.primary_domain or (urlparse(company.website).hostname if company.website else None)
    dresult, dwhy = domain_check(sender_dom, primary)
    dev = store.add_evidence(Evidence(case_id=case_id, signal="domain_check", value={"result": dresult, "sender_domain": sender_dom,
                                      "company_domain": primary}, source=f"company/identify #{company.crustdata_id} primary_domain",
                                      status="verified", note=dwhy))
    sresult, swhy, sev = sender_check(case_id, req, company)

    flags, summary = [], [f"Domain: {dwhy}", f"Sender: {swhy}"]
    if dresult == "lookalike":
        status = "fraud"
        flags.append("lookalike_domain")
    elif dresult == "exact" and sresult in ("buying_role", "not_named"):
        status = "pass"
    elif dresult == "exact" and sresult == "other_role":
        status = "pass"
        flags.append("sender_not_buying_role")
    else:
        status = "unverified"
        flags.append(f"domain_{dresult}" if dresult != "exact" else f"sender_{sresult}")
    vr = VerifyResult(status=status, flags=flags, summary=summary, evidence_ids=[dev.id, *sev])
    if status != "pass":
        vr.finance_head = find_finance_head(case_id, company)
    return vr
