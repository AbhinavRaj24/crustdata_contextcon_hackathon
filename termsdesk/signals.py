"""The ten credit signals. Each one saves its evidence (with source and as-of date) and returns a level.

We compute trends ourselves from raw data; summary fields (growth %, people, revenue) are never trusted.
A failed call never stops the case: the signal becomes 'na' and the letter lists it under "Could not verify".

Point in time: collect(..., as_of=T) answers "what could we have known on date T?" for the backtest.
Every step then uses only data dated on or before T (see the table in the B handoff).
Speed: independent steps run in parallel; results are re-sorted into SIGNALS order, and evidence ids are
per signal, so the same case always gives the same results in the same order.
"""
from __future__ import annotations

import concurrent.futures as cf
import contextvars
import datetime as dt
import os
import re
import statistics
import threading
from typing import Callable, Optional

import yaml

from . import config, llm, store
from .contracts import Company, Evidence, SignalResult
from .crustdata import BadRequest, Forbidden, Unavailable, all_of, any_of, client, cond

SIGNALS = ["profiles", "joiner_rate", "headcount_trend", "finance_head_exit", "finance_team_exits", "clevel_exits",
           "funding_age", "company_age", "job_posts", "website", "distress_news"]

CUR = "experience.employment_details.current"
PAST = "experience.employment_details.past"

# Finance staff. Plural "accounts" is finance in Indian titles ("Accounts Executive"); singular "account" + a
# sales word is sales ("Account Executive", "Key Accounts Manager"), never finance.
FINANCE = re.compile(r"financ|\bcfo\b|controllership|(?:financial|finance)\s+controller|accountant|accounting|"
                     r"\baccounts\b|treasur|\bfp\s*&\s*a\b", re.IGNORECASE)
NOT_FINANCE = re.compile(r"\b(?:key|strategic|enterprise|global|named|major|national|large|corporate|regional|"
                         r"mid[-\s]?market)\s+accounts?\b|\baccount\s+(?:executive|manager|management|director|lead|"
                         r"strategist|planner)\b", re.IGNORECASE)
FINANCE_HEAD = re.compile(r"\bcfo\b|chief\s+financial|head\s*(?:of|[-–,:])\s*(?:core\s+)?financ|financ\w*\s+head|"
                          r"\bvp\b[,\s-]*(?:of\s+)?financ|vice\s+president[,\s-]*(?:of\s+)?financ", re.IGNORECASE)
NOT_HEAD = re.compile(r"office\s+of\s+(?:the\s+)?cfo|cfo'?s?\s+office|\bto\s+(?:the\s+)?cfo|assistant|associate|"
                      r"analyst|intern\b|deputy|chief\s+of\s+staff|\bea\b", re.IGNORECASE)
CLEVEL = re.compile(r"\b(?:ceo|coo|cto|cbo|cro|cmo|cpo)\b|chief\s+\w+\s+officer|\bco-?founder\b|\bfounder\b",
                    re.IGNORECASE)
NOT_CLEVEL = re.compile(r"founder'?s?\s+office|office\s+of\s+(?:the\s+)?(?:founders?|ceo|cto|coo|cfo)|chief\s+of\s+staff|"
                        r"(?:entrepreneur|founder)\s+in\s+residence|\bto\s+(?:the\s+)?(?:ceo|cto|coo|founders?)\b|"
                        r"assistant|associate|analyst|intern\b|\bea\b|"
                        # regional heads are not company leadership: "City CEO", "Cluster CEO", "CEO - Andhra cluster" (Zomato)
                        r"\b(?:city|cluster|zonal|zone|regional|region|area|state|district|circle|branch|store|hub|country|"
                        r"territory|market|vertical|division|unit)\b", re.IGNORECASE)
JUNK_NEWS = re.compile(r"tracxn|crunchbase|zoominfo|cbinsights|pitchbook|owler|glassdoor|ambitionbox|linkedin|markhub",
                       re.IGNORECASE)
BOT_BLOCK_CODES = {401, 403, 405, 406, 429}   # the site answered; it just refuses automated readers


def policy() -> dict:
    return yaml.safe_load(config.POLICY_PATH.read_text())


def _today() -> dt.date:
    return dt.date.today()


def _month_start(d: dt.date, months_back: int) -> dt.date:
    y, m = d.year, d.month - months_back
    while m <= 0:
        m += 12
        y -= 1
    return dt.date(y, m, 1)


def _date(s: Optional[str]) -> Optional[dt.date]:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def is_finance(title: Optional[str]) -> bool:
    t = title or ""
    return bool(FINANCE.search(t)) and not NOT_FINANCE.search(t)


def is_finance_head(title: Optional[str]) -> bool:
    t = title or ""
    return bool(FINANCE_HEAD.search(t)) and not NOT_HEAD.search(t)


