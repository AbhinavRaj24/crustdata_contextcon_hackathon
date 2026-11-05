from fake_api import FakeClient
from termsdesk.contracts import Request
from termsdesk.resolve import core_name, merge_aliases, resolve_company


def R(**kw):
    kw.setdefault("case_id", "t")
    return Request(**kw)


def test_core_name():
    assert core_name("Razorpay Software Pvt Ltd") == "razorpay"
    assert core_name("DUNZO DIGITAL PRIVATE LIMITED") == "dunzo"
    assert core_name("Kanpur Leather Exports") == "kanpur leather exports"


def test_exact_domain_wins():
    res = resolve_company(FakeClient(), R(company_domain="razorpay.com", amount_inr=3_000_000))
    assert (res.status, res.company_id, res.match) == ("resolved", 897127, "domain_exact")
    assert res.exposure_inr == 3_000_000
    assert {e.signal for e in res.evidence} == {"company_identity", "exposure"}


def test_legal_name_merges_into_one_company_and_sums_exposure():
    lookup = {897127: {"aliases": ["Razorpay"], "outstanding_inr": 1_000_000}}.get
    res = resolve_company(FakeClient(), R(company_name="Razorpay Software Pvt Ltd", amount_inr=1_200_000), lookup)
    assert (res.company_id, res.match) == (897127, "name_exact")
    assert res.exposure_inr == 2_200_000 and res.outstanding_inr == 1_000_000
    assert res.aliases == ["Razorpay", "Razorpay Software Pvt Ltd"]


def test_duplicate_names_pick_the_much_larger_company():
    res = resolve_company(FakeClient(), R(company_name="Dunzo"))
    assert (res.company_id, res.match) == (5721, "name_largest")


def test_close_candidates_go_to_a_human():
    res = resolve_company(FakeClient(), R(company_name="Nova Foods"))
    assert res.status == "ambiguous" and res.needs_human and "too close to call" in res.reason


def test_no_confident_match():
    res = resolve_company(FakeClient(), R(company_name="Kanpur Leather Exports", sender_email="ramesh.kle@gmail.com"))
    assert res.status == "not_found" and res.needs_human
    assert res.evidence[0].status == "unavailable"


def test_lookalike_domain_falls_back_to_the_claimed_name():
    res = resolve_company(FakeClient(), R(company_name="Razorpay", company_domain="razorpay-payments.co",
                                          sender_email="rahul.mehta@razorpay-payments.co"))
    assert (res.company_id, res.primary_domain) == (897127, "razorpay.com")


def test_secondary_domain_is_not_trusted():
    res = resolve_company(FakeClient(), R(company_domain="bitly.com"))
    assert res.company_id is None and any("secondary domain" in n for n in res.notes)


def test_small_namesake_owning_the_domain_is_flagged():
    res = resolve_company(FakeClient(), R(company_name="Dunzo", company_domain="getdunzo.com"))
    assert res.company_id == 1735338 and "name_collision" in res.flags


def test_name_and_domain_disagree():
    res = resolve_company(FakeClient(), R(company_name="Dunzo", company_domain="razorpay.com"))
    assert res.company_id == 897127 and "name_domain_mismatch" in res.flags


def test_identify_down_is_unavailable_not_a_guess():
    res = resolve_company(FakeClient(fail={"/company/identify"}), R(company_domain="razorpay.com"))
    assert res.status == "unavailable" and res.needs_human


def test_merge_aliases_dedupes():
    assert merge_aliases(["Razorpay"], "razorpay ", "Razorpay Software Pvt Ltd") == \
        ["Razorpay", "Razorpay Software Pvt Ltd"]
