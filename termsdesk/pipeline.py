"""The case state machine. Each step saves its output and the next state before returning,
so run(case_id) after a crash picks up at the first unfinished step (resume == run)."""
from __future__ import annotations

import traceback

from . import decide, explain, intake, monitor, outbox, resolve, signals, store, verify
from .contracts import Company, Decision, Request, State, VerifyResult
from .crustdata import OutOfCredits, current_case


def _req(case: dict) -> Request:
    return Request(**case["request"])


def _company(case: dict) -> Company:
    return Company(**case["company"])


def step_parse(case: dict) -> None:
    req = intake.parse(case["id"], case["raw_text"])
    store.update_case(case["id"], request=req.model_dump())
    amt = f"Rs {req.amount_inr:,}" if req.amount_inr else "no amount"
    store.set_state(case["id"], State.PARSED, f"Parsed by {req.parsed_by}: {req.company_name or req.company_domain or '?'}, {amt}, "
                                              f"{req.terms_days or 'default'} days, sender {req.sender_email or req.sender_name or '?'}")


def step_resolve(case: dict) -> None:
    req = _req(case)
    try:
        company = resolve.resolve(req)
    except resolve.NeedsHuman as e:
        store.queue_human(case["id"], "identity", str(e))
        store.set_state(case["id"], State.REFERRED, f"Referred to a human: {e}")
        return
    store.update_case(case["id"], company=company.model_dump(), company_id=company.crustdata_id)
    store.set_state(case["id"], State.RESOLVED, f"Resolved to {company.name} (Crustdata #{company.crustdata_id}, "
                                                f"matched on {company.matched_on}, confidence {company.match_confidence:.2f})")


def step_verify(case: dict) -> None:
    req, company = _req(case), _company(case)
    vr = verify.verify(case["id"], req, company)
    store.update_case(case["id"], verify=vr.model_dump())
    hold, why = decide.needs_hold(req, vr)
    if hold:
        decision = decide.hold_decision(req, company, vr, why)
        version = store.save_decision(case["id"], decision)
        outbox.start_hold(case["id"], version, req, company, vr)
        store.queue_human(case["id"], "hold_verify", why)
        store.set_state(case["id"], State.HOLD_VERIFY, f"On hold, no credit: {why}")
        return
    store.set_state(case["id"], State.VERIFIED, "; ".join(vr.summary) or "Buyer verified")


def step_evidence(case: dict) -> None:
    results = signals.collect(case["id"], _company(case))
    store.update_case(case["id"], signals=[r.model_dump() for r in results])
    levels = {lvl: sum(1 for r in results if r.level == lvl) for lvl in ("red", "amber", "ok", "na")}
    store.set_state(case["id"], State.EVIDENCE_DONE,
                    f"{len(results)} signals: {levels['red']} red, {levels['amber']} amber, {levels['ok']} clear, {levels['na']} could not verify")


def step_decide(case: dict) -> None:
    req, company = _req(case), _company(case)
    vr = VerifyResult(**case["verify"]) if case.get("verify") else None
    from .contracts import SignalResult
    sigs = [SignalResult(**s) for s in (case.get("signals") or [])]
    account = store.get_account(company.crustdata_id)
    decision = decide.decide(req, company, vr, sigs, account, case_id=case["id"])
    store.save_decision(case["id"], decision)
    store.update_case(case["id"], policy_version=decision.policy_version)
    store.set_state(case["id"], State.DECIDED, f"{decision.decision} Rs {decision.limit_inr:,} on {decision.terms_days} days (rule {decision.rule})")


def step_explain(case: dict) -> None:
    version, decision = store.latest_decision(case["id"])
    subject, body, by = explain.write(case["id"], _req(case), _company(case), decision)
    store.save_letter(case["id"], version, subject, body, by)
    store.set_state(case["id"], State.EXPLAINED, f"Letter v{version} written ({by})")