def is_clevel(title: Optional[str]) -> bool:
    t = title or ""
    return bool(CLEVEL.search(t)) and not NOT_CLEVEL.search(t)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower().replace("'", "").replace("’", ""))


def mentions(name: str, text: str) -> bool:
    """Whole-word mention: 'CRED' does not match 'credit'; "Byju's" matches "BYJU'S"."""
    want, have = _tokens(name), _tokens(text)
    if not want:
        return False
    n = len(want)
    return any(have[i:i + n] == want for i in range(len(have) - n + 1))


class Ctx:
    """What one collection run shares between signals."""

    def __init__(self, case_id: str, company: Company, as_of: Optional[dt.date] = None):
        self.case_id, self.company = case_id, company
        pol = policy()
        self.pol = pol["signals"]
        self.defaults = pol["defaults"]
        self.real_today = _today()
        self.as_of = as_of
        self.today = as_of or self.real_today          # "today" for every window; the backtest's month T
        self.pit = as_of is not None and as_of < self.real_today
        self.enriched: Optional[dict] = None
        self.profiles: Optional[int] = None
        self.change_6m: Optional[float] = None
        self.joiner_level: str = "na"
        self.enrich_failed = False
        self.results: list[SignalResult] = []
        self._lock = threading.Lock()
        self._seq: dict[str, int] = {}

    def ev(self, signal: str, value, source: str, *, status: str = "verified", as_of: Optional[str] = None,
           url: Optional[str] = None, unit: Optional[str] = None, note: Optional[str] = None) -> str:
        with self._lock:                                # ids per signal: safe in parallel, stable across runs
            n = self._seq[signal] = self._seq.get(signal, 0) + 1
        e = store.add_evidence(Evidence(id=f"{self.case_id}-{signal}-{n}", case_id=self.case_id, signal=signal,
                                        value=value, source=source, status=status,
                                        as_of=as_of or self.today.isoformat(), url=url, unit=unit, note=note))
        return e.id

    def add(self, signal: str, level: str, summary: str, ids: list[str]) -> None:
        with self._lock:
            self.results.append(SignalResult(signal=signal, level=level, summary=summary, evidence_ids=ids))

    def unavailable(self, signal: str, what: str, err: Exception) -> None:
        eid = self.ev(signal, None, what, status="unavailable", note=str(err)[:200])
        self.add(signal, "na", f"Could not verify {signal.replace('_', ' ')}: {what} failed", [eid])


# ---------- headcount series helpers ----------

def _parse_series(ctx: Ctx) -> tuple[list[tuple[dt.date, int]], list[dict]]:
    """(points visible on ctx.today, invalid points). Invalid = negative or after the real today.
    Points after a backtest's T are not invalid, just not known yet, so they are left out silently."""
    hc = (ctx.enriched or {}).get("headcount") or {}
    series, invalid = [], []
    for p in hc.get("timeseries") or []:
        d, n = _date(p.get("date")), p.get("employee_count")
        if d is None or n is None:
            continue
        if n < 0 or d > ctx.real_today:
            invalid.append(p)
            continue
        if d <= ctx.today:
            series.append((d, int(n)))
    series.sort()
    return series, invalid


def _smoothed(series: list[tuple[dt.date, int]]) -> list[tuple[dt.date, int]]:
    """Rolling median of 3 points, so one bad data point cannot create a fake peak."""
    out = []
    for i, (d, _) in enumerate(series):
        window = [n for _, n in series[max(0, i - 1): i + 2]]
        out.append((d, int(statistics.median(window))))
    return out


def _value_at(series: list[tuple[dt.date, int]], when: dt.date, max_gap_days: int) -> Optional[tuple[dt.date, int]]:
    known = [x for x in series if x[0] <= when]
    if not known or (when - known[-1][0]).days > max_gap_days:
        return None
    return known[-1]


# ---------- individual signals ----------

def s_enrich(ctx: Ctx) -> None:
    ctx.enriched = client().enrich(ctx.company.crustdata_id, ["basic_info", "headcount", "funding"])


def _joiner_level(ctx: Ctx, joiners: int, profiles: int, since: dt.date) -> tuple[str, str]:
    p = ctx.pol["joiner_rate"]
    rate = joiners / profiles if profiles else 0.0
    if profiles < ctx.defaults["min_profiles"]:
        return "na", f"Too few profiles ({profiles}) to judge hiring"
    if rate < p["red_below"]:
        return "red", f"{joiners} joiners since {since:%b %Y} out of {profiles:,} profiles ({rate:.1%}): nobody is joining"
    if rate < p["amber_below"]:
        return "amber", f"Only {joiners} joiners since {since:%b %Y} out of {profiles:,} profiles ({rate:.1%})"
    return "ok", f"{joiners} joiners since {since:%b %Y} out of {profiles:,} profiles ({rate:.1%})"


