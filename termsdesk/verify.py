"""C · Verify: is the buyer real? Domain check, sender check, finance head, hold-and-verify.

A buyer passes only if the email domain is the company's primary_domain (or a subdomain)
and the sender is a current employee in a buying role, with company, title and "current"
checked on the same job entry. Otherwise the case is held and verified through a finance
head we found ourselves, never by replying to the sender.

Honest limit: email-to-person lookup is disabled on our keys, so we cannot prove that an
address belongs to the named person. The domain check is what ties the email to the company.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from .contracts import Evidence, Request
from .intake import FREE_EMAIL_DOMAINS, canonical_city, clean_domain, email_domain, name_from_email
from .resolve import Resolution, call_api

PERSON_SEARCH = "/person/search"
PERSON_FIELDS = ["basic_profile", "experience"]       # whole sections only; leaf paths return 400
HOLD_UNVERIFIED_MIN_INR = 500_000                     # policy.yaml can override (₹5 lakh)

CURRENT = "experience.employment_details.current"
_COMPANY = f"{CURRENT}.company_id"
_TITLE = f"{CURRENT}.title"
_SENIORITY = f"{CURRENT}.seniority_level"

# --------------------------------------------------------------------------- domain check

_TWO_PART_SUFFIXES = frozenset({
    "co.in", "org.in", "net.in", "firm.in", "gen.in", "ind.in", "ac.in", "edu.in", "gov.in", "res.in",
    "co.uk", "org.uk", "ac.uk", "com.au", "net.au", "org.au", "co.jp", "com.sg", "com.br", "co.za",
    "com.my", "co.nz", "com.cn", "com.hk",
})


def split_domain(domain: str) -> tuple[str, str, str]:
    """'mail.razorpay.co.in' -> ('mail', 'razorpay', 'co.in')."""
    parts = domain.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _TWO_PART_SUFFIXES:
        suffix, rest = ".".join(parts[-2:]), parts[:-2]
    else:
        suffix, rest = parts[-1], parts[:-1]
    label = rest[-1] if rest else ""
    return ".".join(rest[:-1]), label, suffix


def edit_distance(a: str, b: str) -> int:
    """Damerau-Levenshtein (optimal string alignment): swaps of neighbours count once."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def _glyphs(s: str) -> str:
    s = s.replace("rn", "m").replace("vv", "w")
    return s.translate(str.maketrans({"0": "o", "1": "l", "i": "l", "3": "e", "5": "s"}))


@dataclass
class DomainCheck:
    status: str                    # pass | lookalike | unverified
    kind: str                      # exact | subdomain | different_ending | swapped_characters | extra_hyphen |
                                   # extra_word | changed_letters | contains_real_domain | free_email |
                                   # unrelated_domain | listed_secondary_domain | no_email | no_company_domain
    sender_domain: Optional[str]
    primary_domain: Optional[str]
    detail: str

    @property
    def passed(self) -> bool:
        return self.status == "pass"


_KIND_TEXT = {
    "different_ending": "same name with a different ending",
    "swapped_characters": "look-alike characters (rn for m, 0 for o, 1 for l)",
    "extra_hyphen": "an extra hyphen",
    "extra_word": "an extra word around the real name",
    "changed_letters": "one or two letters changed",
    "contains_real_domain": "the real domain hidden inside another domain",
}


