"""The ten credit signals. Each one saves its evidence (with source and as-of date) and returns a level.

We compute trends ourselves from raw data; summary fields (growth %, people, revenue) are never trusted.
A failed call never stops the case: the signal becomes 'na' and the letter lists it under "Could not verify".
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Callable, Optional

import yaml

from . import config, llm, store
from .contracts import Company, Evidence, SignalResult
from .crustdata import BadRequest, Forbidden, Unavailable, all_of, any_of, client, cond

SIGNALS = ["profiles", "joiner_rate", "headcount_trend", "finance_head_exit", "finance_team_exits", "clevel_exits",
           "funding_age", "company_age", "job_posts", "website", "distress_news"]

FINANCE = re.compile(r"financ|\bcfo\b|controller|accounts?\b|accounting|treasur", re.IGNORECASE)
FINANCE_HEAD = re.compile(r"\bcfo\b|chief financial|head of (core )?financ|financ\w* head|\bvp\b,?\s*-?\s*financ|vice president,?\s*-?\s*financ",
                          re.IGNORECASE)
CLEVEL = re.compile(r"\bceo\b|\bcoo\b|\bcto\b|\bcbo\b|\bcro\b|\bcmo\b|\bcpo\b|chief \w+ officer|co-?founder|\bfounder\b", re.IGNORECASE)
JUNK_NEWS = re.compile(r"tracxn|crunchbase|zoominfo|cbinsights|pitchbook|owler|glassdoor|ambitionbox|linkedin|markhub", re.IGNORECASE)


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


class Ctx:
    """What one collection run shares between signals."""

    def __init__(self, case_id: str, company: Company):
        self.case_id, self.company = case_id, company
        self.pol = policy()["signals"]
        self.today = _today()
        self.enriched: Optional[dict] = None
        self.profiles: Optional[int] = None
        self.change_6m: Optional[float] = None
        self.joiner_level: str = "na"
        self.enrich_failed = False
        self.results: list[SignalResult] = []

    def ev(self, signal: str, value, source: str, *, status: str = "verified", as_of: Optional[str] = None,
           url: Optional[str] = None, unit: Optional[str] = None, note: Optional[str] = None) -> str:
        e = store.add_evidence(Evidence(case_id=self.case_id, signal=signal, value=value, source=source, status=status,
                                        as_of=as_of or self.today.isoformat(), url=url, unit=unit, note=note))
        return e.id

    def add(self, signal: str, level: str, summary: str, ids: list[str]) -> None:
        self.results.append(SignalResult(signal=signal, level=level, summary=summary, evidence_ids=ids))

    def unavailable(self, signal: str, what: str, err: Exception) -> None:
        eid = self.ev(signal, None, what, status="unavailable", note=str(err)[:200])
        self.add(signal, "na", f"Could not verify {signal.replace('_', ' ')}: {what} failed", [eid])


# ---------- individual signals ----------

def s_enrich(ctx: Ctx) -> None:
    ctx.enriched = client().enrich(ctx.company.crustdata_id, ["basic_info", "headcount", "funding"])


def s_profiles_and_joiners(ctx: Ctx) -> None:
    cid, name = ctx.company.crustdata_id, ctx.company.name
    c = client()
    by_id = c.count_people(cond("experience.employment_details.current.company_id", "=", cid))
    try:
        by_name = c.count_people(cond("experience.employment_details.current.company_name", "[.]", name))
    except Unavailable:
        by_name = None
    ctx.profiles = by_id
    gap = abs(by_id - by_name) / max(by_id, by_name, 1) if by_name is not None else 0
    status = "conflict" if gap > 0.5 else "verified"
    pid = ctx.ev("profiles", {"by_company_id": by_id, "by_exact_name": by_name}, f"person/search current.company_id={cid}",
                 status=status, unit="profiles",
                 note=None if status == "verified" else "ID and name counts disagree by more than half; using the ID count")
    ctx.add("profiles", "ok" if by_id >= policy()["defaults"]["min_profiles"] else "na",
            f"{by_id:,} people list {name} as their current employer", [pid])

    since = _month_start(ctx.today, 12)
    joiners = c.count_people(all_of(cond("experience.employment_details.current.company_id", "=", cid),
                                    cond("experience.employment_details.current.start_date", "=>", since.isoformat())))
    rate = joiners / by_id if by_id else 0.0
    jid = ctx.ev("joiner_rate", {"joiners": joiners, "profiles": by_id, "rate": round(rate, 4), "since": since.isoformat()},
                 f"person/search current.company_id={cid} + current.start_date=>{since}", unit="share")
    p = ctx.pol["joiner_rate"]
    if by_id < policy()["defaults"]["min_profiles"]:
        level, text = "na", f"Too few profiles ({by_id}) to judge hiring"
    elif rate < p["red_below"]:
        level, text = "red", f"{joiners} joiners since {since:%b %Y} out of {by_id:,} profiles ({rate:.1%}): nobody is joining"
    elif rate < p["amber_below"]:
        level, text = "amber", f"Only {joiners} joiners since {since:%b %Y} out of {by_id:,} profiles ({rate:.1%})"
    else:
        level, text = "ok", f"{joiners} joiners since {since:%b %Y} out of {by_id:,} profiles ({rate:.1%})"
    ctx.joiner_level = level
    ctx.add("joiner_rate", level, text, [jid])


def s_headcount(ctx: Ctx) -> None:
    if ctx.enrich_failed:
        return  # already recorded as could-not-verify
    hc = (ctx.enriched or {}).get("headcount") or {}
    series = []
    invalid = []
    for p in hc.get("timeseries") or []:
        d, n = _date(p.get("date")), p.get("employee_count")
        if d is None or n is None:
            continue
        if n < 0 or d > ctx.today:
            invalid.append(p)
            continue
        series.append((d, int(n)))
    series.sort()
    src = f"company/enrich #{ctx.company.crustdata_id} headcount.timeseries"
    if invalid:
        ctx.ev("headcount_trend", invalid[:3], src, status="invalid", note="Negative or future headcount points ignored")
    if len(series) < 2:
        eid = ctx.ev("headcount_trend", None, src, status="unavailable", note="No usable headcount history")
        ctx.add("headcount_trend", "na", "Could not verify headcount trend: no usable history", [eid])
        return
    last_d, last_n = series[-1]
    prev = [x for x in series if x[0] <= last_d - dt.timedelta(days=182)]
    prev_d, prev_n = prev[-1] if prev else series[0]
    window = [x for x in series if x[0] >= last_d - dt.timedelta(days=730)]
    peak_d, peak_n = max(window, key=lambda x: x[1])
    all_peak_d, all_peak_n = max(series, key=lambda x: x[1])
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
    if api_yoy is not None and year_ago:
        ours = last_n / year_ago[-1][1] - 1
        api = api_yoy / 100 if abs(api_yoy) > 1.5 else api_yoy
        if abs(ours - api) > 0.10:
            ids.append(ctx.ev("headcount_trend", {"api_yoy": api_yoy, "series_yoy": round(ours, 4)}, f"company/enrich #{ctx.company.crustdata_id} headcount.growth_percent",
                              status="conflict", note="API growth field disagrees with the series; the series wins"))
    p6, pdd = ctx.pol["headcount_6m_change"], ctx.pol["headcount_drawdown"]
    if change <= p6["red_at_or_below"] or drawdown >= pdd["red_at_or_above"]:
        level = "red"
    elif change <= p6["amber_at_or_below"] or drawdown >= pdd["amber_at_or_above"]:
        level = "amber"
    else:
        level = "ok"
    text = (f"Headcount {last_n:,} ({last_d:%b %Y}), {change:+.0%} in 6 months, {drawdown:.0%} below its 24-month peak"
            + (f"; all-time peak {all_peak_n:,} in {all_peak_d:%b %Y}" if all_peak_n > peak_n else ""))
    ctx.add("headcount_trend", level, text, ids)


def s_exits(ctx: Ctx) -> None:
    cid = ctx.company.crustdata_id
    days = ctx.pol["finance_team_exits"]["window_days"]
    since = ctx.today - dt.timedelta(days=days)
    titles = any_of(*[cond("experience.employment_details.past.title", "(.)", t)
                      for t in ("finance", "CFO", "controller", "accounts", "chief", "CEO", "COO", "CTO", "founder")])
    data = client().person_search(all_of(cond("experience.employment_details.past.company_id", "=", cid), titles,
                                         cond("experience.employment_details.end_date", "=>", since.isoformat())), limit=25)
    exits = []
    for p in data.get("profiles", []):
        for e in (p.get("experience") or {}).get("employment_details", {}).get("past") or []:
            end = _date(e.get("end_date"))
            if e.get("crustdata_company_id") == cid and end and since <= end <= ctx.today:  # same job entry
                exits.append({"name": p.get("basic_profile", {}).get("name"), "title": e.get("title"), "left": end.isoformat()[:7]})
    for ev in _simulated_events(cid, "finance_head_left"):
        exits.append({"name": ev.get("name", "Finance head"), "title": ev.get("title", "Head of Finance"), "left": ev.get("left", ctx.today.isoformat()[:7]),
                      "simulated": True})
    fin = [x for x in exits if FINANCE.search(x["title"] or "")]
    heads = [x for x in fin if FINANCE_HEAD.search(x["title"] or "")]
    clevel = [x for x in exits if CLEVEL.search(x["title"] or "") and x not in fin]
    src = f"person/search past.company_id={cid} + end_date=>{since}"
    sim = any(x.get("simulated") for x in heads)
    hid = ctx.ev("finance_head_exit", heads, src + (" + watcher event (simulated)" if sim else ""))
    if heads:
        h = heads[0]
        ctx.add("finance_head_exit", "red", f"{h['title']} left in {h['left']}" + (" (watcher event, simulated)" if h.get("simulated") else ""), [hid])
    else:
        ctx.add("finance_head_exit", "ok", f"No finance head left in the last {days} days", [hid])
    pf = ctx.pol["finance_team_exits"]
    try:
        current_fin = client().count_people(all_of(cond("experience.employment_details.current.company_id", "=", cid),
                                                   any_of(*[cond("experience.employment_details.current.title", "(.)", t)
                                                            for t in ("finance", "accounts", "controller", "CFO")])))
    except Unavailable:
        current_fin = None
    team = (current_fin or 0) + len(fin)
    share = len(fin) / team if team else 0.0
    fid = ctx.ev("finance_team_exits", {"left": len(fin), "current_finance_staff": current_fin, "share_left": round(share, 3),
                                        "people": fin[:10]}, src, unit="people")
    if len(fin) >= pf["min_people"] and share >= pf["red_share"]:
        lvl = "red"
    elif len(fin) >= pf["min_people"] and share >= pf["amber_share"]:
        lvl = "amber"
    else:
        lvl = "ok"
    ctx.add("finance_team_exits", lvl, f"{len(fin)} of about {team} finance staff ({share:.0%}) left in the last {days} days", [fid])
    pc = ctx.pol["clevel_exits"]
    cl = ctx.ev("clevel_exits", {"count": len(clevel), "people": clevel[:10]}, src, unit="people")
    lvl = "red" if len(clevel) >= pc["red_at_or_above"] else "amber" if len(clevel) >= pc["amber_at_or_above"] else "ok"
    ctx.add("clevel_exits", lvl, f"{len(clevel)} founders or C-level leaders left in the last {days} days", [cl])


def s_funding_and_age(ctx: Ctx) -> None:
    if ctx.enrich_failed:
        eid = ctx.ev("funding_age", None, "company/enrich", status="unavailable", note="Company enrich failed")
        ctx.add("funding_age", "na", "Could not verify funding: company data unavailable", [eid])
        return
    cd = ctx.enriched or {}
    src = f"company/enrich #{ctx.company.crustdata_id}"
    last = _date((cd.get("funding") or {}).get("last_fundraise_date"))
    if last and last > ctx.today:
        eid = ctx.ev("funding_age", str(last), src + " funding.last_fundraise_date", status="invalid", note="Funding date is in the future")
        ctx.add("funding_age", "na", "Could not verify funding age: the funding date is in the future", [eid])
    elif last:
        months = (ctx.today.year - last.year) * 12 + ctx.today.month - last.month
        f = cd.get("funding") or {}
        eid = ctx.ev("funding_age", {"last_fundraise_date": last.isoformat(), "months": months, "round": f.get("last_round_type"),
                                     "amount_usd": f.get("last_round_amount_usd")}, src + " funding", as_of=last.isoformat())
        p = ctx.pol["funding_age_months"]
        shrinking = ctx.change_6m is not None and ctx.change_6m < -0.05
        stalling = shrinking or ctx.joiner_level in ("red", "amber")
        lvl = "red" if months > p["red_above"] and shrinking else "amber" if months > p["amber_above"] and stalling else "ok"
        ctx.add("funding_age", lvl, f"Last funding {months} months ago ({last:%b %Y}" + (f", {f.get('last_round_type')}" if f.get("last_round_type") else "") + ")", [eid])
    else:
        eid = ctx.ev("funding_age", None, src + " funding", status="unavailable", note="No funding record")
        ctx.add("funding_age", "na", "Could not verify funding: no record", [eid])

    year = (cd.get("basic_info") or {}).get("year_founded")
    if isinstance(year, int) and year > ctx.today.year:
        eid = ctx.ev("company_age", year, src + " basic_info.year_founded", status="invalid", note="Founding year in the future")
        ctx.add("company_age", "na", "Could not verify company age", [eid])
    elif isinstance(year, int):
        age = ctx.today.year - year
        eid = ctx.ev("company_age", {"year_founded": year, "age_years": age}, src + " basic_info.year_founded")
        ctx.add("company_age", "amber" if age < ctx.pol["company_age_years"]["amber_below"] else "ok", f"Founded in {year} ({age} years old)", [eid])


def s_job_posts(ctx: Ctx) -> None:
    cid = ctx.company.crustdata_id
    since = _month_start(ctx.today, 12)
    data = client().job_search(all_of(cond("company.basic_info.crustdata_company_id", "=", cid),
                                      cond("metadata.date_added", "=>", since.isoformat())),
                               limit=3, sorts=[{"field": "metadata.date_added", "order": "desc"}])
    total = int(data.get("total_count") or 0)
    newest = [{"title": (j.get("job_details") or {}).get("title"), "added": (j.get("metadata") or {}).get("date_added", "")[:10]}
              for j in data.get("job_listings", [])]
    eid = ctx.ev("job_posts", {"count": total, "since": since.isoformat(), "newest": newest}, f"job/search company_id={cid} + date_added=>{since}", unit="posts")
    if total == 0 and (ctx.profiles or 0) >= policy()["defaults"]["min_profiles"]:
        lvl = "red" if ctx.joiner_level == "red" else "amber"
        ctx.add("job_posts", lvl, f"No job posts since {since:%b %Y}", [eid])
    else:
        ctx.add("job_posts", "ok", f"{total} job posts since {since:%b %Y}" + (f", newest on {newest[0]['added']}" if newest else ""), [eid])


def s_website(ctx: Ctx) -> None:
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
    eid = ctx.ev("website", {"url": url, "success": ok, "status_code": item.get("status_code"), "error": item.get("error"),
                             "title": item.get("title")}, "web/enrich/live", url=url)
    ctx.add("website", "ok" if ok else "amber",
            f"Website {url} loads" if ok else f"Website {url} unreachable after a retry ({item.get('status_code') or item.get('error')})", [eid])


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
DISTRESS_WORDS = re.compile(r"unpaid|salar(y|ies) delay|delayed salar|insolven|shut ?down|layoffs?|laid off|bankrupt|nclt|dues|cash crunch|wind(ing)? up|demise|collapse", re.IGNORECASE)


def s_news(ctx: Ctx) -> None:
    name = ctx.company.name
    days = ctx.pol.get("news_window_days", 180) if isinstance(ctx.pol, dict) else 180
    start = int((dt.datetime.combine(ctx.today, dt.time()) - dt.timedelta(days=days)).timestamp())
    data = client().web_search(f"{name} layoffs salary delay unpaid vendors insolvency shutdown", ["news"], start_date=start)
    hits, ids = [], []
    for r in (data.get("results") or [])[:6]:
        title, snippet, url = r.get("title") or "", r.get("snippet") or "", r.get("url") or ""
        if JUNK_NEWS.search(url + " " + title):
            continue
        if name.lower().split()[0] not in (title + " " + snippet).lower():
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
    if hits:
        strong = [h for h in hits if h["classified_by"] != "keywords"]
        ctx.add("distress_news", "red" if strong else "amber", f"{len(hits)} news report(s) of distress in the last {days} days, e.g. \"{hits[0]['title'][:90]}\"", ids)
    else:
        eid = ctx.ev("distress_news", {"count": 0}, "web/search/live news", as_of=f"within {days} days")
        ctx.add("distress_news", "ok", f"No distress news found in the last {days} days", [eid])


def _simulated_events(company_id: int, kind: str) -> list[dict]:
    import json
    rows = store.conn().execute("SELECT payload_json FROM watch_events WHERE company_id = ? AND kind = ?", (company_id, kind)).fetchall()
    return [json.loads(r["payload_json"]) for r in rows]


STEPS: list[tuple[str, Callable[[Ctx], None], str]] = [
    ("headcount_trend", s_enrich, "company/enrich"),
    ("joiner_rate", s_profiles_and_joiners, "person/search counts"),
    ("headcount_trend", s_headcount, "headcount history"),
    ("finance_head_exit", s_exits, "person/search exits"),
    ("funding_age", s_funding_and_age, "funding and age"),
    ("job_posts", s_job_posts, "job/search"),
    ("website", s_website, "web fetch"),
    ("distress_news", s_news, "news search"),
]


def collect(case_id: str, company: Company) -> list[SignalResult]:
    store.clear_evidence(case_id, SIGNALS)
    ctx = Ctx(case_id, company)
    for signal, fn, what in STEPS:
        if store.case_cost(case_id) > config.CASE_CREDIT_CAP:
            ctx.unavailable(signal, f"{what} (case credit cap reached)", RuntimeError("credit cap"))
            continue
        try:
            fn(ctx)
        except (Unavailable, BadRequest, Forbidden) as e:
            if fn is s_enrich:
                ctx.enrich_failed = True
            ctx.unavailable(signal, what, e)
    return ctx.results