def s_profiles_and_joiners(ctx: Ctx) -> None:
    if ctx.pit:
        return _profiles_and_joiners_at(ctx)
    cid, name = ctx.company.crustdata_id, ctx.company.name
    c = client()
    by_id = c.count_people(cond(f"{CUR}.company_id", "=", cid))
    try:
        by_name = c.count_people(cond(f"{CUR}.company_name", "[.]", name))
    except Unavailable:
        by_name = None
    ctx.profiles = by_id
    gap = abs(by_id - by_name) / max(by_id, by_name, 1) if by_name is not None else 0
    status = "conflict" if gap > 0.5 else "verified"
    pid = ctx.ev("profiles", {"by_company_id": by_id, "by_exact_name": by_name}, f"person/search current.company_id={cid}",
                 status=status, unit="profiles",
                 note=None if status == "verified" else "ID and name counts disagree by more than half; using the ID count")
    ctx.add("profiles", "ok" if by_id >= ctx.defaults["min_profiles"] else "na",
            f"{by_id:,} people list {name} as their current employer", [pid])

    since = _month_start(ctx.today, 12)
    joiners = c.count_people(all_of(cond(f"{CUR}.company_id", "=", cid), cond(f"{CUR}.start_date", "=>", since.isoformat())))
    rate = joiners / by_id if by_id else 0.0
    value = {"joiners": joiners, "profiles": by_id, "rate": round(rate, 4), "since": since.isoformat()}
    level, text = _joiner_level(ctx, joiners, by_id, since)
    jstatus = "verified"
    if level == "red" and by_name:
        # A false zombie turns an honest buyer away, so confirm it by company name before trusting the ID count.
        try:
            name_joiners = c.count_people(all_of(cond(f"{CUR}.company_name", "[.]", name),
                                                 cond(f"{CUR}.start_date", "=>", since.isoformat())))
        except Unavailable:
            name_joiners = None
        value.update({"joiners_by_name": name_joiners, "profiles_by_name": by_name})
        if name_joiners is not None and name_joiners / by_name >= ctx.pol["joiner_rate"]["amber_below"]:
            level, jstatus = "amber", "conflict"
            text = (f"{joiners} joiners since {since:%b %Y} by company ID but {name_joiners} by company name, "
                    f"so the no-hiring signal is not confirmed")
    jid = ctx.ev("joiner_rate", value, f"person/search current.company_id={cid} + current.start_date=>{since}",
                 unit="share", status=jstatus)
    ctx.joiner_level = level
    ctx.add("joiner_rate", level, text, [jid])


def _profiles_and_joiners_at(ctx: Ctx) -> None:
    """Point in time: profiles = the headcount series at T; joiners = jobs at the company that started in
    the 12 months before T, whether the person is still there (current) or has left since (past)."""
    cid, name, T = ctx.company.crustdata_id, ctx.company.name, ctx.today
    series, _ = _parse_series(ctx)
    at = _value_at(series, T, ctx.pol.get("headcount_max_gap_days", 120))
    src = f"company/enrich #{cid} headcount.timeseries"
    if at is None:
        eid = ctx.ev("profiles", None, src, status="unavailable", note=f"No headcount point near {T}")
        ctx.add("profiles", "na", f"Could not verify company size in {T:%b %Y}: no headcount history then", [eid])
        jid = ctx.ev("joiner_rate", None, src, status="unavailable", note="No size to compare joiners with")
        ctx.add("joiner_rate", "na", f"Could not verify joiner rate in {T:%b %Y}: no headcount history then", [jid])
        return
    n = at[1]
    ctx.profiles = n
    pid = ctx.ev("profiles", {"by_company_id": n, "by_exact_name": None, "method": "headcount series value at T",
                              "series_date": at[0].isoformat()}, src, unit="profiles", as_of=at[0].isoformat())
    ctx.add("profiles", "ok" if n >= ctx.defaults["min_profiles"] else "na",
            f"About {n:,} people worked at {name} in {at[0]:%b %Y}", [pid])
    since, c = _month_start(T, 12), client()
    window = lambda side: all_of(cond(f"{side}.company_id", "=", cid), cond(f"{side}.start_date", "=>", since.isoformat()),
                                 cond(f"{side}.start_date", "=<", T.isoformat()))
    still_there = c.count_people(window(CUR))
    left_since = c.count_people(window(PAST))
    joiners = still_there + left_since
    level, text = _joiner_level(ctx, joiners, n, since)
    jid = ctx.ev("joiner_rate", {"joiners": joiners, "still_there": still_there, "left_since": left_since, "profiles": n,
                                 "rate": round(joiners / n, 4) if n else 0.0, "since": since.isoformat(), "until": T.isoformat()},
                 f"person/search current/past.company_id={cid} + start_date in {since}..{T}", unit="share")
    ctx.joiner_level = level
    ctx.add("joiner_rate", level, text, [jid])