def check_domain(sender: Optional[str], primary_domain: Optional[str],
                 all_domains: Optional[list[str]] = None) -> DomainCheck:
    """Compare the sender's domain with the company's primary_domain only."""
    sd = email_domain(sender) if sender and "@" in sender else clean_domain(sender)
    pd = clean_domain(primary_domain)
    if not sd:
        return DomainCheck("unverified", "no_email", None, pd, "No sender email given.")
    if sd in FREE_EMAIL_DOMAINS:
        return DomainCheck("unverified", "free_email", sd, pd, f"{sd} is a free email service.")
    if not pd:
        return DomainCheck("unverified", "no_company_domain", sd, None, "The company has no known primary domain.")
    if sd == pd:
        return DomainCheck("pass", "exact", sd, pd, f"{sd} is the company's primary domain.")
    if sd.endswith("." + pd):
        return DomainCheck("pass", "subdomain", sd, pd, f"{sd} is a subdomain of {pd}.")

    _, sl, sx = split_domain(sd)
    _, pl, px = split_domain(pd)
    kind = None
    if sd.startswith(pd + ".") or ("." + pd + ".") in sd:
        kind = "contains_real_domain"
    elif sl == pl and sx != px:
        kind = "different_ending"
    elif sl != pl:
        if _glyphs(sl) == _glyphs(pl):
            kind = "swapped_characters"
        elif sl.replace("-", "") == pl.replace("-", ""):
            kind = "extra_hyphen"
        elif len(pl) >= 4 and pl in sl.replace("-", ""):
            kind = "extra_word"
        else:
            limit = 1 if len(pl) <= 5 else 2
            if len(sl) >= 3 and 0 < edit_distance(sl, pl) <= limit:
                kind = "changed_letters"
    if kind == "different_ending":
        # Integration (team decision): dunzo.in is Dunzo's real second domain, so the same name with another
        # ending is unverified (held and verified for Rs 5 lakh and above), never fraud.
        return DomainCheck("unverified", kind, sd, pd,
                           f"{sd} has the company's name but not its known domain {pd}; it may be theirs or a copy.")
    if kind:
        return DomainCheck("lookalike", kind, sd, pd, f"{sd} imitates {pd}: {_KIND_TEXT[kind]}.")
    secondary = {clean_domain(d) for d in (all_domains or [])}
    if sd in secondary:
        return DomainCheck("unverified", "listed_secondary_domain", sd, pd,
                           f"{sd} appears in Crustdata's domain list for the company, which also holds junk; not trusted.")
    return DomainCheck("unverified", "unrelated_domain", sd, pd,
                       f"{sd} is not the company's domain ({pd}). Real companies use other domains too, so this is "
                       f"unverified, not fraud.")


# --------------------------------------------------------------------------- person search helpers

BUYING_ROLE = re.compile(
    r"procure|purchas|\bbuyer\b|sourcing|vendor|supply\s*chain|financ|\baccounts?\b|accounting|controller|"
    r"treasury|\bcfo\b|operations|\bops\b|\bcoo\b|\badmin|facilit|office\s+manager|founder|\bceo\b|"
    r"chief\s+executive|managing\s+director|proprietor", re.I)
BUYING_FUNCTIONS = frozenset({"operations", "finance", "purchasing", "procurement", "administrative", "accounting"})

_SENIORITY_ORDER = {"CXO": 0, "Owner / Partner": 1, "Vice President": 2, "Director": 3,
                    "Experienced Manager": 4, "Entry Level Manager": 5, "Strategic": 6, "Senior": 7,
                    "Entry Level": 8, "In Training": 9}

_FINANCE_HEAD_RANKS = [
    (1, re.compile(r"\bcfo\b|chief\s+financ", re.I)),
    (2, re.compile(r"head\s*(?:of|-|–|,)?\s*(?:core\s+)?financ|financ\w*\s+head", re.I)),
    (3, re.compile(r"\b(?:vp|vice\s+president)\b.*financ|financ.*\b(?:vp|vice\s+president)\b", re.I)),
    (4, re.compile(r"financ\w*\s+director|director\s*(?:of|-|,)?\s*financ", re.I)),
    (5, re.compile(r"controller", re.I)),
    (6, re.compile(r"financ|\baccounts?\b|accounting|treasury", re.I)),
]
_LEADER = re.compile(r"founder|\bceo\b|chief\s+executive|managing\s+director", re.I)


def profiles(resp: Any) -> list[dict]:
    if isinstance(resp, list):
        return [p for p in resp if isinstance(p, dict)]
    for key in ("profiles", "results", "data", "people"):
        v = (resp or {}).get(key) if isinstance(resp, dict) else None
        if isinstance(v, list):
            return [p for p in v if isinstance(p, dict)]
    return []


def current_entries(profile: dict) -> list[dict]:
    cur = ((profile.get("experience") or {}).get("employment_details") or {}).get("current")
    if isinstance(cur, dict):
        return [cur]
    return [e for e in (cur or []) if isinstance(e, dict)]


