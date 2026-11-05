import datetime as dt

import pytest

from fakes import (HEALTHY, HEALTHY_ID, ZOMBIE, ZOMBIE_ID, FakeClient, OutOfCredits, Unavailable, entry, healthy_world,
                   monthly, person, zombie_world)
from termsdesk import decide, signals, store
from termsdesk.contracts import Company, Request, VerifyResult
from termsdesk.signals import SIGNALS, Ctx, _month_start, is_clevel, is_finance, is_finance_head, mentions

H = Company(crustdata_id=HEALTHY_ID, name=HEALTHY, primary_domain="healthy.example")
Z = Company(crustdata_id=ZOMBIE_ID, name=ZOMBIE, primary_domain="zombie.example")


def levels(results):
    return {r.signal: r.level for r in results}


def ev(case_id, signal):
    return [e for e in store.list_evidence(case_id) if e.signal == signal]


# ---------- pure helpers ----------

def test_month_start():
    assert _month_start(dt.date(2026, 11, 6), 12) == dt.date(2025, 11, 1)
    assert _month_start(dt.date(2026, 1, 31), 1) == dt.date(2025, 12, 1)
    assert _month_start(dt.date(2026, 3, 15), 26) == dt.date(2024, 1, 1)


@pytest.mark.parametrize("title,fin,head", [
    ("Accounts Executive", True, False), ("Account Executive", False, False), ("Key Accounts Manager", False, False),
    ("Chartered Accountant", True, False), ("Head of Core Finance", True, True), ("CFO", True, True),
    ("VP - Finance", True, True), ("Senior Director Finance", True, False), ("Associate, Office of the CFO", True, False),
    ("Deputy CFO", True, False), ("Strategic Accounts Lead", False, False),
])
def test_finance_titles(title, fin, head):
    assert is_finance(title) is fin and is_finance_head(title) is head


@pytest.mark.parametrize("title,clevel", [
    ("Co-founder & CTO", True), ("Chief Business Officer", True), ("Associate - Founder's Office", False),
    ("Chief of Staff", False), ("Founding Engineer", False), ("EA to the CEO", False), ("Founder in Residence", False),
])
def test_clevel_titles(title, clevel):
    assert is_clevel(title) is clevel


def test_whole_word_mentions():
    assert not mentions("CRED", "Credit card defaults rise; incredible losses")
    assert mentions("Byju's", "BYJU'S delays salaries again")
    assert mentions("Bira 91", "Bira 91 layoffs") and not mentions("Bira 91", "Bira beer")


# ---------- headcount ----------

def _ctx_with_series(series, as_of=None):
    ctx = Ctx("hc", H, as_of)
    ctx.enriched = {"headcount": {"timeseries": series}}
    return ctx


def test_headcount_maths():
    ctx = _ctx_with_series(monthly(dt.date(2025, 1, 1), [1000] * 12 + [900, 850, 800, 760, 740, 720, 700, 700, 700, 700]))
    signals.s_headcount(ctx)
    v = ev("hc", "headcount_trend")[0].value
    assert v["latest"] == 700 and v["six_months_ago"] == 760 and round(v["drawdown_24m"], 2) == 0.30
    assert ctx.results[-1].level == "red"


def test_one_bad_point_does_not_make_a_fake_peak():
    ctx = _ctx_with_series(monthly(dt.date(2025, 1, 1), [500] * 10 + [5000] + [500] * 11))
    signals.s_headcount(ctx)
    assert ctx.results[-1].level == "ok" and ev("hc", "headcount_trend")[0].value["peak_24m"] == 500


def test_small_team_noise_is_ignored():
    ctx = _ctx_with_series(monthly(dt.date(2025, 1, 1), [40] * 14 + [36, 35, 34, 34, 34, 34, 34, 34]))
    signals.s_headcount(ctx)
    assert ctx.results[-1].level == "ok"          # -15% but only 6 people


def test_negative_and_future_points_are_invalid():
    series = monthly(dt.date(2025, 1, 1), [100] * 20) + [{"date": "2025-09-15", "employee_count": -500},
                                                          {"date": "2031-01-01", "employee_count": 120}]
    ctx = _ctx_with_series(series)
    signals.s_headcount(ctx)
    statuses = [e.status for e in ev("hc", "headcount_trend")]
    assert "invalid" in statuses and ctx.results[-1].level == "ok"


def test_point_in_time_hides_later_points_without_calling_them_invalid():
    ctx = _ctx_with_series(monthly(dt.date(2023, 1, 1), [100 + i for i in range(40)]), as_of=dt.date(2024, 6, 15))
    signals.s_headcount(ctx)
    rows = ev("hc", "headcount_trend")
    assert all(e.status != "invalid" for e in rows) and rows[0].value["latest_date"] == "2024-06-01"


