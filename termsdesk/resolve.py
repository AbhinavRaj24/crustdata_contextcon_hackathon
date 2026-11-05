"""Resolve: one Crustdata company per request, aliases merged, exposure summed.

Order of evidence, strongest first:
  1. The request's domain equals a company's primary_domain (identify by domain, free).
  2. A name whose core (legal words like "Pvt Ltd", "Software" removed) equals the
     claimed name's core. If several companies share the core name (Crustdata has four
     called "Dunzo"), the largest wins only when it is at least 10x the next one.
  3. Otherwise the case goes to a human with the candidates listed.
Matches on all_domains alone are never trusted (Razorpay's list holds bitly.com).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

from .contracts import Evidence, Request
from .intake import FREE_EMAIL_DOMAINS, clean_domain, email_domain, name_from_domain

IDENTIFY = "/company/identify"

GENERIC_NAME_WORDS = frozenset({
    "pvt", "private", "ltd", "limited", "llp", "inc", "incorporated", "corp", "corporation", "co",
    "company", "plc", "opc", "pte", "gmbh", "the", "india", "technologies", "technology",
    "software", "solutions", "services", "digital",
})
SOLO_RANGES = frozenset({"myself only", "1", "0-1", "1-1", "self-employed"})
SOLO_TYPES = frozenset({"self-employed", "self employed", "self-owned"})
SIZE_DOMINANCE = 10          # largest same-name company must be this many times the next


def call_api(client: Any, path: str, body: dict) -> Any:
    """Call the Crustdata client. Accepts an object with .post(path, body) or a callable."""
    if hasattr(client, "post"):
        return client.post(path, body)
    if callable(client):
        return client(path, body)
    raise TypeError("client must have .post(path, body) or be callable")


def core_name(name: Optional[str]) -> str:
    """'Razorpay Software Pvt Ltd' -> 'razorpay'; 'DUNZO DIGITAL PRIVATE LIMITED' -> 'dunzo'."""
    if not name:
        return ""
    s = re.sub(r"[^a-z0-9 ]+", " ", name.lower().replace("&", " and "))
    toks = s.split()
    core = [t for t in toks if t not in GENERIC_NAME_WORDS]
    return " ".join(core) if core else " ".join(toks)


def size_lower_bound(employee_count_range: Optional[str]) -> int:
    r = (employee_count_range or "").strip().lower()
    if not r:
        return 0
    if r in SOLO_RANGES:
        return 1
    m = re.match(r"(\d[\d,]*)", r)
    return int(m.group(1).replace(",", "")) if m else 0


@dataclass
class Candidate:
    company_id: int
    name: str
    primary_domain: Optional[str]
    all_domains: list[str]
    confidence: float
    employee_count_range: Optional[str]
    company_type: Optional[str]
    year_founded: Optional[int]

    @property
    def size(self) -> int:
        return size_lower_bound(self.employee_count_range)

    @property
    def solo(self) -> bool:
        return ((self.employee_count_range or "").strip().lower() in SOLO_RANGES
                or (self.company_type or "").strip().lower() in SOLO_TYPES)

    def brief(self) -> dict:
        return {"crustdata_company_id": self.company_id, "name": self.name,
                "primary_domain": self.primary_domain, "employee_count_range": self.employee_count_range,
                "confidence": self.confidence}


def parse_identify(resp: Any) -> list[tuple[str, list[Candidate]]]:
    """[(matched_on, [Candidate, ...]), ...] from a /company/identify reply."""
    items = resp if isinstance(resp, list) else (resp or {}).get("results") or (resp or {}).get("data") or []
    out = []
    for item in items:
        cands = []
        for m in item.get("matches") or []:
            cd = m.get("company_data") or {}
            bi = cd.get("basic_info") or {}
            cid = cd.get("crustdata_company_id") or bi.get("crustdata_company_id")
            if cid is None:
                continue
            cands.append(Candidate(
                company_id=int(cid), name=bi.get("name") or "", primary_domain=clean_domain(bi.get("primary_domain")),
                all_domains=[d for d in (clean_domain(x) for x in (bi.get("all_domains") or [])) if d],
                confidence=float(m.get("confidence_score") or 0.0),
                employee_count_range=bi.get("employee_count_range"), company_type=bi.get("company_type"),
                year_founded=bi.get("year_founded"),
            ))
        out.append((str(item.get("matched_on") or ""), cands))
    return out


@dataclass
class Resolution:
    status: str                                   # resolved | ambiguous | not_found | unavailable
    company_id: Optional[int] = None
    name: Optional[str] = None
    primary_domain: Optional[str] = None
    all_domains: list[str] = field(default_factory=list)
    year_founded: Optional[int] = None
    employee_count_range: Optional[str] = None
    match: Optional[str] = None                   # domain_exact | name_exact | name_largest | name_confident
    confidence: Optional[float] = None
    candidates: list[dict] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    outstanding_inr: int = 0
    exposure_inr: Optional[int] = None
    flags: list[str] = field(default_factory=list)     # name_collision | name_domain_mismatch
    needs_human: bool = False
    reason: Optional[str] = None
    notes: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)


def merge_aliases(existing: Optional[list[str]], *names: Optional[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for n in list(existing or []) + list(names):
        if not n:
            continue
        key = re.sub(r"\s+", " ", n.strip()).lower()
        if key and key not in seen:
            seen.add(key)
            out.append(re.sub(r"\s+", " ", n.strip()))
    return out


def total_exposure(outstanding_inr: Optional[int], amount_inr: Optional[int]) -> int:
    return int(outstanding_inr or 0) + int(amount_inr or 0)


def _pick_by_name(name: str, cands: list[Candidate]) -> tuple[str, Optional[Candidate], str, list[Candidate]]:
    """Returns (status, chosen, match_kind, shortlist)."""
    live = [c for c in cands if not c.solo]
    if not live:
        return "not_found", None, "", []
    q = core_name(name)
    exact = [c for c in live if core_name(c.name) == q]
    if exact:
        exact.sort(key=lambda c: (-c.size, c.company_id))
        if len(exact) == 1:
            return "resolved", exact[0], "name_exact", exact
        top, nxt = exact[0], exact[1]
        if top.size >= SIZE_DOMINANCE * max(nxt.size, 1):
            return "resolved", top, "name_largest", exact
        return "ambiguous", None, "", exact[:3]
    live.sort(key=lambda c: (-c.confidence, -c.size, c.company_id))
    best = live[0]
    second = live[1] if len(live) > 1 else None
    if best.confidence >= 0.9 and (second is None or best.confidence - second.confidence > 0.1):
        return "resolved", best, "name_confident", live[:3]
    return "not_found", None, "", live[:3]


def resolve_company(client: Any, req: Request, lookup: Optional[Callable[[int], Optional[dict]]] = None,
                    case_date: Optional[str] = None) -> Resolution:
    """Find the buyer's company, merge aliases and sum exposure.

    lookup(company_id) returns the stored record, e.g. {"aliases": [...], "domains": [...],
    "outstanding_inr": 1000000}, or None for a new buyer. See sqlite_lookup() below.
    """
    case_date = case_date or date.today().isoformat()
    notes: list[str] = []
    domains: list[str] = []
    for d in (req.company_domain, email_domain(req.sender_email)):
        d = clean_domain(d)
        if d and d not in FREE_EMAIL_DOMAINS and d not in domains:
            domains.append(d)

    chosen: Optional[Candidate] = None
    match = None
    shortlist: list[Candidate] = []
    api_failed = False

    for d in domains:
        try:
            parsed = parse_identify(call_api(client, IDENTIFY, {"domains": [d]}))
        except Exception as exc:                       # the client already retried
            api_failed = True
            notes.append(f"Company Identify failed for {d}: {exc}")
            continue
        cands = [c for _, cs in parsed for c in cs]
        exact = [c for c in cands if c.primary_domain == d and not c.solo]
        if exact:
            exact.sort(key=lambda c: (-c.size, c.company_id))
            chosen, match = exact[0], "domain_exact"
            break
        if cands:
            notes.append(f"{d} matched only a secondary domain of {cands[0].name}; not trusted.")

    status = "resolved" if chosen else "not_found"
    name = req.company_name or name_from_domain(domains[0] if domains else None)
    if chosen is None and name:
        try:
            parsed = parse_identify(call_api(client, IDENTIFY, {"names": [name]}))
            cands = [c for _, cs in parsed for c in cs]
            status, chosen, match, shortlist = _pick_by_name(name, cands)
            if match == "name_largest":
                notes.append(f"{len(shortlist)} companies are called '{chosen.name}'; picked the largest.")
        except Exception as exc:
            api_failed = True
            notes.append(f"Company Identify failed for name '{name}': {exc}")
            status = "unavailable"

    if chosen is None and status == "not_found" and api_failed:
        status = "unavailable"

    res = Resolution(status=status, notes=notes, candidates=[c.brief() for c in shortlist])
    if chosen is None:
        res.needs_human = True
        if status == "ambiguous":
            a, b = shortlist[0], shortlist[1]
            res.reason = (f"Two candidate companies too close to call: {a.name} ({a.primary_domain or 'no domain'}, "
                          f"{a.employee_count_range or 'size unknown'}) and {b.name} "
                          f"({b.primary_domain or 'no domain'}, {b.employee_count_range or 'size unknown'}).")
        elif status == "unavailable":
            res.reason = "Company Identify could not be reached."
        else:
            res.reason = f"No confident match in Crustdata for '{name or req.company_domain or 'the request'}'."
        res.evidence.append(Evidence(
            id=f"{req.case_id}:company_identity", case_id=req.case_id, signal="company_identity",
            value={"error": res.reason, "candidates": [c.name for c in shortlist]}, source="company/identify",
            as_of=case_date, status="unavailable", cost=0.0))
        res.exposure_inr = req.amount_inr
        return res

    # Impersonation check: a tiny company owning the domain shares its name with a much bigger one.
    if match == "domain_exact" and req.company_name and core_name(req.company_name) == core_name(chosen.name) \
            and chosen.size < 50:
        try:
            parsed = parse_identify(call_api(client, IDENTIFY, {"names": [req.company_name]}))
            bigger = [c for _, cs in parsed for c in cs
                      if c.company_id != chosen.company_id and not c.solo
                      and core_name(c.name) == core_name(chosen.name)
                      and c.size >= SIZE_DOMINANCE * max(chosen.size, 1)]
            if bigger:
                big = max(bigger, key=lambda c: c.size)
                res.flags.append("name_collision")
                notes.append(f"{chosen.primary_domain} belongs to a small '{chosen.name}' "
                             f"({chosen.employee_count_range}); a much larger '{big.name}' uses {big.primary_domain}.")
        except Exception:
            pass

    if match == "domain_exact" and req.company_name and core_name(req.company_name) != core_name(chosen.name):
        res.flags.append("name_domain_mismatch")
        notes.append(f"The request names '{req.company_name}' but its domain belongs to '{chosen.name}'.")

    existing = (lookup(chosen.company_id) if lookup else None) or {}
    outstanding = int(existing.get("outstanding_inr") or 0)
    res.company_id = chosen.company_id
    res.name = chosen.name
    res.primary_domain = chosen.primary_domain
    res.all_domains = chosen.all_domains
    res.year_founded = chosen.year_founded
    res.employee_count_range = chosen.employee_count_range
    res.match = match
    res.confidence = chosen.confidence
    res.aliases = merge_aliases(existing.get("aliases"), chosen.name, req.company_name)
    res.outstanding_inr = outstanding
    res.exposure_inr = total_exposure(outstanding, req.amount_inr)
    res.evidence.append(Evidence(
        id=f"{req.case_id}:company_identity", case_id=req.case_id, signal="company_identity",
        value={"name": chosen.name, "crustdata_company_id": chosen.company_id,
               "primary_domain": chosen.primary_domain, "match": match},
        source=f"company/identify #{chosen.company_id} basic_info", as_of=case_date, status="verified", cost=0.0))
    res.evidence.append(Evidence(
        id=f"{req.case_id}:exposure", case_id=req.case_id, signal="exposure",
        value={"outstanding_inr": outstanding, "requested_inr": req.amount_inr, "exposure_inr": res.exposure_inr},
        unit="inr", source="accounts ledger", as_of=case_date, status="verified", cost=0.0))
    return res


# --------------------------------------------------------------------------- optional SQLite helpers
# These follow the spec's tables: companies(crustdata_id, name, domains, aliases) with JSON lists,
# accounts(company_id, limit_inr, terms_days, outstanding_inr, status). Adjust if store.py differs.

def sqlite_lookup(conn) -> Callable[[int], Optional[dict]]:
    def _lookup(company_id: int) -> Optional[dict]:
        row = conn.execute("SELECT name, domains, aliases FROM companies WHERE crustdata_id = ?",
                           (company_id,)).fetchone()
        acc = conn.execute("SELECT COALESCE(SUM(outstanding_inr), 0) FROM accounts WHERE company_id = ?",
                           (company_id,)).fetchone()
        if row is None and not (acc and acc[0]):
            return None
        out = {"outstanding_inr": int(acc[0] or 0) if acc else 0}
        if row is not None:
            out["domains"] = json.loads(row[1] or "[]")
            out["aliases"] = json.loads(row[2] or "[]")
        return out
    return _lookup


def save_company(conn, res: Resolution, confirmed_domains: Optional[list[str]] = None) -> None:
    """Upsert the resolved company with merged aliases. Only the primary domain and domains a
    human confirmed are stored: never a domain we flagged as a lookalike."""
    if res.company_id is None:
        return
    row = conn.execute("SELECT domains FROM companies WHERE crustdata_id = ?", (res.company_id,)).fetchone()
    domains = merge_aliases(json.loads(row[0] or "[]") if row else [], res.primary_domain, *(confirmed_domains or []))
    if row is None:
        conn.execute("INSERT INTO companies (crustdata_id, name, domains, aliases) VALUES (?, ?, ?, ?)",
                     (res.company_id, res.name, json.dumps(domains), json.dumps(res.aliases)))
    else:
        conn.execute("UPDATE companies SET name = ?, domains = ?, aliases = ? WHERE crustdata_id = ?",
                     (res.name, json.dumps(domains), json.dumps(res.aliases), res.company_id))


# --------------------------------------------------------------------------- pipeline adapter

class NeedsHuman(Exception):
    pass


def store_lookup(company_id: int) -> Optional[dict]:
    """Aliases and outstanding balance from our store (replaces the spec-table sqlite_lookup above)."""
    from . import store
    row = store.conn().execute("SELECT aliases_json FROM companies WHERE crustdata_id = ?", (company_id,)).fetchone()
    acct = store.get_account(company_id)
    if row is None and not acct:
        return None
    return {"aliases": json.loads(row["aliases_json"]) if row and row["aliases_json"] else [],
            "outstanding_inr": int((acct or {}).get("outstanding_inr") or 0)}


def resolve(req: Request):
    """Called by pipeline.step_resolve. Returns contracts.Company or raises NeedsHuman."""
    from . import store
    from .contracts import Company
    from .crustdata import client
    res = resolve_company(client(), req, lookup=store_lookup)
    for ev in res.evidence:
        store.add_evidence(ev)
    for note in res.notes:
        store.log_event(req.case_id, "RESOLVE_NOTE", note)
    if res.needs_human or res.company_id is None:
        raise NeedsHuman(res.reason or "Company could not be resolved")
    store.upsert_company(res.company_id, res.name, res.primary_domain, req.company_name)
    return Company(crustdata_id=res.company_id, name=res.name, primary_domain=res.primary_domain,
                   employee_count_range=res.employee_count_range, match_confidence=res.confidence or 0.0,
                   matched_on=res.match or "", candidates=res.candidates, all_domains=res.all_domains,
                   aliases=res.aliases, year_founded=res.year_founded, flags=res.flags)