def _entry_company(e: dict) -> Optional[int]:
    for k in ("crustdata_company_id", "company_id"):
        v = e.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
    return None


def _same_current_job(e: dict, company_id: int) -> bool:
    return _entry_company(e) == int(company_id) and not e.get("end_date")


def _month(iso: Optional[str]) -> Optional[str]:
    return iso[:7] if iso and len(iso) >= 7 else None


def names_match(wanted: str, found: str) -> bool:
    w = [t for t in re.findall(r"[a-z]+", wanted.lower())]
    f = [t for t in re.findall(r"[a-z]+", found.lower())]
    if not w or not f or w[0] != f[0]:
        return False
    for t in w[1:]:
        if not any(x == t or (len(t) == 1 and x.startswith(t)) or (len(x) == 1 and t.startswith(x)) for x in f[1:]):
            return False
    return True


def _search(client: Any, filters: dict, limit: int) -> Any:
    body = {"filters": filters, "limit": limit, "fields": PERSON_FIELDS}
    try:
        return call_api(client, PERSON_SEARCH, body)
    except Exception as exc:
        if "400" in str(exc):                         # fields not accepted: retry without them
            body.pop("fields")
            return call_api(client, PERSON_SEARCH, body)
        raise


# --------------------------------------------------------------------------- sender check

@dataclass
class SenderCheck:
    status: str                    # pass | note | unverified
    detail: str
    name: Optional[str] = None
    title: Optional[str] = None
    since: Optional[str] = None
    person_id: Optional[int] = None
    matches: int = 0
    unavailable: bool = False


def check_sender(client: Any, company_id: Optional[int], sender_name: Optional[str],
                 sender_email: Optional[str] = None) -> SenderCheck:
    if not company_id:
        return SenderCheck("unverified", "Company not resolved, so the sender cannot be checked.")
    name = sender_name or name_from_email(sender_email)
    if not name:
        # A role mailbox (procurement@company.com) names no person. If the domain is the company's own,
        # that is what ties the request to the company; verify_buyer accepts it with a note.
        return SenderCheck("unnamed", "The request comes from a role mailbox and names no person.")
    filters = {"op": "and", "conditions": [
        {"field": _COMPANY, "type": "=", "value": int(company_id)},
        {"field": "basic_profile.name", "type": "(.)", "value": name},
    ]}
    try:
        resp = _search(client, filters, limit=5)
    except Exception as exc:
        return SenderCheck("unverified", f"Person Search failed: {exc}", unavailable=True)
    buying, other, n = None, None, 0
    for p in profiles(resp):
        pname = ((p.get("basic_profile") or {}).get("name")) or ""
        if not names_match(name, pname):
            continue
        for e in current_entries(p):
            if not _same_current_job(e, company_id):
                continue
            n += 1
            title = e.get("title") or ""
            row = (pname, title, _month(e.get("start_date")), p.get("crustdata_person_id"))
            if BUYING_ROLE.search(title) or (e.get("function_category") or "").lower() in BUYING_FUNCTIONS:
                buying = buying or row
            else:
                other = other or row
    if buying:
        return SenderCheck("pass", f"{buying[0]} is a current {buying[1]} at the company.", *buying, matches=n)
    if other:
        return SenderCheck("note", f"{other[0]} works at the company as {other[1]}, not a buying role.", *other,
                           matches=n)
    return SenderCheck("unverified", f"No current employee named {name} found at the company "
                                     f"(missing coverage is not proof of fraud).")


# --------------------------------------------------------------------------- finance head

@dataclass
class FinanceHead:
    name: str
    title: str
    since: Optional[str]
    person_id: Optional[int]
    rank: int
    seniority: Optional[str]
    fallback: bool = False         # True when no finance title was found and a founder/CEO is used


def _rank_title(title: str) -> Optional[int]:
    for rank, rx in _FINANCE_HEAD_RANKS:
        if rx.search(title):
            return rank
    return None