def s_headcount(ctx: Ctx) -> None:
    if ctx.enrich_failed:
        return  # already recorded as could-not-verify
    hc = (ctx.enriched or {}).get("headcount") or {}
    series, invalid = _parse_series(ctx)
    src = f"company/enrich #{ctx.company.crustdata_id} headcount.timeseries"
    if invalid:
        ctx.ev("headcount_trend", invalid[:3], src, status="invalid", note="Negative or future headcount points ignored")
    if ctx.pit and series and (ctx.today - series[-1][0]).days > ctx.pol.get("headcount_max_gap_days", 120):
        series = []
    if len(series) < 2:
        eid = ctx.ev("headcount_trend", None, src, status="unavailable", note="No usable headcount history")
        ctx.add("headcount_trend", "na", "Could not verify headcount trend: no usable history", [eid])
        return
    last_d, last_n = series[-1]
    prev = [x for x in series if x[0] <= last_d - dt.timedelta(days=182)]
    prev_d, prev_n = prev[-1] if prev else series[0]
    smooth = _smoothed(series)
    window = [x for x in smooth if x[0] >= last_d - dt.timedelta(days=730)]
    peak_d, peak_n = max(window, key=lambda x: x[1])
    all_peak_d, all_peak_n = max(smooth, key=lambda x: x[1])
    peak_n = max(peak_n, last_n)
    change = last_n / prev_n - 1 if prev_n else 0.0
    drawdown = 1 - last_n / peak_n if peak_n else 0.0
    ctx.change_6m = change
    value = {"latest": last_n, "latest_date": last_d.isoformat(), "six_months_ago": prev_n, "six_months_ago_date": prev_d.isoformat(),
             "change_6m": round(change, 4), "peak_24m": peak_n, "peak_24m_date": peak_d.isoformat(), "drawdown_24m": round(drawdown, 4),
             "all_time_peak": all_peak_n, "all_time_peak_date": all_peak_d.isoformat(), "points": len(series)}
    eid = ctx.ev("headcount_trend", value, src, as_of=last_d.isoformat())
    ids = [eid]
    api_yoy = (hc.get("growth_percent") or {}).get("yoy")
    year_ago = [x for x in series if x[0] <= last_d - dt.timedelta(days=365)]
    if api_yoy is not None and year_ago and not ctx.pit:
        ours = last_n / year_ago[-1][1] - 1
        api = api_yoy / 100 if abs(api_yoy) > 1.5 else api_yoy
        if abs(ours - api) > 0.10:
            ids.append(ctx.ev("headcount_trend", {"api_yoy": api_yoy, "series_yoy": round(ours, 4)},
                              f"company/enrich #{ctx.company.crustdata_id} headcount.growth_percent",
                              status="conflict", note="API growth field disagrees with the series; the series wins"))
    p6, pdd = ctx.pol["headcount_6m_change"], ctx.pol["headcount_drawdown"]
    min_drop = p6.get("min_abs_change", 0)            # a few people on a small team is noise, not distress
    big_6m = prev_n - last_n >= min_drop
    big_dd = peak_n - last_n >= min_drop
    if (change <= p6["red_at_or_below"] and big_6m) or (drawdown >= pdd["red_at_or_above"] and big_dd):
        level = "red"
    elif (change <= p6["amber_at_or_below"] and big_6m) or (drawdown >= pdd["amber_at_or_above"] and big_dd):
        level = "amber"
    else:
        level = "ok"
    text = (f"Headcount {last_n:,} ({last_d:%b %Y}), {change:+.0%} in 6 months, {drawdown:.0%} below its 24-month peak"
            + (f"; all-time peak {all_peak_n:,} in {all_peak_d:%b %Y}" if all_peak_n > peak_n else ""))
    ctx.add("headcount_trend", level, text, ids)


def _internal_move(profile: dict, cid: int, entry: dict, end: dt.date, days: int) -> bool:
    """True when the person took another job at the same company around the time this one ended
    (a promotion or transfer), so it is not an exit."""
    ed = (profile.get("experience") or {}).get("employment_details") or {}
    for other in (ed.get("current") or []) + (ed.get("past") or []):
        if other is entry or other.get("crustdata_company_id") != cid:
            continue
        if other.get("title") == entry.get("title") and other.get("start_date") == entry.get("start_date"):
            continue
        start, stop = _date(other.get("start_date")), _date(other.get("end_date"))
        if start and start <= end + dt.timedelta(days=days) and (stop is None or stop >= end):
            return True
    return False


