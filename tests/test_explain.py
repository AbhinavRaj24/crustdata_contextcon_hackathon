import pytest

from termsdesk.contracts import Decision, Evidence, Reason, Request
from termsdesk.explain import (build_facts, citation_guard, could_not_verify, explain, format_date, format_inr,
                               letter_key, _payload)
from termsdesk.intake import normalise_amount


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    monkeypatch.setenv("TERMSDESK_USE_MODEL", "0")


REQ = Request(case_id="c42", company_name="Razorpay", sender_name="Asha Rao", sender_email="asha.rao@razorpay.com",
              amount_inr=4_000_000, terms_days=60, goods="40 laptops")
EV = [
    Evidence(id="c42:joiner_rate", case_id="c42", signal="joiner_rate", value=0.03, unit="ratio",
             source="person/search current.company_id=897127", as_of="2026-11-06"),
    Evidence(id="c42:profiles", case_id="c42", signal="profiles", value=310, source="person/search", as_of="2026-11-06"),
    Evidence(id="c42:finance_head_exit", case_id="c42", signal="finance_head_exit",
             value={"title": "Head of Finance", "end_date": "2026-08-01"}, source="person/search past.company_id=897127",
             as_of="2026-08-01"),
    Evidence(id="c42:distress_news", case_id="c42", signal="distress_news", value={"error": "web search timed out"},
             source="web/search/live", as_of="2026-11-06", status="unavailable"),
    Evidence(id="c42:sender_check", case_id="c42", signal="sender_check",
             value={"result": "pass", "name": "Asha Rao", "title": "Procurement Manager", "since": "2023-08"},
             source="person/search current.company_id=897127 + basic_profile.name", as_of="2026-11-06"),
]
REASONS = [Reason(text="Head of Finance left in Aug 2026", evidence_ids=["c42:finance_head_exit"]),
           Reason(text="Joiners in the last 12 months: 3% of 310 profiles",
                  evidence_ids=["c42:joiner_rate", "c42:profiles"])]


def D(kind, **kw):
    base = dict(decision=kind, limit_inr=1_800_000, terms_days=30, advance_pct=None, reasons=REASONS,
                exposure_inr=4_000_000, policy_version="v2")
    base.update(kw)
    return Decision(**base)


@pytest.mark.parametrize("amount", [1_800_000, 15_000_000, 125_000, 75_000, 2_500_000, 100_000])
def test_inr_round_trip(amount):
    assert normalise_amount(format_inr(amount)) == amount


def test_format_date():
    assert format_date("2026-11-06") == "6 Nov 2026" and format_date("2023-09") == "Sep 2023"


@pytest.mark.parametrize("kind,extra", [("approve", {}), ("reduce", {}), ("advance_only", {"advance_pct": 100}),
                                        ("decline", {}), ("refer", {})])
def test_template_letters_pass_the_guard(kind, extra):
    out = explain(D(kind, **extra), EV, REQ, "Razorpay", case_id="c42", version=2)
    assert out.letter is not None and out.letter.method == "template"
    assert out.guard_log[-1]["ok"], out.guard_log[-1]["rejected"]


def test_reduce_letter_content():
    out = explain(D("reduce"), EV, REQ, "Razorpay", case_id="c42", version=2)
    assert "₹18 lakh on 30-day terms" in out.letter.body and "₹40 lakh on 60 days" in out.letter.body
    assert "Could not verify: distress news (web search timed out)" in out.letter.body
    assert out.letter.key == "c42:terms:v2" and out.letter.to_intended == "asha.rao@razorpay.com"


def test_note_has_sources_and_gaps():
    ev = EV + [Evidence(id="c42:domain_check", case_id="c42", signal="domain_check",
                        value={"result": "pass", "sender_domain": "razorpay.com", "primary_domain": "razorpay.com"},
                        source="company/identify #897127 basic_info.primary_domain", as_of="2026-11-06")]
    note = explain(D("reduce"), ev, REQ, "Razorpay", case_id="c42", version=2, credits=9.4).note
    assert note.headline == "Reduce to ₹18 lakh on 30 days (requested ₹40 lakh on 60 days)"
    assert note.reasons[0]["display"] == "Head of Finance left in Aug 2026 (Person Search, 1 Aug 2026)"
    assert any(r["text"].startswith("Buyer verified: Procurement Manager") for r in note.reasons)
    assert note.could_not_verify == ["Could not verify: distress news (web search timed out)"]
    assert note.footer == "Policy v2 · 9.4 credits · letter key c42:terms:v2"


def test_hold_has_no_buyer_letter():
    out = explain(D("hold_verify", limit_inr=None, terms_days=None), EV, REQ, "Razorpay", case_id="c42")
    assert out.letter is None and out.note.headline.startswith("Hold")


@pytest.mark.parametrize("bad", ["₹25 lakh", "on 15 March 2026", "4 finance staff left", "Mr. Sharma approved it",
                                 "12% of profiles", "seven people left"])
def test_guard_rejects_invented_facts(bad):
    facts = build_facts(_payload(D("reduce"), EV, REQ, "Razorpay"))
    assert not citation_guard(f"Dear Asha,\n\nWe can offer ₹18 lakh on 30-day terms. Note: {bad}.", facts).ok


def test_guard_accepts_facts_from_evidence():
    facts = build_facts(_payload(D("reduce"), EV, REQ, "Razorpay"))
    text = ("Dear Asha,\n\nWe can offer Razorpay ₹18 lakh on 30-day terms (you asked for ₹40 lakh on 60 days). "
            "Joiners in the last 12 months were 3% of 310 profiles, and the Head of Finance left in Aug 2026.")
    assert citation_guard(text, facts).ok


def test_could_not_verify_lines():
    assert could_not_verify([{"signal": "website", "status": "unavailable", "value": None}]) == \
        ["Could not verify: website"]


def test_letter_key():
    assert letter_key("c42", 2) == "c42:terms:v2" and letter_key("c42", "v3") == "c42:terms:v3"


def test_real_name_on_a_fake_domain_is_not_called_verified():
    ev = EV + [Evidence(id="c42:domain_check", case_id="c42", signal="domain_check",
                        value={"result": "lookalike", "sender_domain": "razorpay-payments.co",
                               "primary_domain": "razorpay.com"}, source="company/identify", as_of="2026-11-06")]
    note = explain(D("hold_verify", limit_inr=None, terms_days=None), ev, REQ, "Razorpay", case_id="c42").note
    texts = [r["text"] for r in note.reasons]
    assert not any(t.startswith("Buyer verified") for t in texts)
    assert any("email domain is not the company's" in t for t in texts)