def _collect_heads(resp: Any, company_id: int, leaders: bool = False) -> list[FinanceHead]:
    out = []
    for p in profiles(resp):
        pname = ((p.get("basic_profile") or {}).get("name")) or ""
        for e in current_entries(p):
            if not _same_current_job(e, company_id):
                continue
            title = e.get("title") or ""
            rank = 7 if leaders and _LEADER.search(title) else (None if leaders else _rank_title(title))
            if rank is None:
                continue
            out.append(FinanceHead(pname, title, _month(e.get("start_date")), p.get("crustdata_person_id"),
                                   rank, e.get("seniority_level"), fallback=leaders))
    return out


def _title_or(*phrases: str) -> dict:
    return {"op": "or", "conditions": [{"field": _TITLE, "type": "(.)", "value": ph} for ph in phrases]}


def find_finance_head(client: Any, company_id: Optional[int]) -> tuple[Optional[FinanceHead], list[str]]:
    """Cheapest query first; stop as soon as one tier finds someone. Returns (head, errors)."""
    if not company_id:
        return None, ["Company not resolved."]
    cid = {"field": _COMPANY, "type": "=", "value": int(company_id)}
    tiers = [
        ({"op": "and", "conditions": [cid, _title_or("CFO", "chief financial officer")]}, 3, False),
        ({"op": "and", "conditions": [cid, _title_or("head of finance", "finance head", "vp finance",
                                                       "vice president finance", "finance director",
                                                       "director finance", "finance controller",
                                                       "financial controller"),
                                      {"field": _SENIORITY, "type": "in",
                                       "value": ["CXO", "Owner / Partner", "Vice President", "Director"]}]}, 5, False),
        ({"op": "and", "conditions": [cid, {"field": _TITLE, "type": "(.)", "value": "finance"}]}, 5, False),
        ({"op": "and", "conditions": [cid, _title_or("founder", "CEO", "managing director")]}, 3, True),
    ]
    errors = []
    for filters, limit, leaders in tiers:
        try:
            heads = _collect_heads(_search(client, filters, limit), company_id, leaders)
        except Exception as exc:
            errors.append(str(exc))
            continue
        if heads:
            heads.sort(key=lambda h: (h.rank, _SENIORITY_ORDER.get(h.seniority or "", 99), h.since or "9999",
                                      h.person_id or 0))
            return heads[0], errors
    return None, errors


# --------------------------------------------------------------------------- hold-and-verify

def hold_decision(fraud_flags: list[str], buyer_verified: bool, amount_inr: Optional[int],
                  hold_min_inr: int = HOLD_UNVERIFIED_MIN_INR) -> tuple[bool, Optional[str]]:
    """Decision-table rows 1 and 2. decide.py can call this so both stay in step."""
    if fraud_flags:
        return True, "Fraud flag: " + ", ".join(fraud_flags)
    if not buyer_verified and amount_inr is not None and amount_inr >= hold_min_inr:
        return True, f"Buyer unverified and the amount is at least ₹{hold_min_inr // 100_000} lakh"
    return False, None


def build_verification_email(case_id: str, company_name: str, head: FinanceHead, req: Request,
                             domain: DomainCheck, base_url: str = "") -> dict:
    """The out-of-band check. It goes to the sandbox; to_intended shows who it is meant for."""
    from .explain import format_inr               # local import: explain imports verify

    amount = format_inr(req.amount_inr) if req.amount_inr else "amount not stated"
    terms = f"{req.terms_days} days" if req.terms_days else "terms not stated"
    sender = f"{req.sender_name or 'name not given'} <{req.sender_email or 'no email'}>"
    link = (base_url.rstrip("/") + f"/case/{case_id}/verify") if base_url else f"/case/{case_id}/verify"
    body = "\n".join([
        f"Dear {head.name},",
        "",
        f"Acme Supplies received a request to buy on credit from someone who says they work at {company_name}.",
        f"- Sender: {sender}",
        f"- Order: {req.goods or 'goods not stated'}, {amount}, {terms}",
        f"- Ship to: {req.ship_to_city or 'not stated'}",
        f"- Email domain check: {domain.detail}",
        "",
        f"We hold every order like this until someone at {company_name} confirms it. We found your name "
        "ourselves and have not replied to the sender.",
        "",
        "Is this order genuine?",
        f"Confirm: {link}?answer=confirm",
        f"Deny: {link}?answer=deny",
        "",
        "Credit desk, Acme Supplies",
    ])
    return {
        "key": f"{case_id}:verify:v1",
        "to_intended": f"{head.name}, {head.title}, {company_name} (email not looked up: contact lookup is disabled)",
        "subject": f"Please confirm an order placed in {company_name}'s name (ref {case_id})",
        "body": body,
    }