def s_exits(ctx: Ctx) -> None:
    cid, T = ctx.company.crustdata_id, ctx.today
    days = ctx.pol["finance_team_exits"]["window_days"]
    lag = ctx.pol.get("exits_lag_days", 60) if ctx.pit else 0     # profiles are updated late
    until = T - dt.timedelta(days=lag)
    since = until - dt.timedelta(days=days)
    titles = any_of(*[cond(f"{PAST}.title", "(.)", t)
                      for t in ("finance", "CFO", "controller", "accounts", "accountant", "chief", "CEO", "COO", "CTO", "founder")])
    data = client().person_search(all_of(cond(f"{PAST}.company_id", "=", cid), titles,
                                         cond("experience.employment_details.end_date", "=>", since.isoformat())), limit=25)
    exits, moves = [], 0
    for p in data.get("profiles", []):
        for e in (p.get("experience") or {}).get("employment_details", {}).get("past") or []:
            end = _date(e.get("end_date"))
            if e.get("crustdata_company_id") != cid or not end or not (since <= end <= until):   # same job entry
                continue
            if _internal_move(p, cid, e, end, ctx.pol.get("internal_move_days", 62)):
                moves += 1
                continue
            exits.append({"name": p.get("basic_profile", {}).get("name"), "title": e.get("title"), "left": end.isoformat()[:7]})
    if not ctx.pit:
        for ev in _simulated_events(cid, "finance_head_left"):
            exits.append({"name": ev.get("name", "Finance head"), "title": ev.get("title", "Head of Finance"),
                          "left": ev.get("left", T.isoformat()[:7]), "simulated": True})
    fin = [x for x in exits if is_finance(x["title"]) or (x.get("simulated") and is_finance_head(x["title"]))]
    heads = [x for x in fin if is_finance_head(x["title"])]
    clevel = [x for x in exits if is_clevel(x["title"]) and x not in fin]
    window_txt = f"{since}..{until}" + (f" (jobs that ended at least {lag} days before {T})" if lag else "")
    src = f"person/search past.company_id={cid} + end_date in {window_txt}"
    sim = any(x.get("simulated") for x in heads)
    hid = ctx.ev("finance_head_exit", {"heads": heads, "internal_moves_skipped": moves},
                 src + (" + watcher event (simulated)" if sim else ""))
    span = f"between {since:%b %Y} and {until:%b %Y}" if lag else f"in the last {days} days"
    if heads:
        h = heads[0]
        ctx.add("finance_head_exit", "red",
                f"{h['title']} left in {h['left']}" + (" (watcher event, simulated)" if h.get("simulated") else ""), [hid])
    else:
        ctx.add("finance_head_exit", "ok", f"No finance head left {span}", [hid])

    pf = ctx.pol["finance_team_exits"]
    fin_titles = any_of(*[cond(f"{CUR}.title", "(.)", t) for t in ("finance", "accounts", "controller", "CFO")])
    try:
        if ctx.pit:
            fin_past = any_of(*[cond(f"{PAST}.title", "(.)", t) for t in ("finance", "accounts", "controller", "CFO")])
            current_fin = client().count_people(all_of(cond(f"{CUR}.company_id", "=", cid), fin_titles,
                                                       cond(f"{CUR}.start_date", "=<", T.isoformat()))) + \
                client().count_people(all_of(cond(f"{PAST}.company_id", "=", cid), fin_past,
                                             cond(f"{PAST}.start_date", "=<", T.isoformat()),
                                             cond("experience.employment_details.end_date", "=>", T.isoformat())))
        else:
            current_fin = client().count_people(all_of(cond(f"{CUR}.company_id", "=", cid), fin_titles))
    except Unavailable:
        current_fin = None
    team = (current_fin or 0) + len(fin)
    share = len(fin) / team if team else 0.0
    fid = ctx.ev("finance_team_exits", {"left": len(fin), "current_finance_staff": current_fin, "share_left": round(share, 3),
                                        "internal_moves_skipped": moves, "people": fin[:10]}, src, unit="people")
    if len(fin) >= pf["min_people"] and share >= pf["red_share"]:
        lvl = "red"
    elif len(fin) >= pf["min_people"] and share >= pf["amber_share"]:
        lvl = "amber"
    else:
        lvl = "ok"
    ctx.add("finance_team_exits", lvl, f"{len(fin)} of about {team} finance staff ({share:.0%}) left {span}", [fid])

    pc = ctx.pol["clevel_exits"]
    large = (ctx.profiles or 0) >= pc.get("large_company_profiles", 10**9)
    red_at = pc.get("large_red_at_or_above", pc["red_at_or_above"]) if large else pc["red_at_or_above"]
    amber_at = pc.get("large_amber_at_or_above", pc["amber_at_or_above"]) if large else pc["amber_at_or_above"]
    cl = ctx.ev("clevel_exits", {"count": len(clevel), "people": clevel[:10], "large_company": large,
                                 "red_at": red_at, "amber_at": amber_at}, src, unit="people")
    lvl = "red" if len(clevel) >= red_at else "amber" if len(clevel) >= amber_at else "ok"
    ctx.add("clevel_exits", lvl, f"{len(clevel)} founders or C-level leaders left {span}", [cl])


