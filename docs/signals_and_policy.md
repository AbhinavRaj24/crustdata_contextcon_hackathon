# Signals and policy

Files: `termsdesk/signals.py`, `termsdesk/policy.yaml`, `termsdesk/decide.py`, `termsdesk/monitor.py`.

## Interface
- `signals.collect(case_id, company)` collects the eleven signals for one company; the pipeline calls exactly this.
- `signals.collect(case_id, company, as_of=date(2023, 10, 1))` uses only what was knowable on that date (backtest).
- Steps run in parallel in groups (`TERMSDESK_PARALLEL=0` runs them one by one). Results are always sorted in `SIGNALS`
  order, so both ways give the same output.
- Evidence ids are `{case_id}-{signal}-{n}`, safe across threads.
- `decide.decide(...)` applies the fixed rules R1–R8 from `policy.yaml`; every decision stores the policy version.

## Run
```bash
.venv/bin/pytest -q tests/signals                       # signal and decision tests, no network
.venv/bin/python scripts/calibrate.py --quiet           # real companies, today (about 7-10 credits each, cached for the day)
.venv/bin/python scripts/calibrate.py --as-of 2023-10-01  # only what was knowable then
```

## Calibration fixes (false alarms on healthy companies)

| Trap | Fix |
|---|---|
| Sales titles ("Account Executive", "Key Accounts Manager") counted as finance | Finance = finance, CFO, accounts (plural), accountant, accounting, financial controller, controllership, treasury, FP&A |
| "Senior Director Finance", "Office of the CFO", "Deputy CFO" counted as the finance head | Head = CFO, Head of Finance or VP Finance only |
| "Founder's Office", "Chief of Staff", "Founder in Residence" counted as C-level | Excluded |
| Regional titles ("City CEO", "Cluster CEO", "City COO") counted as C-level | Excluded (v3.1, found at Zomato) |
| Promotions and transfers counted as exits | A new job at the same company within 62 days is an internal move |
| "CRED" matched "credit" in news | Name must appear as whole words |
| Bot protection (401, 403, 405, 406, 429) read as "website down" | Counted as up; other failures are amber after one retry |
| Zero job posts on companies that hire elsewhere | Amber or red only when joiners are weak too |
| One bad headcount point made a fake peak | 3-point rolling median before peak and drawdown |
| Small teams: 6 people leaving a 40-person team read as -15% | Drops under 10 people are ignored |
| Joiners near zero because the company moved to a new Crustdata ID | When the joiner rate is red, re-count by exact name |
| 1 or 2 C-level exits at a 1,000+ person company | Large companies: red at 4 exits, amber at 2 |
| Lean Zerodha (1.7% joiners) called a zombie, while dead companies sit at 0–0.03% | Zombie threshold 1% (v3.1) |
| Hiring momentum flagged most healthy companies live (profiles are updated late) | Windows end 2 months back (v3.2) |

## Point in time: `collect(..., as_of=T)`
- Headcount: only series points dated on or before T; the nearest must be within 120 days of T.
- Profiles: the series value at T, not today's profile count.
- Joiners and hiring momentum: job starts before T (current and past jobs); momentum windows end 2 months before T.
- Exits: jobs that ended in the 180 days up to T minus 60 days, because profiles are updated late.
- Funding: rounds announced on or before T. Age: founding year on or before T.
- Job posts: posts added in the 12 months up to T.
- Website: skipped (no history). News: the search window ends at T.
