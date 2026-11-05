"""Fixed credit rules from policy.yaml. No model is involved: the same evidence always gives the same decision."""
from __future__ import annotations

from typing import Optional

import yaml

from . import config, store
from .contracts import Company, Decision, Reason, Request, SignalResult, VerifyResult
from .money import inr


def policy() -> dict:
    return yaml.safe_load(config.POLICY_PATH.read_text())


def base_limit(profiles: Optional[int], pol: dict) -> int:
    for band in pol["base_limits"]:
        if band["max_profiles"] is None or (profiles or 0) <= band["max_profiles"]:
            return band["limit_inr"]
    return pol["base_limits"][-1]["limit_inr"]


def needs_hold(req: Request, vr: VerifyResult) -> tuple[bool, str]:
    d = policy()["defaults"]
    if vr.status == "fraud":
        return True, "Possible purchase-order fraud: " + next((s.removeprefix("Domain: ") for s in vr.summary if s.startswith("Domain:")), "lookalike domain")
    if vr.status == "unverified" and (req.amount_inr or 0) >= d["unverified_hold_from_inr"]:
        return True, f"Sender could not be verified and the request is {inr(req.amount_inr)}: " + "; ".join(vr.summary)
    return False, ""


def hold_decision(req: Request, company: Company, vr: VerifyResult, why: str) -> Decision:
    pol = policy()
    reasons = [Reason(text=why, evidence_ids=vr.evidence_ids)]
    if vr.finance_head:
        reasons.append(Reason(text=f"Verification sent to {vr.finance_head['name']} ({vr.finance_head['title']}), found independently of the sender",
                              evidence_ids=[]))
    return Decision(decision="hold_verify", rule="R1 fraud flag" if vr.status == "fraud" else "R2 unverified and large",
                    fraud_flags=vr.flags, reasons=reasons, requested_inr=req.amount_inr, requested_terms=req.terms_days,
                    policy_version=pol["version"], needs_human=False)


def _profiles(case_id: str) -> Optional[int]:
    for ev in store.list_evidence(case_id):
        if ev.signal == "profiles" and isinstance(ev.value, dict):
            return ev.value.get("by_company_id")
    return None


def decide(req: Request, company: Company, vr: Optional[VerifyResult], sigs: list[SignalResult],
           account: Optional[dict], case_id: str) -> Decision:
    pol = policy()
    d = pol["defaults"]
    rules = {r["id"]: r for r in pol["rules"]}
    reds = [s for s in sigs if s.level == "red"]
    ambers = [s for s in sigs if s.level == "amber"]
    oks = [s for s in sigs if s.level == "ok" and s.signal not in ("profiles",)]
    unknown = [s for s in sigs if s.level == "na" and s.signal != "profiles"]
    profiles = _profiles(case_id)
    base = base_limit(profiles, pol)
    requested, terms_req = req.amount_inr, req.terms_days or d["terms_days"]
    outstanding = int((account or {}).get("outstanding_inr") or 0)
    zombie = any(s.signal == "joiner_rate" and s.level == "red" for s in sigs)
    no_history = all(s.level == "na" for s in sigs if s.signal in ("headcount_trend", "joiner_rate"))
    thin = profiles is None or profiles < d["min_profiles"] or no_history

    reasons = [Reason(text=s.summary, evidence_ids=s.evidence_ids) for s in reds + ambers]
    if vr:
        reasons.append(Reason(text="Buyer check: " + "; ".join(vr.summary), evidence_ids=vr.evidence_ids))

    advance_pct, needs_human = 0, False
    if zombie:
        rule, kind, allowed, terms = "R3 zombie (nobody joining)", "advance_only", 0, 0
        advance_pct = 100
    elif len(reds) >= 2:
        rule, kind, allowed, terms = "R4 two or more red signals", "advance_only", 0, 0
        advance_pct = 100
    elif thin:
        rule, allowed, terms = "R5 thin data", d["thin_data_cap_inr"], d["thin_data_terms_days"]
        kind = "approve"
        if requested and requested > allowed:
            needs_human = True
            reasons.append(Reason(text=f"Too little public data to justify more than {inr(allowed)}; a person should decide", evidence_ids=[]))
    elif len(reds) == 1:
        r = rules["R6"]
        rule, kind = "R6 one red signal", "reduce"
        allowed, terms = int(base * r["share_of_base"]), min(terms_req, r["max_terms_days"])
    elif len(ambers) >= 2:
        r = rules["R7"]
        rule, kind = "R7 two or more amber signals", "reduce"
        allowed, terms = int(base * r["share_of_base"]), min(terms_req, r["max_terms_days"])
    else:
        r = rules["R8"]
        rule, kind = "R8 clean", "approve"
        allowed, terms = int(base * r["share_of_base"]), min(terms_req, r["max_terms_days"])
        reasons += [Reason(text=s.summary, evidence_ids=s.evidence_ids) for s in oks[:4]]

    # Unverified sender on a small request: cap below the hold threshold until verified.
    cap = d.get("unverified_cap_inr", d["unverified_hold_from_inr"] - 1)
    if vr and vr.status == "unverified" and allowed > cap:
        allowed = cap
        rule += " + capped (sender unverified)"

    available = max(0, allowed - outstanding)
    if outstanding:
        reasons.append(Reason(text=f"Already owed by this buyer: {inr(outstanding)}; limits apply to total exposure", evidence_ids=[]))
    if kind == "advance_only":
        grant = 0
    elif requested:
        grant = min(requested, available)
        if grant < requested or terms < terms_req:
            kind = "reduce" if grant > 0 else "advance_only"
            if grant == 0:
                advance_pct, terms = 100, 0
    else:
        grant = available
        reasons.append(Reason(text=f"No amount was stated, so this is the maximum safe limit ({inr(grant)})", evidence_ids=[]))

    # Limits only fall automatically: never raise an earlier decision on this case without a person.
    _, prev = store.latest_decision(case_id)
    if prev and prev.decision not in ("hold_verify", "decline") and grant > prev.limit_inr:
        grant, terms = prev.limit_inr, prev.terms_days
        reasons.append(Reason(text=f"Raising the limit needs a person; kept at {inr(grant)}", evidence_ids=[]))

    if requested and requested > d["refer_above_inr"]:
        needs_human = True
        rule += " + refer (over Rs 50 lakh)"

    return Decision(decision=kind, limit_inr=int(grant), terms_days=int(terms), advance_pct=advance_pct,
                    fraud_flags=vr.flags if vr else [], red=[s.signal for s in reds], amber=[s.signal for s in ambers],
                    reasons=reasons, could_not_verify=[s.summary for s in unknown], requested_inr=requested,
                    requested_terms=req.terms_days, exposure_inr=outstanding + (requested or 0), base_limit_inr=base,
                    rule=rule, policy_version=pol["version"], needs_human=needs_human)