def _rounds_until(funding: dict, T: dt.date) -> list[tuple[dt.date, dict]]:
    rounds = []
    for m in (funding.get("milestones") or funding.get("funding_rounds") or []):
        d = _date(m.get("funding_date") or m.get("date"))
        if d and d <= T:
            rounds.append((d, m))
    return sorted(rounds, key=lambda x: x[0])


def s_funding_and_age(ctx: Ctx) -> None:
    if ctx.enrich_failed:
        eid = ctx.ev("funding_age", None, "company/enrich", status="unavailable", note="Company enrich failed")
        ctx.add("funding_age", "na", "Could not verify funding: company data unavailable", [eid])
        return
    cd = ctx.enriched or {}
    T = ctx.today
    src = f"company/enrich #{ctx.company.crustdata_id}"
    f = cd.get("funding") or {}
    reported = _date(f.get("last_fundraise_date"))
    round_type, amount = f.get("last_round_type"), f.get("last_round_amount_usd")
    if reported and reported > ctx.real_today:
        eid = ctx.ev("funding_age", str(reported), src + " funding.last_fundraise_date", status="invalid",
                     note="Funding date is in the future")
        ctx.add("funding_age", "na", "Could not verify funding age: the funding date is in the future", [eid])
        last = None
    else:
        last = reported
        if ctx.pit:                                     # only rounds announced on or before T
            rounds = _rounds_until(f, T)
            if rounds:
                last, m = rounds[-1]
                round_type, amount = m.get("round_type"), m.get("amount_usd")
            elif last and last > T:
                last = None
        if last:
            months = (T.year - last.year) * 12 + T.month - last.month
            eid = ctx.ev("funding_age", {"last_fundraise_date": last.isoformat(), "months": months, "round": round_type,
                                         "amount_usd": amount}, src + " funding", as_of=last.isoformat())
            p = ctx.pol["funding_age_months"]
            shrinking = ctx.change_6m is not None and ctx.change_6m < -0.05
            stalling = shrinking or ctx.joiner_level in ("red", "amber")
            lvl = "red" if months > p["red_above"] and shrinking else "amber" if months > p["amber_above"] and stalling else "ok"
            ctx.add("funding_age", lvl, f"Last funding {months} months ago ({last:%b %Y}" +
                    (f", {round_type}" if round_type else "") + ")", [eid])
        elif not (reported and reported > ctx.real_today):
            eid = ctx.ev("funding_age", None, src + " funding", status="unavailable",
                         note="No funding record" + (f" on or before {T}" if ctx.pit else ""))
            ctx.add("funding_age", "na", "Could not verify funding: no record", [eid])

    year = (cd.get("basic_info") or {}).get("year_founded")
    if isinstance(year, int) and year > ctx.real_today.year:
        eid = ctx.ev("company_age", year, src + " basic_info.year_founded", status="invalid", note="Founding year in the future")
        ctx.add("company_age", "na", "Could not verify company age", [eid])
    elif isinstance(year, int) and year > T.year:
        eid = ctx.ev("company_age", year, src + " basic_info.year_founded", status="unavailable",
                     note=f"Founded after {T}")
        ctx.add("company_age", "na", f"Could not verify company age: founded after {T:%b %Y}", [eid])
    elif isinstance(year, int):
        age = T.year - year
        eid = ctx.ev("company_age", {"year_founded": year, "age_years": age}, src + " basic_info.year_founded")
        ctx.add("company_age", "amber" if age < ctx.pol["company_age_years"]["amber_below"] else "ok",
                f"Founded in {year} ({age} years old)", [eid])