# ---------- exits ----------

def test_exits_skip_promotions_sales_titles_and_founders_office(use_client):
    use_client(healthy_world())
    ctx = Ctx("ex", H)
    ctx.profiles = 306
    signals.s_exits(ctx)
    lv = {r.signal: r for r in ctx.results}
    fin = ev("ex", "finance_team_exits")[0].value
    assert fin["left"] == 2 and fin["internal_moves_skipped"] == 1          # real leaver + senior director, not the promotion
    assert lv["finance_head_exit"].level == "ok"                           # "Senior Director Finance" is not the head
    assert lv["finance_team_exits"].level == "ok"                          # 2 of ~22
    assert lv["clevel_exits"].level == "ok"                                # founder's office / chief of staff don't count
    assert "Sales Leaver" not in str(fin["people"])


def test_exits_point_in_time_respects_the_reporting_lag(use_client):
    use_client(zombie_world())
    early = Ctx("pit-oct", Z, dt.date(2023, 10, 1))
    early.profiles = 2833
    signals.s_exits(early)
    late = Ctx("pit-dec", Z, dt.date(2023, 12, 1))
    late.profiles = 2594
    signals.s_exits(late)
    e, l = levels(early.results), levels(late.results)
    assert e["finance_head_exit"] == "ok"       # left Sep 2023: not yet knowable on 1 Oct (60-day lag)
    assert l["finance_head_exit"] == "red"      # knowable by 1 Dec
    assert e["finance_team_exits"] == "red"     # 6 of ~10 finance staff left Mar-Jul 2023


def test_large_companies_need_more_clevel_exits(use_client):
    world = use_client(healthy_world())
    for i in range(2):
        world.people.append(person(f"CXO {i}", [entry(980 + i, "Other", "CEO", "2026-09-01")],
                                   [entry(HEALTHY_ID, HEALTHY, "Chief Product Officer", "2020-01-01", "2026-08-01")]))
    small = Ctx("small", H)
    small.profiles = 300
    signals.s_exits(small)
    big = Ctx("big", H)
    big.profiles = 4000
    signals.s_exits(big)
    assert levels(small.results)["clevel_exits"] == "red" and levels(big.results)["clevel_exits"] == "amber"


# ---------- job posts, website, news ----------

@pytest.mark.parametrize("joiners,expected", [("ok", "ok"), ("amber", "amber"), ("red", "red")])
def test_zero_job_posts_needs_weak_joiners(use_client, joiners, expected):
    use_client(healthy_world())
    ctx = Ctx("jp", H)
    ctx.profiles, ctx.joiner_level = 300, joiners
    signals.s_job_posts(ctx)
    assert ctx.results[-1].level == expected


def test_no_job_post_history_is_could_not_verify(use_client):
    use_client(healthy_world(jobs=[]))
    ctx = Ctx("jp0", H)
    ctx.profiles, ctx.joiner_level = 300, "red"
    signals.s_job_posts(ctx)
    assert ctx.results[-1].level == "na" and ev("jp0", "job_posts")[0].status == "unavailable"


def test_website_bot_block_is_ok_and_504_is_amber(use_client):
    use_client(healthy_world())
    ok = Ctx("w1", H)
    signals.s_website(ok)
    use_client(zombie_world())
    bad = Ctx("w2", Z)
    signals.s_website(bad)
    assert ok.results[-1].level == "ok" and bad.results[-1].level == "amber"


def test_website_is_skipped_in_the_past(use_client):
    fake = use_client(zombie_world())
    ctx = Ctx("w3", Z, dt.date(2023, 10, 1))
    signals.s_website(ctx)
    assert ctx.results == [] and not any(c[0] == "web_fetch" for c in fake.calls)


def test_news_point_in_time_window_and_whole_word_match(use_client):
    fake = use_client(zombie_world())
    ctx = Ctx("n1", Z, dt.date(2023, 10, 1))
    signals.s_news(ctx)
    call = [c for c in fake.calls if c[0] == "web_search"][0]
    assert call[3] is not None and call[3] > call[2]                      # end_date set, after start_date
    assert ctx.results[-1].level == "amber"                               # keywords only (no model in tests)
    cred = FakeClient(companies={}, people=[], jobs=[],
                      news=[{"title": "Credit card dues pile up", "snippet": "unpaid credit", "url": "https://x.example"}])
    use_client(cred)
    ctx2 = Ctx("n2", Company(crustdata_id=1, name="CRED"))
    signals.s_news(ctx2)
    assert ctx2.results[-1].level == "ok"


# ---------- whole collection ----------

