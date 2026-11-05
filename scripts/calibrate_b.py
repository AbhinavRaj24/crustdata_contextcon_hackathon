"""Calibrate Module B on real companies, with your own key (CRUSTDATA_API_KEY in .env).

  python scripts/calibrate_b.py                          # default cohort, as of today
  python scripts/calibrate_b.py --as-of 2023-10-01       # only what was knowable then (backtest cohort)
  python scripts/calibrate_b.py --only Dunzo,Razorpay    # a subset of the cohort
  python scripts/calibrate_b.py --add "Acme:acme.com:healthy" --add "Oldco:#1234:distressed"
  python scripts/calibrate_b.py --write-notes            # also append the table to NOTES.md

Each company costs about 7-10 credits the first time; a re-run on the same day is served from the cache.
The run stops before a company that would take the total over --max-credits (default 120).
Labels ("healthy" / "distressed") are our own reading of each company; check them before trusting a score.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from termsdesk import decide, resolve, signals, store  # noqa: E402
from termsdesk.contracts import Company, Request, VerifyResult  # noqa: E402
from termsdesk.crustdata import CrustdataError, OutOfCredits, current_case  # noqa: E402
from termsdesk.money import inr  # noqa: E402

# (name, domain or #crustdata_id, label today)
LIVE = [
    ("Razorpay", "razorpay.com", "healthy"), ("Zerodha", "zerodha.com", "healthy"),
    ("Freshworks", "freshworks.com", "healthy"), ("Swiggy", "swiggy.com", "healthy"),
    ("Zomato", "zomato.com", "healthy"), ("Groww", "groww.in", "healthy"),
    ("Meesho", "meesho.com", "healthy"), ("PhonePe", "phonepe.com", "healthy"),
    ("Dunzo", "#5721", "distressed"), ("Byju's", "byjus.com", "distressed"),
    ("Koo", "kooapp.com", "distressed"), ("BluSmart", "blu-smart.com", "distressed"),
    ("Builder.ai", "builder.ai", "distressed"),
]
# (name, target, label in late 2023) for --as-of runs
PAST = [
    ("Dunzo", "#5721", "distressed"), ("Byju's", "byjus.com", "distressed"),
    ("Razorpay", "razorpay.com", "healthy"), ("Zerodha", "zerodha.com", "healthy"),
    ("Freshworks", "freshworks.com", "healthy"),
]
ASK_INR, ASK_DAYS = 2_000_000, 45          # a typical first order: Rs 20 lakh on 45 days


def find(name: str, target: str) -> Company:
    if target.startswith("#"):
        return Company(crustdata_id=int(target[1:]), name=name, matched_on="id")
    req = Request(case_id="calibrate", raw_text="", company_name=name, company_domain=target or None)
    return resolve.resolve(req)


def verdict(label: str, d) -> str:
    rule = d.rule[:2]
    if rule == "R5":
        return "thin data"
    alarm = rule in ("R3", "R4", "R6")      # at least one red signal
    if label == "healthy":
        return "FALSE ALARM" if alarm else ("right (caution)" if rule == "R7" else "right")
    return "right" if alarm else ("partly (amber only)" if rule == "R7" else "MISSED")


def value(case_id: str, signal: str) -> dict:
    for e in store.list_evidence(case_id):
        if e.signal == signal and isinstance(e.value, dict) and e.status != "conflict":
            return e.value
    return {}


def run_one(name: str, target: str, label: str, as_of: dt.date | None) -> dict:
    slug = "".join(ch for ch in name.lower() if ch.isalnum())
    case_id = f"cal-{slug}-{as_of.isoformat() if as_of else 'live'}"
    token = current_case.set(case_id)
    try:
        try:
            company = find(name, target)
        except (resolve.NeedsHuman, CrustdataError) as e:
            if isinstance(e, OutOfCredits):
                raise
            return {"name": name, "label": label, "error": f"not resolved: {e}"}
        results = signals.collect(case_id, company, as_of=as_of)
        req = Request(case_id=case_id, raw_text="", company_name=company.name, amount_inr=ASK_INR, terms_days=ASK_DAYS)
        d = decide.decide(req, company, VerifyResult(status="pass"), results, None, case_id=case_id)
    finally:
        current_case.reset(token)
    jr, jp = value(case_id, "joiner_rate"), value(case_id, "job_posts")
    return {
        "name": f"{company.name} #{company.crustdata_id}", "label": label,
        "profiles": value(case_id, "profiles").get("by_company_id"),
        "joiners": f"{jr['joiners']} ({jr['rate']:.1%})" if "rate" in jr else "n/a",
        "job_posts": jp.get("count", "n/a"),
        "flags": ", ".join([f"{r.signal} red" for r in results if r.level == "red"]
                           + [f"{r.signal} amber" for r in results if r.level == "amber"]) or "none",
        "unknown": ", ".join(r.signal for r in results if r.level == "na") or "none",
        "decision": f"{d.decision} {inr(d.limit_inr)} / {d.terms_days}d ({d.rule.split(';')[0]})",
        "verdict": verdict(label, d), "credits": round(store.case_cost(case_id), 1),
        "lines": [f"{r.level:5} {r.signal:18} {r.summary}" for r in results],
    }


def table(rows: list[dict], as_of: dt.date | None) -> str:
    when = f"as of {as_of}" if as_of else f"live, {dt.date.today()}"
    out = [f"### Calibration run ({when}, policy {decide.policy()['version']}, ask {inr(ASK_INR)} on {ASK_DAYS} days)",
           "", "| Company | Label | Profiles | Joiners 12m | Job posts 12m | Red / amber | Could not verify | Decision | Right? | Credits |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if "error" in r:
            out.append(f"| {r['name']} | {r['label']} | | | | | | {r['error']} | not run | 0 |")
            continue
        out.append(f"| {r['name']} | {r['label']} | {r['profiles'] if r['profiles'] is not None else 'n/a'} | {r['joiners']} | "
                   f"{r['job_posts']} | {r['flags']} | {r['unknown']} | {r['decision']} | {r['verdict']} | {r['credits']} |")
    scored = [r for r in rows if "error" not in r and r["verdict"] != "thin data"]
    good = sum(r["verdict"].startswith("right") for r in scored)
    out += ["", f"{good} of {len(scored)} right. False alarms: "
            + (", ".join(r["name"] for r in scored if r["verdict"] == "FALSE ALARM") or "none")
            + ". Missed: " + (", ".join(r["name"] for r in scored if r["verdict"] == "MISSED") or "none") + "."]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--as-of", type=dt.date.fromisoformat, help="point in time, YYYY-MM-DD")
    ap.add_argument("--only", help="comma-separated names from the cohort")
    ap.add_argument("--add", action="append", default=[], help='extra company "name:domain-or-#id:healthy|distressed"')
    ap.add_argument("--max-credits", type=float, default=120)
    ap.add_argument("--write-notes", action="store_true", help="append the table to NOTES.md")
    ap.add_argument("--quiet", action="store_true", help="table only, no per-signal lines")
    a = ap.parse_args()

    cohort = list(PAST if a.as_of else LIVE)
    for extra in a.add:
        name, target, label = (extra.split(":") + ["", ""])[:3]
        cohort.append((name, target, label or "healthy"))
    if a.only:
        keep = {n.strip().lower() for n in a.only.split(",")}
        cohort = [c for c in cohort if c[0].lower() in keep]

    rows, spent = [], 0.0
    for name, target, label in cohort:
        if spent + 10 > a.max_credits:
            print(f"Stopping before {name}: about {spent:.0f} credits used (cap {a.max_credits:.0f}).")
            break
        try:
            row = run_one(name, target, label, a.as_of)
        except OutOfCredits:
            print("Out of Crustdata credits; table below is partial.")
            break
        rows.append(row)
        spent += row.get("credits", 0)
        print(f"\n== {row['name']} ({label}): {row.get('verdict', row.get('error'))}")
        if not a.quiet:
            for line in row.get("lines", []):
                print("   ", line)

    md = table(rows, a.as_of)
    print("\n" + md)
    if a.write_notes and rows:
        notes = ROOT / "NOTES.md"
        with notes.open("a") as f:
            f.write("\n\n" + md + "\n")
        print(f"\nAppended to {notes}")


if __name__ == "__main__":
    main()
