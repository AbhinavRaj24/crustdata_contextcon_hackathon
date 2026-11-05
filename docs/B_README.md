# Module B: signals and policy (drop-in)

Unzip at the repo root. Nothing in A's files is changed (`crustdata.py`, `store.py`, `pipeline.py`, `outbox.py`, `app.py`,
`contracts.py`). `decide.py` and `monitor.py` are unchanged too.

| File | What |
|---|---|
| `termsdesk/signals.py` | Rewritten: false-alarm fixes, point in time (`as_of`), steps run in parallel |
| `termsdesk/policy.yaml` | v3: a few new keys, all old values kept |
| `tests/conftest.py`, `tests/fakes.py` | Temp DB per test, no model calls, fixed "today", a fake Crustdata client that runs the real filter trees |
| `tests/test_signals.py`, `tests/test_decide.py`, `tests/test_calibrate_script.py` | 63 tests, no network, no key |
| `scripts/calibrate_b.py` | Runs real companies through `collect` + `decide` and prints the NOTES table |
| `NOTES_B.md` | Paste at the end of NOTES.md (kept separate so unzipping can't overwrite your NOTES.md) |

## Interface
- `signals.collect(case_id, company)` works as before, so `pipeline.py` needs no change.
- Backtest: `signals.collect(case_id, company, as_of=date(2023, 10, 1))` uses only what was knowable on that date.
- `parallel=False`, or `TERMSDESK_PARALLEL=0` in the environment, runs the steps one by one (for debugging).
  Results are always sorted in `SIGNALS` order, so both ways give the same output.
- B's evidence IDs are now `{case_id}-{signal}-{n}`, set by B and safe across threads.
- Decisions now record policy `v3`.

## Run
```bash
python -m pytest -q                                    # 63 passed
python scripts/calibrate_b.py --quiet                  # real companies, today
python scripts/calibrate_b.py --as-of 2023-10-01       # backtest: Dunzo #5721 should be flagged
```

## For A (your file, not changed by B)
`store.add_evidence` builds IDs from the row count and writes with `INSERT OR REPLACE`. After `clear_evidence` the count drops,
so a later count-based ID can overwrite an older verify row that a decision still cites. B no longer uses count-based IDs;
verify could pass explicit ones too (for example `f"{case_id}-verify-{n}"`).
