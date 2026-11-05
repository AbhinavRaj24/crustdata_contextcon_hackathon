"""Backtest: replay the credit policy month by month before each collapse, using only data dated on or before
that month, and check how often it raises false alarms on healthy companies.

Point-in-time rules (stated in the README):
- headcount trend: headcount.timeseries points dated <= T
- joiner rate: people whose job at the company started in (T-12m, T] (current and past jobs), / headcount at T
- exits: only jobs that ended at least 60 days before T (profiles are updated late), within the 180 days before that
- funding age: funding milestones dated <= T
- job posts, website, news: not replayed (job-post history is too thin before 2025; websites cannot be fetched
  as they were; news is checked live only). They count as "could not verify", never for or against.

Usage: python -m backtest.run_backtest [Company ...]    -> backtest/results/*.json + summary.md
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

import yaml

from termsdesk import config
from termsdesk.crustdata import Unavailable, all_of, any_of, client, cond

HERE = Path(__file__).parent
OUT = HERE / "results"
E = "experience.employment_details."
POL = yaml.safe_load(config.POLICY_PATH.read_text())
SIG = POL["signals"]
FIN = re.compile(r"financ|\bcfo\b|controller|accounts?\b|accounting|treasur", re.I)
FIN_HEAD = re.compile(r"\bcfo\b|chief financial|head of (core )?financ|financ\w* head|\bvp\b,?\s*-?\s*financ|vice president,?\s*-?\s*financ", re.I)
CLEVEL = re.compile(r"\bceo\b|\bcoo\b|\bcto\b|\bcbo\b|\bcro\b|\bcmo\b|\bcpo\b|chief \w+ officer|co-?founder|\bfounder\b", re.I)


def d(s) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def month_start(x: dt.date, back: int) -> dt.date:
    y, m = x.year, x.month - back
    while m <= 0:
        m, y = m + 12, y - 1
    return dt.date(y, m, 1)


def load_company(cid: int) -> dict:
    """Everything that does not depend on T: one enrich + people lists (fetched once, cached per day)."""
    c = client()
    cd = c.enrich(cid, ["basic_info", "headcount", "funding"])
    series = sorted((d(p["date"]), p["employee_count"]) for p in ((cd.get("headcount") or {}).get("timeseries") or [])
                    if d(p.get("date")) and isinstance(p.get("employee_count"), int) and p["employee_count"] >= 0)
    rounds = sorted(d(m.get("date") or m.get("funding_date")) for m in ((cd.get("funding") or {}).get("milestones") or [])
                    if d(m.get("date") or m.get("funding_date")) and not m.get("is_fund"))
    titles = [cond(E + "past.title", "(.)", t) for t in ("finance", "CFO", "controller", "accounts", "chief", "CEO", "COO", "CTO", "founder")]
    leavers = []
    try:
        data = c.person_search(all_of(cond(E + "past.company_id", "=", cid), any_of(*titles)), limit=50)
        for p in data.get("profiles", []):
            for e in (p.get("experience") or {}).get("employment_details", {}).get("past") or []:
                if e.get("crustdata_company_id") == cid and d(e.get("end_date")):
                    leavers.append({"title": e.get("title") or "", "start": str(d(e.get("start_date")) or ""), "end": str(d(e.get("end_date")))})
    except Unavailable:
        pass
    current_fin = []
    try:
        data = c.person_search(all_of(cond(E + "current.company_id", "=", cid),
                                      any_of(*[cond(E + "current.title", "(.)", t) for t in ("finance", "accounts", "controller", "CFO")])), limit=50)
        for p in data.get("profiles", []):
            for e in (p.get("experience") or {}).get("employment_details", {}).get("current") or []:
                if e.get("crustdata_company_id") == cid:
                    current_fin.append({"title": e.get("title") or "", "start": str(d(e.get("start_date")) or "")})
    except Unavailable:
        pass
    return {"name": (cd.get("basic_info") or {}).get("name"), "series": series, "rounds": rounds,
            "leavers": leavers, "current_fin": current_fin, "founded": (cd.get("basic_info") or {}).get("year_founded")}


_JOINS: dict = {}


def joins(cid: int, lo: dt.date, hi: dt.date) -> int:
    """People whose job at the company started in [lo, hi), counting current and past jobs."""
    key = (cid, lo, hi)
    if key not in _JOINS:
        c = client()
        cur = c.count_people(all_of(cond(E + "current.company_id", "=", cid), cond(E + "current.start_date", "=>", lo.isoformat()),
                                    cond(E + "current.start_date", "<", hi.isoformat())))
        past = c.count_people(all_of(cond(E + "past.company_id", "=", cid), cond(E + "past.start_date", "=>", lo.isoformat()),
                                     cond(E + "past.start_date", "<", hi.isoformat())))
        _JOINS[key] = cur + past
    return _JOINS[key]


# v3 candidate (found on Dunzo, tested on the rest): hiring in the last 6 months vs the best 6 months of the prior year
MOMENTUM = {"red_at_or_below": -0.65, "amber_at_or_below": -0.50}


def level(value, red, amber, below=True) -> str:
    if value is None:
        return "na"
    if below:
        return "red" if value < red else "amber" if value < amber else "ok"
    return "red" if value >= red else "amber" if value >= amber else "ok"


def evaluate(cid: int, co: dict, T: dt.date) -> dict:
    sig, facts = {}, {}
    # headcount at T
    pts = [p for p in co["series"] if p[0] <= T]
    hc = pts[-1][1] if pts else None
    if len(pts) >= 2:
        last_d, last_n = pts[-1]
        prev = [p for p in pts if p[0] <= last_d - dt.timedelta(days=182)]
        prev_n = (prev[-1] if prev else pts[0])[1]
        peak = max(n for x, n in pts if x >= last_d - dt.timedelta(days=730))
        ch, dd = last_n / prev_n - 1, 1 - last_n / peak
        p6, pdd = SIG["headcount_6m_change"], SIG["headcount_drawdown"]
        sig["headcount_trend"] = ("red" if ch <= p6["red_at_or_below"] or dd >= pdd["red_at_or_above"] else
                                  "amber" if ch <= p6["amber_at_or_below"] or dd >= pdd["amber_at_or_above"] else "ok")
        facts.update(headcount=last_n, change_6m=round(ch, 3), drawdown=round(dd, 3))
    else:
        sig["headcount_trend"], ch = "na", None
    # joiner rate and hiring momentum at T (6-month windows ending at the start of T's month)
    try:
        t0 = month_start(T, 0)
        w0, w1, w2 = (joins(cid, month_start(T, 6), t0), joins(cid, month_start(T, 12), month_start(T, 6)),
                      joins(cid, month_start(T, 18), month_start(T, 12)))
        j = w0 + w1
        rate = j / hc if hc else None
        facts.update(joiners_12m=j, joiner_rate=round(rate, 3) if rate is not None else None, joiners_6m=w0, prior_best_6m=max(w1, w2))
        sig["joiner_rate"] = "na" if not hc or hc < POL["defaults"]["min_profiles"] else level(rate, SIG["joiner_rate"]["red_below"], SIG["joiner_rate"]["amber_below"])
        best = max(w1, w2)
        if best >= 20:
            mom = w0 / best - 1
            facts["hiring_momentum"] = round(mom, 3)
            sig["hiring_momentum"] = "red" if mom <= MOMENTUM["red_at_or_below"] else "amber" if mom <= MOMENTUM["amber_at_or_below"] else "ok"
        else:
            sig["hiring_momentum"] = "na"
    except Unavailable:
        sig["joiner_rate"] = sig["hiring_momentum"] = "na"
    # exits that ended in (T-60d-180d, T-60d]
    seen_until = T - dt.timedelta(days=60)
    window_lo = seen_until - dt.timedelta(days=SIG["finance_team_exits"]["window_days"])
    gone = [x for x in co["leavers"] if window_lo < d(x["end"]) <= seen_until]
    fin = [x for x in gone if FIN.search(x["title"])]
    heads = [x for x in fin if FIN_HEAD.search(x["title"])]
    clevel = [x for x in gone if CLEVEL.search(x["title"]) and x not in fin]
    team_at_t = sum(1 for x in co["current_fin"] if d(x["start"]) and d(x["start"]) <= T) + \
        sum(1 for x in co["leavers"] if FIN.search(x["title"]) and d(x["start"]) and d(x["start"]) <= T and d(x["end"]) > T)
    team = team_at_t + len(fin)
    share = len(fin) / team if team else 0
    pf = SIG["finance_team_exits"]
    sig["finance_head_exit"] = "red" if heads else "ok"
    sig["finance_team_exits"] = ("red" if len(fin) >= pf["min_people"] and share >= pf["red_share"] else
                                 "amber" if len(fin) >= pf["min_people"] and share >= pf["amber_share"] else "ok")
    pc = SIG["clevel_exits"]
    sig["clevel_exits"] = level(len(clevel), pc["red_at_or_above"], pc["amber_at_or_above"], below=False)
    facts.update(finance_exits=len(fin), finance_share=round(share, 2), finance_head_exit=[h["title"] for h in heads], clevel_exits=len(clevel))
    # funding age at T
    past_rounds = [r for r in co["rounds"] if r <= T]
    if past_rounds:
        months = (T.year - past_rounds[-1].year) * 12 + T.month - past_rounds[-1].month
        shrinking = ch is not None and ch < -0.05
        stalling = shrinking or sig["joiner_rate"] in ("red", "amber")
        p = SIG["funding_age_months"]
        sig["funding_age"] = "red" if months > p["red_above"] and shrinking else "amber" if months > p["amber_above"] and stalling else "ok"
        facts["funding_months"] = months
    else:
        sig["funding_age"] = "na"
    v2 = {k: v for k, v in sig.items() if k != "hiring_momentum"}
    return {"T": T.isoformat(), "signals": sig, "facts": facts, "band_v2": band(v2), "band": band(sig)}


def band(sig: dict) -> str:
    """Mirrors decide.py rules R3-R8 (verification rules R1-R2 do not apply to a backtest)."""
    reds = [k for k, v in sig.items() if v == "red"]
    ambers = [k for k, v in sig.items() if v == "amber"]
    if sig.get("joiner_rate") == "red":
        return "advance_only (R3)"
    if len(reds) >= 2:
        return "advance_only (R4)"
    if sig.get("headcount_trend") == "na" and sig.get("joiner_rate") == "na":
        return "thin_data (R5)"
    if len(reds) == 1:
        return "reduce_25 (R6)"
    if len(ambers) >= 2:
        return "reduce_50 (R7)"
    return "approve (R8)"


def months_between(a: dt.date, b: dt.date) -> int:
    return (b.year - a.year) * 12 + b.month - a.month


def run(name: str, domain: str, months: list[dt.date], event: dt.date | None) -> dict:
    m = client().identify(domain=domain) or client().identify(name=name)
    cid = max(m, key=lambda x: x["confidence_score"])["company_data"]["crustdata_company_id"]
    co = load_company(cid)
    rows = []
    for T in months:
        r = evaluate(cid, co, T)
        rows.append(r)
        print(f"  {r['T']}  v2={r['band_v2']:18} v3={r['band']:18} mom={r['facts'].get('hiring_momentum')} "
              + " ".join(f"{k[:8]}={v}" for k, v in r["signals"].items() if v in ("red", "amber")), flush=True)
    res = {"name": name, "crustdata_id": cid, "event_date": event.isoformat() if event else None, "rows": rows,
           "series_start": co["series"][0][0].isoformat() if co["series"] else None}
    for ver, key in (("v2", "band_v2"), ("v3", "band")):
        if event:
            # warning = months flagged without a break up to the event (one-off noisy flags do not count)
            before = [r for r in rows if d(r["T"]) <= event]
            streak = 0
            for r in reversed(before):
                if r[key].startswith(("approve", "thin")):
                    break
                streak += 1
            res[f"{ver}_months_warning"] = streak
            res[f"{ver}_flag_since"] = before[len(before) - streak]["T"] if streak else None
            res[f"{ver}_status_at_event"] = before[-1][key] if before else None
        else:
            res[f"{ver}_false_alarms"] = sum(1 for r in rows if not r[key].startswith(("approve", "thin")))
            res["points"] = len(rows)
    OUT.mkdir(exist_ok=True)
    (OUT / f"{re.sub(r'[^a-z0-9]+', '_', name.lower())}.json").write_text(json.dumps(res, indent=2))
    return res


def main(only: set[str]) -> None:
    cohort = yaml.safe_load((HERE / "cohort.yaml").read_text())
    events = json.loads((HERE / "events.json").read_text())
    results = []
    for co in cohort["distressed"]:
        if only and co["name"] not in only:
            continue
        ev = d(co.get("event_date") or (events.get(co["name"]) or {}).get("event_date"))
        if not ev or co.get("exclude"):
            print(f"== {co['name']}: skipped ({co.get('exclude') or 'no cited event date'})")
            continue
        print(f"== {co['name']} (event {ev})")
        months = [month_start(ev, k) for k in range(18, -1, -1)]
        results.append(run(co["name"], co["domain"], months, ev))
    for co in cohort["controls"]:
        if (only and co["name"] not in only) or co.get("exclude"):
            continue
        print(f"== {co['name']} (control)")
        months = [dt.date(y, m, 1) for y in (2023, 2024, 2025, 2026) for m in (3, 6, 9, 12) if dt.date(y, m, 1) <= dt.date(2026, 9, 1)]
        results.append(run(co["name"], co["domain"], months, None))
    write_summary()


def write_summary() -> None:
    cohort = yaml.safe_load((HERE / "cohort.yaml").read_text())
    events = json.loads((HERE / "events.json").read_text())
    lines = ["# Backtest summary", "", "Policy " + POL["version"] + ". Each month uses only data dated on or before that month.", "",
             "## Distressed companies", "",
             "| Company | Event (cited) | Headcount history from | v2: status at event | v2: months flagged before event | v3: status at event | v3: months flagged before event |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for co in cohort["distressed"]:
        p = OUT / f"{re.sub(r'[^a-z0-9]+', '_', co['name'].lower())}.json"
        if not p.exists():
            continue
        r = json.loads(p.read_text())
        src = co.get("event_source") or ((events.get(co["name"]) or {}).get("source") or {}).get("final_url") or ""
        lines.append(f"| {co['name']} | [{r['event_date']}]({src}) | {r['series_start'] or 'none'} | {r.get('v2_status_at_event')} | "
                     f"{r.get('v2_months_warning')} | {r.get('v3_status_at_event')} | {r.get('v3_months_warning')} |")
    lines += ["", "## Healthy controls (quarterly, Mar 2023 to Sep 2026)", "", "| Company | Points checked | v2 false alarms | v3 false alarms |", "| --- | --- | --- | --- |"]
    for co in cohort["controls"]:
        p = OUT / f"{re.sub(r'[^a-z0-9]+', '_', co['name'].lower())}.json"
        if p.exists() and not co.get("exclude"):
            r = json.loads(p.read_text())
            lines.append(f"| {co['name']} | {r['points']} | {r.get('v2_false_alarms')} | {r.get('v3_false_alarms')} |")
    lines += ["", "v2 = the live policy. v3 = v2 plus a hiring-momentum signal (joiners in the last 6 months vs the best 6 months of the "
              "prior year; red <= -65%, amber <= -50%). v3 was designed after looking at Dunzo, so Dunzo is in-sample; the other companies are the test.",
              "", "Caveats: small sample (case studies, not an accuracy figure); companies chosen with hindsight; profiles are updated late and "
              "deleted profiles are missing (survivorship); job posts, websites and news are not replayed; event dates are the earliest credible "
              "article we found, not necessarily the first ever."]
    (OUT / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main(set(sys.argv[1:]))