def s_job_posts(ctx: Ctx) -> None:
    cid, T = ctx.company.crustdata_id, ctx.today
    since = _month_start(T, 12)
    conds = [cond("company.basic_info.crustdata_company_id", "=", cid), cond("metadata.date_added", "=>", since.isoformat())]
    if ctx.pit:
        conds.append(cond("metadata.date_added", "=<", T.isoformat()))
    data = client().job_search(all_of(*conds), limit=3, sorts=[{"field": "metadata.date_added", "order": "desc"}])
    total = int(data.get("total_count") or 0)
    newest = [{"title": (j.get("job_details") or {}).get("title"), "added": (j.get("metadata") or {}).get("date_added", "")[:10]}
              for j in data.get("job_listings", [])]
    src = f"job/search company_id={cid} + date_added=>{since}" + (f" and =<{T}" if ctx.pit else "")
    if total == 0:
        # Zero can mean "stopped hiring" or "the job dataset never covered this company". Tell them apart.
        before = client().job_search(all_of(cond("company.basic_info.crustdata_company_id", "=", cid),
                                            cond("metadata.date_added", "=<", T.isoformat())), limit=1)
        history = int(before.get("total_count") or 0)
        if history == 0:
            eid = ctx.ev("job_posts", {"count": 0, "posts_ever_before": 0, "since": since.isoformat()}, src,
                         status="unavailable", unit="posts", note="No job-post history for this company in the dataset")
            ctx.add("job_posts", "na", "Could not verify hiring posts: no job-post history for this company", [eid])
            return
        eid = ctx.ev("job_posts", {"count": 0, "posts_ever_before": history, "since": since.isoformat()}, src, unit="posts")
        weak_joiners = ctx.joiner_level in ("red", "amber")
        if (ctx.profiles or 0) >= ctx.defaults["min_profiles"] and weak_joiners:
            lvl = "red" if ctx.joiner_level == "red" else "amber"
        else:
            lvl = "ok"                                  # people are still joining: hiring without public posts
        ctx.add("job_posts", lvl, f"No job posts since {since:%b %Y}" + ("" if lvl != "ok" else ", but people are still joining"),
                [eid])
        return
    eid = ctx.ev("job_posts", {"count": total, "since": since.isoformat(), "newest": newest}, src, unit="posts")
    ctx.add("job_posts", "ok", f"{total} job posts since {since:%b %Y}" + (f", newest on {newest[0]['added']}" if newest else ""), [eid])


def s_website(ctx: Ctx) -> None:
    if ctx.pit:
        return                                          # the past cannot be fetched: no result at all
    url = f"https://{ctx.company.primary_domain}" if ctx.company.primary_domain else ctx.company.website
    if not url:
        return
    url = url.split("?")[0]
    c = client()
    res = c.web_fetch([url])
    item = res[0] if isinstance(res, list) and res else {}
    if not item.get("success") and item.get("retryable"):
        res = c.post("/web/enrich/live", {"urls": [url]}, cacheable=False)
        item = res[0] if isinstance(res, list) and res else item
    ok = bool(item.get("success"))
    code = item.get("status_code")
    blocked = not ok and code in BOT_BLOCK_CODES
    eid = ctx.ev("website", {"url": url, "success": ok, "status_code": code, "error": item.get("error"),
                             "title": item.get("title"), "blocks_automated_readers": blocked}, "web/enrich/live", url=url)
    if ok:
        ctx.add("website", "ok", f"Website {url} loads", [eid])
    elif blocked:
        ctx.add("website", "ok", f"Website {url} responds but blocks automated reading ({code})", [eid])
    else:
        ctx.add("website", "amber", f"Website {url} unreachable after a retry ({code or item.get('error')})", [eid])


NEWS_SCHEMA = {
    "type": "OBJECT",
    "properties": {"distress": {"type": "BOOLEAN"}, "about_company": {"type": "BOOLEAN"}, "quote": {"type": "STRING"}},
    "required": ["distress", "about_company", "quote"],
}
NEWS_PROMPT = """Company: {company}
Headline: {title}
Snippet: {snippet}

Does this headline/snippet report that THIS company has unpaid salaries, unpaid vendors or dues, layoffs, insolvency,
shutdown or a cash crunch? Answer about_company=false if it is mainly about another company or a general list.
quote = the exact words from the headline or snippet that support your answer (empty if none)."""
DISTRESS_WORDS = re.compile(r"unpaid|salar(y|ies) delay|delayed salar|insolven|shut ?down|layoffs?|laid off|bankrupt|nclt|dues|"
                            r"cash crunch|wind(ing)? up|demise|collapse", re.IGNORECASE)


