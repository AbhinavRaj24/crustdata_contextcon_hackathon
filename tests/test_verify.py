import pytest

from request_cases import CASES
from fake_api import FakeClient
from termsdesk.contracts import Request
from termsdesk.intake import parse_request
from termsdesk.resolve import resolve_company
from termsdesk.verify import (apply_verification_answer, check_domain, check_sender, check_ship_to,
                              find_finance_head, hold_decision, verify_buyer)


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    monkeypatch.setenv("TERMSDESK_USE_MODEL", "0")


@pytest.mark.parametrize("sender,kind", [
    ("rzorpay.com", "changed_letters"), ("razorpay-payments.co", "extra_word"),
    ("razor-pay.com", "extra_hyphen"), ("razorpay.com.secure-login.co", "contains_real_domain"),
    ("getrazorpay.com", "extra_word"), ("razorpav.com", "changed_letters"),
])
def test_lookalikes_are_flagged(sender, kind):
    d = check_domain("x@" + sender, "razorpay.com")
    assert (d.status, d.kind) == ("lookalike", kind)


def test_swapped_characters():
    assert check_domain("a@dunz0.com", "dunzo.com").kind == "swapped_characters"
    assert check_domain("a@rnoney.com", "money.com").kind == "swapped_characters"


@pytest.mark.parametrize("sender,status,kind", [
    ("a@razorpay.com", "pass", "exact"), ("a@in.razorpay.com", "pass", "subdomain"),
    ("a@gmail.com", "unverified", "free_email"), ("a@zerodha.com", "unverified", "unrelated_domain"),
    ("a@bitly.com", "unverified", "listed_secondary_domain"), (None, "unverified", "no_email"),
    # Team decision at integration: same name, other ending may be the company's own (dunzo.in): unverified, not fraud
    ("a@razorpay.co", "unverified", "different_ending"), ("a@razorpay.co.in", "unverified", "different_ending"),
])
def test_domain_results(sender, status, kind):
    d = check_domain(sender, "razorpay.com", ["curlec.com", "bitly.com", "razorpay.com"])
    assert (d.status, d.kind) == (status, kind)


def test_sender_in_buying_role_passes():
    s = check_sender(FakeClient(), 897127, "Asha Rao")
    assert (s.status, s.title, s.since) == ("pass", "Procurement Manager", "2023-08")


def test_sender_in_other_role_is_a_note():
    assert check_sender(FakeClient(), 897127, "Vikram Shah").status == "note"


def test_title_from_another_company_does_not_count():
    # Neel Das is "Procurement Head" elsewhere and an engineer here: same-entry check must not mix them.
    assert check_sender(FakeClient(), 897127, "Neel Das").status == "note"


def test_unknown_sender_is_unverified_not_fraud():
    s = check_sender(FakeClient(), 897127, "Nobody Here")
    assert s.status == "unverified" and "not proof of fraud" in s.detail


def test_sender_search_failure_is_unavailable():
    s = check_sender(FakeClient(fail={"/person/search"}), 897127, "Asha Rao")
    assert s.unavailable and s.status == "unverified"


def test_finance_head_prefers_cfo():
    head, _ = find_finance_head(FakeClient(), 897127)
    assert (head.name, head.rank) == ("Meera Iyer", 1)


def test_finance_head_falls_back_to_a_founder():
    head, _ = find_finance_head(FakeClient(), 5721)
    assert head.fallback and head.title == "Co-founder & CEO"


def test_hold_rules():
    assert hold_decision(["lookalike_domain:extra_word"], True, 100_000)[0]
    assert hold_decision([], False, 500_000)[0]
    assert not hold_decision([], False, 400_000)[0]
    assert not hold_decision([], True, 9_000_000)[0]


def _run(n):
    client = FakeClient()
    req = parse_request(CASES[n]["text"], case_id=f"c{n}").request
    res = resolve_company(client, req)
    return req, res, verify_buyer(client, req, res, case_date="2026-11-06")


def test_case1_buyer_verified():
    _, _, v = _run(1)
    assert v.buyer_verified and not v.hold and v.verification_email is None


def test_case3_fraud_held_and_verified_out_of_band():
    req, _, v = _run(3)
    assert v.hold and v.fraud_flags == ["lookalike_domain:extra_word"]
    assert v.finance_head.name == "Meera Iyer"
    mail = v.verification_email
    assert mail["key"] == "c3:verify:v1" and "Meera Iyer" in mail["to_intended"]
    assert req.sender_email not in mail["to_intended"]          # never reply to the sender
    assert "have not replied to the sender" in mail["body"]
    assert any("impersonation" in n for n in v.notes)          # the fake used a real employee's name


def test_case7_free_email_under_threshold_is_not_held():
    _, res, v = _run(7)
    assert res.status == "not_found" and not v.buyer_verified and not v.hold


def test_evidence_rows_have_sources_and_dates():
    _, _, v = _run(3)
    for e in v.evidence:
        assert e.source and e.as_of == "2026-11-06" and e.id.startswith("c3:")


def test_verification_answers():
    ok = apply_verification_answer("c3", "confirm", "reviewer", "razorpay-payments.co")
    assert ok["state"] == "VERIFIED" and ok["confirmed_domain"] == "razorpay-payments.co"
    no = apply_verification_answer("c3", "deny", "reviewer", "razorpay-payments.co")
    assert no["state"] == "DECLINED" and no["fraud_attempt"]["sender_domain"] == "razorpay-payments.co"


def test_ship_to():
    regions = {"Greater Bengaluru Area": 2900, "Mumbai Metropolitan Region": 120}
    assert check_ship_to("Kolkata", "Bengaluru", regions)["status"] == "amber"
    assert check_ship_to("Bangalore", None, regions)["status"] == "match"
    assert check_ship_to(None, None, regions)["status"] == "unavailable"


def test_companys_own_listed_domain_is_not_fraud():
    """Live UI run, 6 Nov: zeptonow.com (Zepto's real domain, listed as secondary; primary zepto.com) was called a lookalike."""
    d = check_domain("ravi@zeptonow.com", "zepto.com", ["zeptonow.com", "zepto.com"])
    assert (d.status, d.kind) == ("unverified", "listed_secondary_domain")
    assert check_domain("ravi@zeptonow.com", "zepto.com", ["zepto.com"]).status == "lookalike"   # not listed: still a lookalike
