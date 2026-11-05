"""Fast tests, no network."""
import pytest

from termsdesk import store
from termsdesk.explain import citation_guard
from termsdesk.intake import parse_amount, parse_terms, rules_parse
from termsdesk.money import inr
from termsdesk.verify import domain_check


@pytest.mark.parametrize("text,expected", [
    ("₹40L of laptops", 4_000_000),
    ("40 lakh order", 4_000_000),
    ("40 lac", 4_000_000),
    ("Rs 4000000", 4_000_000),
    ("4 million", 4_000_000),
    ("4,00,000 total", 400_000),
    ("Rs. 2.5 crore", 25_000_000),
    ("18 lakh ka order hai", 1_800_000),
    ("20 laptops on 30 days", None),  # quantities and days are not money
])
def test_amounts(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("on 60 days credit", 60), ("net 45", 45), ("net-30", 30), ("30 din ke credit pe", 30),
    ("ek mahine ka credit", 30), ("2 months", 60), ("do mahine", 60), ("no terms here", None),
])
def test_terms(text, expected):
    assert parse_terms(text) == expected


def test_role_mailbox_is_not_a_person():
    r = rules_parse("t", "Need 50 laptops, Rs 20 lakh. procurement@dunzo.com")
    assert r.sender_name is None and r.company_domain == "dunzo.com" and r.amount_inr == 2_000_000


def test_free_mail_is_not_a_company_domain():
    r = rules_parse("t", "Rs 5 lakh on 30 days, ravi.kumar@gmail.com")
    assert r.company_domain is None and r.sender_name == "Ravi Kumar"


@pytest.mark.parametrize("sender,result", [
    ("razorpay.com", "exact"),
    ("payments.razorpay.com", "exact"),
    ("rzorpay.com", "lookalike"),            # a letter dropped
    ("razorpay-payments.co", "lookalike"),   # real name wrapped in extra words
    ("razorpay.co", "alt_domain"),           # same name, other ending: maybe theirs -> unverified, not fraud
    ("razorpay.co.in", "alt_domain"),
    ("razorpav.com", "lookalike"),           # y -> v
    ("gmail.com", "free_mail"),
    ("dunzo.in", "alt_domain"),              # Dunzo's real second domain must not be called fraud
    ("acme-traders.com", "unrelated"),
])
def test_domain_check(sender, result):
    primary = "dunzo.com" if sender == "dunzo.in" else "razorpay.com"
    assert domain_check(sender, primary)[0] == result


def test_homoglyph_lookalike():
    assert domain_check("rnicrosoft.com", "microsoft.com")[0] == "lookalike"


def test_money_format():
    assert inr(1_800_000) == "Rs 18 lakh"
    assert inr(15_000_000) == "Rs 1.5 crore"
    assert inr(123_456) == "Rs 1,23,456"
    assert inr(None) == "not stated"


def test_citation_guard_rejects_invented_numbers():
    source = "We offer Rs 18 lakh on 30 days. 829 joiners since Nov 2025."
    assert citation_guard("You get Rs 18 lakh on 30-day terms; 829 people joined since Nov 2025.", source) == []
    assert citation_guard("You get Rs 25 lakh on 30 days.", source) == ["25"]


def test_outbox_sends_once():
    key = "ctest:decision:v1"
    assert store.outbox_enqueue(key, "ctest", "decision", "a@b.com", "sandbox", "s", "b") is True
    assert store.outbox_enqueue(key, "ctest", "decision", "a@b.com", "sandbox", "s", "b") is False
    store.outbox_mark_sent(key)
    store.outbox_mark_sent(key)
    rows = [o for o in store.list_outbox("ctest") if o["key"] == key]
    assert len(rows) == 1 and rows[0]["status"] == "sent"