def s_news(ctx: Ctx) -> None:
    name = ctx.company.name
    days = ctx.pol.get("news_window_days", 180)
    start = int((dt.datetime.combine(ctx.today, dt.time()) - dt.timedelta(days=days)).timestamp())
    end = int(dt.datetime.combine(ctx.today, dt.time()).timestamp()) if ctx.pit else None
    data = client().web_search(f"{name} layoffs salary delay unpaid vendors insolvency shutdown", ["news"],
                               start_date=start, end_date=end)
    hits, ids = [], []
    for r in (data.get("results") or [])[:6]:
        title, snippet, url = r.get("title") or "", r.get("snippet") or "", r.get("url") or ""
        if JUNK_NEWS.search(url + " " + title):
            continue
        if not mentions(name, title + " " + snippet):
            continue
        verdict, model = llm.generate("news_distress_v1", NEWS_PROMPT.format(company=name, title=title, snippet=snippet), NEWS_SCHEMA)
        if isinstance(verdict, dict):
            distress, about, quote, how = verdict.get("distress"), verdict.get("about_company"), verdict.get("quote", ""), model
        else:
            m = DISTRESS_WORDS.search(title + " " + snippet)
            distress, about, quote, how = bool(m), True, (m.group(0) if m else ""), "keywords"
        if distress and about:
            hits.append({"title": title, "url": url, "quote": quote, "classified_by": how})
            ids.append(ctx.ev("distress_news", {"title": title, "quote": quote, "classified_by": how}, "web/search/live news",
                              url=url, as_of=f"within {days} days (search date filter)"))
    window = f"in the {days} days before {ctx.today:%d %b %Y}" if ctx.pit else f"in the last {days} days"
    if hits:
        strong = [h for h in hits if h["classified_by"] != "keywords"]
        ctx.add("distress_news", "red" if strong else "amber",
                f"{len(hits)} news report(s) of distress {window}, e.g. \"{hits[0]['title'][:90]}\"", ids)
    else:
        eid = ctx.ev("distress_news", {"count": 0}, "web/search/live news", as_of=f"within {days} days")
        ctx.add("distress_news", "ok", f"No distress news found {window}", [eid])


def _simulated_events(company_id: int, kind: str) -> list[dict]:
    import json
    rows = store.conn().execute("SELECT payload_json FROM watch_events WHERE company_id = ? AND kind = ?", (company_id, kind)).fetchall()
    return [json.loads(r["payload_json"]) for r in rows]


Step = tuple[str, Callable[[Ctx], None], str]

STEPS: list[Step] = [
    ("headcount_trend", s_enrich, "company/enrich"),
    ("joiner_rate", s_profiles_and_joiners, "person/search counts"),
    ("headcount_trend", s_headcount, "headcount history"),
    ("finance_head_exit", s_exits, "person/search exits"),
    ("funding_age", s_funding_and_age, "funding and age"),
    ("job_posts", s_job_posts, "job/search"),
    ("website", s_website, "web fetch"),
    ("distress_news", s_news, "news search"),
]
_BY_FN = {fn: (sig, fn, what) for sig, fn, what in STEPS}

# Steps in a group run together; a group starts only after the previous one finished.
GROUPS_LIVE = [[s_enrich, s_profiles_and_joiners, s_website, s_news],      # independent
               [s_headcount, s_exits, s_job_posts],                         # need enrich / profiles / joiner level
               [s_funding_and_age]]                                         # needs the 6-month change
GROUPS_PIT = [[s_enrich, s_news],
              [s_headcount, s_profiles_and_joiners],                        # profiles at T come from the series
              [s_exits, s_job_posts, s_funding_and_age]]


def _run_step(ctx: Ctx, step: Step) -> None:
    signal, fn, what = step
    if store.case_cost(ctx.case_id) > config.CASE_CREDIT_CAP:
        ctx.unavailable(signal, f"{what} (case credit cap reached)", RuntimeError("credit cap"))
        return
    try:
        fn(ctx)
    except (Unavailable, BadRequest, Forbidden) as e:      # OutOfCredits and bugs propagate
        if fn is s_enrich:
            ctx.enrich_failed = True
        ctx.unavailable(signal, what, e)


def collect(case_id: str, company: Company, as_of: Optional[dt.date] = None,
            parallel: Optional[bool] = None) -> list[SignalResult]:
    """All signals for one company. as_of=T: only what was knowable on date T (backtest)."""
    store.clear_evidence(case_id, SIGNALS)
    ctx = Ctx(case_id, company, as_of)
    if parallel is None:
        parallel = os.environ.get("TERMSDESK_PARALLEL", "1") != "0"
    for group in (GROUPS_PIT if ctx.pit else GROUPS_LIVE):
        steps = [_BY_FN[fn] for fn in group]
        if not parallel or len(steps) == 1:
            for step in steps:
                _run_step(ctx, step)
            continue
        with cf.ThreadPoolExecutor(max_workers=len(steps)) as pool:
            # copy_context per task: the case id reaches the cost log, and no context is entered twice
            futures = [pool.submit(contextvars.copy_context().run, _run_step, ctx, step) for step in steps]
            for fut in futures:
                fut.result()
    order = {s: i for i, s in enumerate(SIGNALS)}
    ctx.results.sort(key=lambda r: order.get(r.signal, len(order)))   # stable: same case, same order
    return ctx.results
