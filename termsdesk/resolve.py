"""Resolve the buyer to exactly one Crustdata company. Aliases merge into one account."""
from __future__ import annotations

import re
from typing import Optional

from . import store
from .contracts import Company, Request
from .crustdata import Unavailable, client
from .intake import FREE_MAIL


class NeedsHuman(Exception):
    pass


def size_rank(rng: Optional[str]) -> int:
    m = re.match(r"(\d+)", rng or "")
    return int(m.group(1)) if m else 0


def stem_from_domain(domain: Optional[str]) -> Optional[str]:
    """'razorpay-payments.co' -> 'razorpay payments' (used only to look up the real company by name)."""
    if not domain:
        return None
    label = domain.lower().removeprefix("www.").split(".")[0]
    return re.sub(r"[-_]+", " ", label).strip() or None


def _candidates(matches: list[dict]) -> list[dict]:
    out = []
    for m in matches:
        cd = m.get("company_data", {})
        b = cd.get("basic_info", {})
        out.append({
            "crustdata_id": cd.get("crustdata_company_id") or b.get("crustdata_company_id"),
            "name": b.get("name"), "primary_domain": (b.get("primary_domain") or "").lower() or None,
            "website": b.get("website"), "employee_count_range": b.get("employee_count_range"),
            "confidence": float(m.get("confidence_score") or 0),
        })
    return [c for c in out if c["crustdata_id"]]


def _lookup(req: Request) -> tuple[list[dict], str, str]:
    c = client()
    domain = req.company_domain if req.company_domain and req.company_domain not in FREE_MAIL else None
    if domain:
        try:
            cands = _candidates(c.identify(domain=domain))
        except Unavailable:
            cands = []
        if cands:
            return cands, "domain", domain
    names = [n for n in (req.company_name, stem_from_domain(domain)) if n]
    for name in names:
        tries = [name] + ([name.split()[0]] if " " in name else [])
        for q in tries:
            cands = _candidates(c.identify(name=q))
            if cands:
                return cands, "name", q
    raise NeedsHuman(f"No company found for '{req.company_name or domain or 'unknown'}'")


def resolve(req: Request) -> Company:
    cands, matched_on, query = _lookup(req)
    # Drop one-person shells unless nothing else matched.
    real = [x for x in cands if x["employee_count_range"] not in ("1", "0-1", "1-1")] or cands
    if matched_on == "domain":
        exact = [x for x in real if x["primary_domain"] == query]
        real = exact or real
    real.sort(key=lambda x: (x["confidence"], size_rank(x["employee_count_range"])), reverse=True)
    top = real[0]
    if len(real) > 1:
        second = real[1]
        if top["confidence"] < 0.95 and top["confidence"] - second["confidence"] < 0.1 and top["name"] != second["name"]:
            raise NeedsHuman(f"Two companies match '{query}': {top['name']} and {second['name']}")
    store.upsert_company(top["crustdata_id"], top["name"], top["primary_domain"], req.company_name)
    return Company(crustdata_id=top["crustdata_id"], name=top["name"], primary_domain=top["primary_domain"],
                   website=top["website"], employee_count_range=top["employee_count_range"],
                   match_confidence=top["confidence"], matched_on=matched_on, candidates=real[:5])