def test_collect_healthy_is_clean_and_approved(use_client):
    use_client(healthy_world())
    res = signals.collect("h1", H)
    lv = levels(res)
    assert [r.signal for r in res] == sorted((r.signal for r in res), key=SIGNALS.index)
    assert not [s for s, l in lv.items() if l in ("red", "amber")], lv
    req = Request(case_id="h1", raw_text="x", amount_inr=3_000_000, terms_days=45)
    d = decide.decide(req, H, VerifyResult(status="pass"), res, None, case_id="h1")
    assert (d.decision, d.limit_inr, d.terms_days, d.rule) == ("approve", 3_000_000, 45, "R8 clean")


def test_collect_zombie_is_advance_only(use_client):
    use_client(zombie_world())
    res = signals.collect("z1", Z)
    lv = levels(res)
    assert lv["joiner_rate"] == "red" and lv["job_posts"] == "red" and lv["website"] == "amber"
    conflict = [e for e in ev("z1", "headcount_trend") if e.status == "conflict"]
    assert conflict                                                       # API +25% yoy vs a flat series
    req = Request(case_id="z1", raw_text="x", amount_inr=2_000_000, terms_days=60)
    d = decide.decide(req, Z, VerifyResult(status="pass"), res, None, case_id="z1")
    assert d.decision == "advance_only" and d.rule.startswith("R3")


def test_parallel_and_sequential_give_the_same_answer(use_client):
    use_client(zombie_world())
    a = signals.collect("p1", Z, parallel=True)
    b = signals.collect("p2", Z, parallel=False)
    assert [(r.signal, r.level, r.summary) for r in a] == [(r.signal, r.level, r.summary) for r in b]
    assert [i.split("-", 1)[1] for r in a for i in r.evidence_ids] == [i.split("-", 1)[1] for r in b for i in r.evidence_ids]


def test_collect_as_of_flags_the_zombie_in_2023(use_client):
    fake = use_client(zombie_world())
    res = signals.collect("bt-2023-10", Z, as_of=dt.date(2023, 10, 1))
    lv = levels(res)
    assert "website" not in lv
    assert lv["headcount_trend"] == "amber"                               # -17% in 6 months, ~19% below peak
    assert lv["finance_team_exits"] == "red"
    assert ev("bt-2023-10", "profiles")[0].value["by_company_id"] == 2750  # size at T = the Oct 2023 point
    funding = ev("bt-2023-10", "funding_age")[0].value
    assert funding["last_fundraise_date"] == "2023-04-06" and funding["months"] == 6
    job_calls = [c for c in fake.calls if c[0] == "job_search"]
    assert any("=<" in str(c[1]) for c in job_calls)                     # posts bounded by T
    earlier = signals.collect("bt-2022-12", Z, as_of=dt.date(2022, 12, 15))
    assert ev("bt-2022-12", "funding_age")[0].value["round"] == "series_e"  # Series F was not announced yet


def test_false_zombie_from_an_id_mismatch_is_not_trusted(use_client):
    rid, name = 303, "Renamedco"
    people = [person(f"Old {i}", [entry(rid, name, "Engineer", "2022-01-01")]) for i in range(100)]
    people += [person(f"New {i}", [entry(999, name, "Engineer", "2026-03-01")]) for i in range(30)]   # new page ID
    use_client(FakeClient(companies={rid: {"basic_info": {}, "headcount": {}, "funding": {}}}, people=people, jobs=[]))
    ctx = Ctx("rn", Company(crustdata_id=rid, name=name))
    signals.s_profiles_and_joiners(ctx)
    jr = [r for r in ctx.results if r.signal == "joiner_rate"][0]
    assert jr.level == "amber" and ev("rn", "joiner_rate")[0].status == "conflict"


def test_failed_enrich_becomes_could_not_verify(use_client):
    use_client(healthy_world(fail={"enrich": Unavailable("enrich: ReadTimeout after 3 attempts")}))
    lv = levels(signals.collect("f1", H))
    assert lv["headcount_trend"] == "na" and lv["funding_age"] == "na" and lv["joiner_rate"] == "ok"


def test_out_of_credits_stops_the_case(use_client):
    use_client(healthy_world(fail={"person_search": OutOfCredits("credits exhausted")}))
    with pytest.raises(OutOfCredits):
        signals.collect("f2", H)


def test_two_argument_call_still_works(use_client):
    use_client(healthy_world())
    assert signals.collect("compat", H)


def test_regional_ceo_titles_are_not_c_level():
    """Zomato false alarm (live calibration, 6 Nov): City/Cluster CEOs and City COOs counted as C-level exits."""
    for t in ["Cluster Ceo", "City CEO Madhya Pradesh and Chhattisgarh", "CEO - Andhra cluster", "City COO - Supply", "Regional CTO"]:
        assert not signals.is_clevel(t), t
    for t in ["CEO", "Chief Operating Officer", "Co-founder & CTO"]:
        assert signals.is_clevel(t), t