@dataclass
class VerifyResult:
    domain: DomainCheck
    sender: SenderCheck
    buyer_verified: bool
    fraud_flags: list[str] = field(default_factory=list)
    hold: bool = False
    hold_reason: Optional[str] = None
    finance_head: Optional[FinanceHead] = None
    verification_email: Optional[dict] = None
    needs_human: bool = False
    notes: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)


def verify_buyer(client: Any, req: Request, res: Resolution, case_date: Optional[str] = None,
                 hold_min_inr: int = HOLD_UNVERIFIED_MIN_INR, base_url: str = "") -> VerifyResult:
    case_date = case_date or date.today().isoformat()
    domain = check_domain(req.sender_email, res.primary_domain, res.all_domains)
    sender = check_sender(client, res.company_id, req.sender_name, req.sender_email)
    fraud = []
    if domain.status == "lookalike":
        fraud.append(f"lookalike_domain:{domain.kind}")
    if "name_collision" in res.flags:
        fraud.append("name_collision")
    buyer_verified = (domain.passed and sender.status in ("pass", "note", "unnamed")
                      and "name_domain_mismatch" not in res.flags)
    hold, reason = hold_decision(fraud, buyer_verified, req.amount_inr, hold_min_inr)
    out = VerifyResult(domain, sender, buyer_verified, fraud, hold, reason)
    if sender.status == "pass" and domain.status == "lookalike":
        out.notes.append("The sender's name matches a real employee, but the email domain is fake: "
                         "a typical impersonation.")
    if "name_domain_mismatch" in res.flags:
        out.notes.append("The company named in the request is not the company that owns the email domain.")

    src_company = f"company/identify #{res.company_id} basic_info.primary_domain" if res.company_id else "request"
    out.evidence.append(Evidence(
        id=f"{req.case_id}:domain_check", case_id=req.case_id, signal="domain_check",
        value={"result": domain.status, "kind": domain.kind, "sender_domain": domain.sender_domain,
               "primary_domain": domain.primary_domain, "detail": domain.detail},
        source=src_company, as_of=case_date, status="verified", cost=0.0))
    out.evidence.append(Evidence(
        id=f"{req.case_id}:sender_check", case_id=req.case_id, signal="sender_check",
        value={"result": sender.status, "name": sender.name, "title": sender.title, "since": sender.since,
               "detail": sender.detail},
        source=f"person/search current.company_id={res.company_id} + basic_profile.name",
        as_of=case_date, status="unavailable" if sender.unavailable else "verified",
        cost=0.13 * max(sender.matches, 1) if res.company_id and not sender.unavailable else 0.0))

    if hold:
        head, errors = find_finance_head(client, res.company_id)
        out.finance_head = head
        if head:
            out.verification_email = build_verification_email(req.case_id, res.name or req.company_name or "the company",
                                                              head, req, domain, base_url)
            out.evidence.append(Evidence(
                id=f"{req.case_id}:finance_head", case_id=req.case_id, signal="finance_head",
                value={"name": head.name, "title": head.title, "since": head.since, "fallback": head.fallback},
                source=f"person/search current.company_id={res.company_id} + finance titles",
                as_of=case_date, status="verified", cost=0.13))
        else:
            out.needs_human = True
            out.notes.append("No finance head or founder found to verify with; a person must decide.")
            out.evidence.append(Evidence(
                id=f"{req.case_id}:finance_head", case_id=req.case_id, signal="finance_head",
                value={"error": "; ".join(errors) or "nobody found"},
                source=f"person/search current.company_id={res.company_id}", as_of=case_date,
                status="unavailable", cost=0.0))
    return out


