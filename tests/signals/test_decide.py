"""One golden test per decision rule, built from hand-made signal lists (no network)."""
import pytest

from termsdesk import decide, store
from termsdesk.contracts import Company, Decision, Evidence, Request, SignalResult, VerifyResult

CO = Company(crustdata_id=1, name="Testco")
OK = VerifyResult(status="pass", summary=["Domain: ok"])


def sig(name, level, text="x"):
    return SignalResult(signal=name, level=level, summary=f"{name} {level} {text}", evidence_ids=[f"e-{name}"])


def clean(**over):
    base = {s: "ok" for s in ("profiles", "joiner_rate", "headcount_trend", "finance_head_exit", "finance_team_exits",
                              "clevel_exits", "funding_age", "company_age", "job_posts", "website", "distress_news")}
    base.update(over)
    return [sig(k, v) for k, v in base.items()]


def run(sigs, amount=3_000_000, terms=45, profiles=3582, vr=OK, account=None, case="c"):
    if profiles is not None:
        store.add_evidence(Evidence(case_id=case, signal="profiles", value={"by_company_id": profiles}, source="test"))
    req = Request(case_id=case, raw_text="t", amount_inr=amount, terms_days=terms)
    return decide.decide(req, CO, vr, sigs, account, case_id=case)


def test_r3_zombie_is_advance_only():
    d = run(clean(joiner_rate="red"))
    assert (d.decision, d.limit_inr, d.advance_pct) == ("advance_only", 0, 100) and d.rule.startswith("R3")


def test_r4_two_reds_is_advance_only():
    d = run(clean(headcount_trend="red", finance_head_exit="red"))
    assert d.decision == "advance_only" and d.rule.startswith("R4")


def test_r5_thin_data_caps_and_refers():
    d = run(clean(), profiles=12)
    assert d.rule.startswith("R5") and d.limit_inr == 100_000 and d.terms_days == 15 and d.needs_human


def test_r5_when_headcount_and_joiners_are_both_unknown():
    d = run(clean(headcount_trend="na", joiner_rate="na"), amount=80_000)
    assert d.rule.startswith("R5") and d.limit_inr == 80_000 and not d.needs_human


def test_r6_one_red_is_a_quarter_of_base_on_30_days():
    d = run(clean(distress_news="red"), amount=6_000_000)          # base Rs 1.5 crore -> 25% = Rs 37.5 lakh
    assert d.rule.startswith("R6") and (d.decision, d.limit_inr, d.terms_days) == ("reduce", 3_750_000, 30)


def test_r7_two_ambers_is_half_of_base():
    d = run(clean(website="amber", company_age="amber"), amount=9_000_000, profiles=500)   # base Rs 75 lakh
    assert d.rule.startswith("R7") and (d.limit_inr, d.terms_days) == (3_750_000, 30)


def test_r8_clean_approves_the_request():
    d = run(clean(clevel_exits="amber"))
    assert (d.decision, d.limit_inr, d.terms_days) == ("approve", 3_000_000, 45) and d.rule == "R8 clean"


def test_r8_caps_terms_at_60_days():
    d = run(clean(), terms=90)
    assert d.terms_days == 60 and d.decision == "reduce"


def test_missing_amount_gives_the_maximum_safe_limit():
    d = run(clean(), amount=None)
    assert d.limit_inr == 15_000_000 and any("maximum safe limit" in r.text for r in d.reasons)


def test_unverified_sender_is_capped_below_the_hold_threshold():
    d = run(clean(), amount=400_000, vr=VerifyResult(status="unverified", summary=["Sender: not found"]))
    assert d.limit_inr == 400_000
    d2 = run(clean(), amount=None, vr=VerifyResult(status="unverified", summary=["Sender: not found"]), case="c2")
    assert d2.limit_inr == 499_999 and "capped" in d2.rule


def test_limits_apply_to_total_exposure():
    d = run(clean(), amount=3_000_000, profiles=150, account={"outstanding_inr": 2_000_000})   # base Rs 30 lakh
    assert (d.limit_inr, d.exposure_inr) == (1_000_000, 5_000_000) and d.decision == "reduce"


def test_over_50_lakh_goes_to_a_human():
    d = run(clean(), amount=6_000_000)
    assert d.needs_human and "refer" in d.rule


def test_a_re_decision_never_raises_the_limit():
    store.save_decision("c", Decision(decision="reduce", limit_inr=1_000_000, terms_days=30, policy_version="v3"))
    d = run(clean(), amount=3_000_000)
    assert d.limit_inr == 1_000_000 and any("needs a person" in r.text for r in d.reasons)


def test_same_signals_same_decision():
    a = run(clean(website="amber"), case="x1")
    b = run(clean(website="amber"), case="x2")
    assert a.model_dump(exclude={"reasons"}) == b.model_dump(exclude={"reasons"})


@pytest.mark.parametrize("vr,amount,hold", [
    (VerifyResult(status="fraud", flags=["lookalike_domain"], summary=["Domain: razorpay-payments.co imitates razorpay.com"]), 100_000, True),
    (VerifyResult(status="unverified", summary=["Sender: not found"]), 500_000, True),
    (VerifyResult(status="unverified", summary=["Sender: not found"]), 499_000, False),
    (OK, 9_000_000, False),
])
def test_r1_r2_hold_rules(vr, amount, hold):
    assert decide.needs_hold(Request(case_id="h", raw_text="t", amount_inr=amount), vr)[0] is hold