def step_act(case: dict) -> None:
    version, decision = store.latest_decision(case["id"])
    req, company = _req(case), _company(case)
    if decision.needs_human:
        store.queue_human(case["id"], "refer", "; ".join(r.text for r in decision.reasons[:2]) or decision.rule)
        store.set_state(case["id"], State.REFERRED, f"Referred to a human ({decision.rule})")
        return
    letter = store.latest_letter(case["id"])
    sent = outbox.send_decision(case["id"], version, req, letter)
    store.upsert_account(company.crustdata_id, limit_inr=decision.limit_inr, terms_days=decision.terms_days,
                         advance_pct=decision.advance_pct, status=decision.decision, last_case_id=case["id"])
    store.set_state(case["id"], State.ACTED, ("Letter sent" if sent else "Letter already sent earlier (not resent)") +
                    f"; account set to Rs {decision.limit_inr:,} on {decision.terms_days} days")
    monitor.register(case["id"], company)
    store.set_state(case["id"], State.MONITORING, f"Watching {company.name} for changes")


STEPS = {
    State.RECEIVED.value: step_parse,
    State.PARSED.value: step_resolve,
    State.RESOLVED.value: step_verify,
    State.VERIFIED.value: step_evidence,
    State.EVIDENCE_DONE.value: step_decide,
    State.DECIDED.value: step_explain,
    State.EXPLAINED.value: step_act,
}


def run(case_id: str) -> str:
    """Run (or resume) a case until it finishes or waits for a person."""
    token = current_case.set(case_id)
    try:
        for _ in range(30):
            case = store.get_case(case_id)
            step = STEPS.get(case["state"])
            if step is None:
                return case["state"]
            try:
                step(case)
            except OutOfCredits as e:
                store.set_state(case_id, State.FAILED, f"Stopped: {e}")
                return State.FAILED.value
            except Exception as e:  # noqa: BLE001 - record and stop; resume() retries the same step
                store.update_case(case_id, error=traceback.format_exc()[-2000:])
                store.log_event(case_id, "ERROR", f"{case['state']} step failed: {type(e).__name__}: {e}")
                return case["state"]
        return store.get_case(case_id)["state"]
    finally:
        current_case.reset(token)


resume = run


def submit(raw_text: str) -> str:
    case_id = store.create_case(raw_text)
    run(case_id)
    return case_id


def confirm_hold(case_id: str, confirmed: bool, reviewer: str) -> str:
    """The independently found finance head confirmed (or denied) the order."""
    case = store.get_case(case_id)
    if case["state"] != State.HOLD_VERIFY.value:
        return case["state"]
    for item in store.list_queue():
        if item["case_id"] == case_id and item["kind"] == "hold_verify":
            store.resolve_queue_item(item["id"], reviewer, "confirmed" if confirmed else "denied")
    if not confirmed:
        decision = Decision(decision="decline", rule="hold denied by finance head", policy_version=decide.policy()["version"],
                            reasons=[], fraud_flags=(VerifyResult(**case["verify"]).flags if case.get("verify") else []))
        version = store.save_decision(case_id, decision)
        store.set_state(case_id, State.DECLINED, f"Finance head denied the order; logged as a fraud attempt (decision v{version})")
        return State.DECLINED.value
    vr = VerifyResult(**case["verify"])
    vr.status, vr.summary = "pass", vr.summary + [f"Order confirmed out of band by the finance head ({reviewer})"]
    store.update_case(case_id, verify=vr.model_dump())
    store.set_state(case_id, State.VERIFIED, "Finance head confirmed the order out of band")
    return run(case_id)


def approve_referral(case_id: str, reviewer: str, approve: bool) -> str:
    case = store.get_case(case_id)
    if case["state"] != State.REFERRED.value:
        return case["state"]
    for item in store.list_queue():
        if item["case_id"] == case_id and item["kind"] in ("refer", "identity"):
            store.resolve_queue_item(item["id"], reviewer, "approved" if approve else "rejected")
    version, decision = store.latest_decision(case_id)
    if decision is None:
        store.set_state(case_id, State.FAILED, "Referral closed without a decision")
        return State.FAILED.value
    decision.needs_human = False
    if not approve:
        decision.decision, decision.limit_inr, decision.terms_days, decision.advance_pct = "advance_only", 0, 0, 100
    decision.rule += f"; {'approved' if approve else 'changed to advance only'} by {reviewer}"
    store.save_decision(case_id, decision)
    store.set_state(case_id, State.DECIDED, f"Human review by {reviewer}: {'approved' if approve else 'advance only'}")
    return run(case_id)


def redecide(case_id: str, why: str) -> str:
    """A watched fact changed: collect evidence again and decide again (limits only fall automatically)."""
    store.set_state(case_id, State.VERIFIED, f"Watched fact changed: {why}. Re-collecting evidence")
    return run(case_id)