def apply_verification_answer(case_id: str, answer: str, reviewer: str, sender_domain: Optional[str] = None,
                              at: Optional[str] = None) -> dict:
    """The finance head (played by a teammate) clicked Confirm or Deny."""
    at = at or date.today().isoformat()
    confirmed = answer.strip().lower() in ("confirm", "confirmed", "yes")
    ev = Evidence(
        id=f"{case_id}:buyer_confirmation", case_id=case_id, signal="buyer_confirmation",
        value={"result": "confirmed" if confirmed else "denied", "reviewer": reviewer},
        source="out-of-band check with the finance head", as_of=at, status="verified", cost=0.0)
    if confirmed:
        return {"state": "VERIFIED", "evidence": ev, "confirmed_domain": clean_domain(sender_domain)}
    return {"state": "DECLINED", "evidence": ev,
            "fraud_attempt": {"case_id": case_id, "sender_domain": clean_domain(sender_domain), "at": at,
                              "reported_by": reviewer}}


# --------------------------------------------------------------------------- ship-to (stretch)

def check_ship_to(ship_to_city: Optional[str], hq_city: Optional[str],
                  by_region_absolute: Optional[dict]) -> dict:
    """Amber note when goods go to a city where the company lists no employees."""
    city = canonical_city(ship_to_city) or ship_to_city
    if not city:
        return {"status": "unavailable", "detail": "No ship-to city in the request."}
    if hq_city and canonical_city(hq_city) == city:
        return {"status": "match", "detail": f"{city} is the company's head office city."}
    if not by_region_absolute:
        return {"status": "unavailable", "detail": "No headcount by region for this company."}
    for region, count in by_region_absolute.items():
        if canonical_city(str(region)) == city and (count or 0) > 0:
            return {"status": "match", "detail": f"The company lists {count} people in {region}."}
    return {"status": "amber", "detail": f"The company lists no employees in {city}."}


# --------------------------------------------------------------------------- pipeline adapter (role A)

def domain_check(sender_domain: Optional[str], primary: Optional[str]) -> tuple[str, str]:
    """Short form used in tests: exact | lookalike | alt_domain | free_mail | unrelated | missing."""
    d = check_domain(sender_domain, primary)
    if d.status == "pass":
        return "exact", d.detail
    if d.status == "lookalike":
        return "lookalike", d.detail
    return {"different_ending": "alt_domain", "free_email": "free_mail", "no_email": "missing"}.get(d.kind, "unrelated"), d.detail


def verify(case_id: str, req: Request, company):
    """Called by pipeline.step_verify. Returns contracts.VerifyResult and saves the evidence rows."""
    from . import config, store
    from .contracts import VerifyResult as VR
    from .crustdata import client
    import yaml
    res = Resolution(status="resolved", company_id=company.crustdata_id, name=company.name, primary_domain=company.primary_domain,
                     all_domains=company.all_domains, flags=list(company.flags))
    hold_min = yaml.safe_load(config.POLICY_PATH.read_text())["defaults"]["unverified_hold_from_inr"]
    v = verify_buyer(client(), req, res, hold_min_inr=hold_min)
    ids = [store.add_evidence(ev).id for ev in v.evidence]
    status = "fraud" if v.fraud_flags else "pass" if v.buyer_verified else "unverified"
    flags = list(v.fraud_flags)
    if not v.buyer_verified and not v.fraud_flags:
        flags.append(f"domain_{v.domain.kind}" if not v.domain.passed else f"sender_{v.sender.status}")
    if v.sender.status == "note":
        flags.append("sender_not_buying_role")
    head = None
    if v.finance_head:
        h = v.finance_head
        head = {"name": h.name, "title": h.title, "since": h.since, "crustdata_person_id": h.person_id, "fallback": h.fallback}
    return VR(status=status, flags=flags, summary=[f"Domain: {v.domain.detail.rstrip('.')}", f"Sender: {v.sender.detail.rstrip('.')}"] + [n.rstrip('.') for n in v.notes],
              evidence_ids=ids, finance_head=head, verification_email=v.verification_email, notes=v.notes)
