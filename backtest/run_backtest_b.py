"""Backtest on the LIVE signal code: signals.collect(..., as_of=T) from Module B, month by month.

Same cohort, events and metrics as run_backtest.py (which re-implemented the first, pre-calibration rules).
Comparing the two shows what B's false-alarm fixes changed. News is not replayed (search results carry no
reliable dates for the past); websites cannot be fetched as they were. Both count as "could not verify".

Bands mirror decide.py rules R3-R8. "+momentum" adds the hiring-momentum candidate (see run_backtest.py).
Usage: python -m backtest.run_backtest_b [Company ...]  -> backtest/results_b/*.json + summary.md
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

import yaml

from termsdesk import config, signals
from termsdesk.contracts import Company
from termsdesk.crustdata import Unavailable, client, current_case

from .run_backtest import MOMENTUM, d, joins, month_start, months_between

HERE = Path(__file__).parent
OUT = HERE / "results_b"
POL = yaml.safe_load(config.POLICY_PATH.read_text())

# Backtest only: no news replay (cost, and dates are unreliable for the past).
signals.GROUPS_PIT = [[fn for fn in g if fn is not signals.s_news] for g in signals.GROUPS_PIT]


def band(levels: dict) -> str:
    reds = [k for k, v in levels.items() if v == "red"]
    ambers = [k for k, v in levels.items() if v == "amber"]
    if levels.get("joiner_rate") == "red":
        return "advance_only (R3)"
    if len(reds) >= 2:
        return "advance_only (R4)"
    if levels.get("profiles") == "na" or (levels.get("headcount_trend", "na") == "na" and levels.get("joiner_rate", "na") == "na"):
        return "thin_data (R5)"
    if len(reds) == 1:
        return "reduce_25 (R6)"
    if len(ambers) >= 2:
        return "reduce_50 (R7)"
    return "approve (R8)"


def momentum(cid: int, T: dt.date) -> str:
    try:
        t0 = month_start(T, 0)
        w0, w1, w2 = (joins(cid, month_start(T, 6), t0), joins(cid, month_start(T, 12), month_start(T, 6)),
                      joins(cid, month_start(T, 18), month_start(T, 12)))
    except Unavailable:
        return "na"
    best = max(w1, w2)
    if best < 20:
        return "na"
    m = w0 / best - 1
    return "red" if m <= MOMENTUM["red_at_or_below"] else "amber" if m <= MOMENTUM["amber_at_or_below"] else "ok"


def run(name: str, domain: str, months: list[dt.date], event: dt.date | None) -> dict:
    m = client().identify(domain=domain) or client().identify(name=name)
    top = max(m, key=lambda x: x["confidence_score"])["company_data"]
    cid = top["crustdata_company_id"]
    company = Company(crustdata_id=cid, name=top["basic_info"]["name"], primary_domain=top["basic_info"].get("primary_domain"))
    rows = []
    for T in months:
        case_id = f"bt-{cid}-{T.isoformat()}"
        token = current_case.set(case_id)
        try:
            res = signals.collect(case_id, company, as_of=T)
        finally:
            current_case.reset(token)
        levels = {r.signal: r.level for r in res}
        mom = momentum(cid, T)
        row = {"T": T.isoformat(), "levels": levels, "summaries": {r.signal: r.summary for r in res},
               "band": band(levels), "band_momentum": band({**levels, "hiring_momentum": mom}), "momentum": mom}
        rows.append(row)
        flags = " ".join(f"{k[:10]}={v}" for k, v in levels.items() if v in ("red", "amber"))
        print(f"  {row['T']}  policy={row['band']:18} +momentum={row['band_momentum']:18} {flags}", flush=True)
    res = {"name": name, "crustdata_id": cid, "event_date": event.isoformat() if event else None, "rows": rows}
    for ver, key in (("policy", "band"), ("momentum", "band_momentum")):
        if event:
            before = [r for r in rows if d(r["T"]) <= event]
            streak = 0
            for r in reversed(before):
                if r[key].startswith(("approve", "thin")):
                    break
                streak += 1
            res[f"{ver}_months_flagged"] = streak
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
    for co in cohort["distressed"]:
        if (only and co["name"] not in only) or co.get("exclude"):
            continue
        ev = d(co.get("event_date") or (events.get(co["name"]) or {}).get("event_date"))
        if not ev:
            continue
        print(f"== {co['name']} (event {ev})", flush=True)
        run(co["name"], co["domain"], [month_start(ev, k) for k in range(12, -1, -1)], ev)
    for co in cohort["controls"]:
        if (only and co["name"] not in only) or co.get("exclude"):
            continue
        print(f"== {co['name']} (control)", flush=True)
        run(co["name"], co["domain"], [dt.date(y, m, 1) for y in (2023, 2024, 2025, 2026) for m in (3, 9)], None)
    write_summary()


def write_summary() -> None:
    cohort = yaml.safe_load((HERE / "cohort.yaml").read_text())
    events = json.loads((HERE / "events.json").read_text())
    old = HERE / "results"
    lines = ["# Backtest on the live signal code", "",
             f"Policy {POL['version']} via `signals.collect(..., as_of=T)`: each month sees only data dated on or before it. "
             "News and websites are not replayed.", "",
             "## Distressed companies (12 months before the cited event)", "",
             "| Company | Event (cited) | Policy: status at event | Policy: months flagged before event | + momentum: status | + momentum: months flagged |",
             "| --- | --- | --- | --- | --- | --- |"]
    for co in cohort["distressed"]:
        p = OUT / f"{re.sub(r'[^a-z0-9]+', '_', co['name'].lower())}.json"
        if not p.exists():
            continue
        r = json.loads(p.read_text())
        src = co.get("event_source") or ((events.get(co["name"]) or {}).get("source") or {}).get("final_url") or ""
        lines.append(f"| {co['name']} | [{r['event_date']}]({src}) | {r['policy_status_at_event']} | {r['policy_months_flagged']} | "
                     f"{r['momentum_status_at_event']} | {r['momentum_months_flagged']} |")
    lines += ["", "## Healthy controls (every six months, Mar 2023 to Sep 2026)", "",
              "| Company | Points | Policy false alarms | + momentum false alarms | First backtest (pre-calibration rules) |",
              "| --- | --- | --- | --- | --- |"]
    for co in cohort["controls"]:
        p = OUT / f"{re.sub(r'[^a-z0-9]+', '_', co['name'].lower())}.json"
        if not p.exists() or co.get("exclude"):
            continue
        r = json.loads(p.read_text())
        o = old / p.name
        before = ""
        if o.exists():
            ro = json.loads(o.read_text())
            before = f"{ro.get('v2_false_alarms')} of {ro.get('points')} (quarterly)"
        lines.append(f"| {co['name']} | {r['points']} | {r['policy_false_alarms']} | {r['momentum_false_alarms']} | {before} |")
    lines += ["", "Caveats: small sample (case studies, not an accuracy figure); companies chosen with hindsight; profiles are "
              "updated late and deleted profiles are missing (survivorship); event dates are the earliest credible article we "
              "found. The momentum signal was designed after looking at Dunzo, so Dunzo is in-sample for it."]
    (OUT / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main(set(sys.argv[1:]))
