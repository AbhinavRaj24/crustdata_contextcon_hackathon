# Build notes (what we tried, what worked, what didn't)

## Data findings (checked live, 6 Nov 2026)
- Dunzo looks alive in summary fields: status "active", ~2,400 headcount, API growth "+8% yoy", 1,490 people list it as employer.
- Raw signals show it is dead: 0 joiners in 12 months (Razorpay: 829), 0 job posts (Razorpay: 202 in 12 months), website 502/504.
- Headcount series peaked at 3,422 (Dec 2022), fell 24% by Dec 2023, then flatlined: profiles go stale, so headcount alone misses dead companies.
- Summary fields are unreliable: growth % disagrees with the series, enrich `people` lists unrelated people (and is 4.2M chars for Razorpay),
  revenue estimate for Dunzo is "$1B-$1T", Razorpay's domain list includes bitly.com.
- Start dates are month-level, so windows start on the 1st of a month.
- Disabled on our keys: email->person lookup, contact enrich, batch endpoints. So the sender check is name + company, and the domain check ties the email to the company.

## Approaches
| Approach | Verdict | Why |
|---|---|---|
| A model judges creditworthiness | Rejected | Not reproducible, not citable |
| Headcount level alone | Rejected | Dead companies flatline (Dunzo) |
| Workforce flows + exits + fixed rules | Chosen | Fresh, citable, testable on past dates |
| Verify by replying to the sender | Rejected | Fraudster controls that inbox |
| Verify via an independently found finance head | Chosen | Out of band |

## Model
Gemini only for language (parsing, news classification, letter wording). `gemini-3.5-flash` and `gemini-flash-latest`
returned 503 under load and one call hung 76 s, so: chain flash-lite -> 2.5-flash -> rules, 15 s timeout, temperature 0, cached.

## Log
- 01:40 First real end-to-end case: Dunzo -> advance payment only (R3 zombie), letter passed the citation guard, ~7 credits, 97 s.
- 01:50 Purchase-order fraud case: razorpay-payments.co flagged as a lookalike; held with no credit; verification routed (sandbox) to Razorpay's Head of Core Finance, found independently. 5 s, ~2 credits.
- 01:55 FALSE ALARM found: healthy Razorpay got "advance only". Causes: (1) "Senior Director Finance left" counted as the finance head leaving;
  (2) 7 finance exits counted as red, but that is 6% of a ~118-person finance team (normal attrition); (3) funding 54 months ago counted as amber
  although Razorpay is growing; (4) the website check loaded a bit.ly link from Crustdata's website field.
  Fixes: finance head = CFO / Head of Finance / VP Finance only; finance exits measured as a share of the team; funding age counts only when
  shrinking or nobody joining; website checked on the primary domain. Result: Razorpay approved Rs 30 lakh on 45 days; Dunzo still advance-only.

## Calibration (policy v3)

False alarms on healthy companies came from these traps. Each fix has a test in `tests/signals/test_signals.py`.

| Trap | Fix |
|---|---|
| Sales titles ("Account Executive", "Key Accounts Manager") counted as finance | Finance = finance, CFO, accounts (plural), accountant, accounting, financial controller, controllership, treasury, FP&A; sales "account" titles excluded |
| "Senior Director Finance", "Office of the CFO", "Deputy CFO" counted as the finance head | Head = CFO, Head of Finance or VP Finance only; assistant, associate, deputy and "office of" excluded |
| "Founder's Office", "Chief of Staff", "Founder in Residence" counted as C-level | Excluded |
| Promotions and transfers counted as exits | A new job at the same company within 62 days is an internal move and is skipped |
| "CRED" matched "credit" in news | Name must appear as whole words |
| 403 or 429 from bot protection read as "website down" | Bot-block codes (401, 403, 405, 406, 429) count as up; other failures are amber, after one retry when the error is retryable |
| Zero job posts flagged on companies that hire elsewhere | Zero posts is amber or red only when joiners are weak too; no post history at all = could not verify |
| One bad headcount point made a fake peak | 3-point rolling median before peak and drawdown |
| Small teams: 6 people leaving a 40-person team read as -15% | Drops under 10 people are ignored |
| Joiners near zero because the company moved to a new Crustdata ID | When the joiner rate is red, re-count by exact company name; if that rate is 6% or more, amber plus "conflict" evidence |
| 1 or 2 C-level exits at a 1,000+ person company (normal churn) | Large companies: red at 4 exits, amber at 2 |
| Evidence IDs collided when signals ran in parallel | Explicit IDs `{case}-{signal}-{n}` |

### Point in time: `collect(case_id, company, as_of=T)`
- Headcount: only series points dated on or before T; the nearest one must be within 120 days of T, otherwise could not verify.
- Profiles: the series value at T, not today's profile count.
- Joiners: people whose job at the company started in the 12 months before T (current and past jobs).
- Exits: jobs that ended in the 180 days up to T minus 60 days, because profiles are updated late.
- Funding: rounds announced on or before T. Age: founding year on or before T.
- Job posts: posts added in the 12 months up to T.
- Website: skipped (no history). News: the search window ends at T.

Reference: the Dunzo-shaped test company at 2023-10-01 gets joiner rate red, headcount amber and finance team exits red, so R3 advance only.
The finance head's exit (Sep 2023) shows up only from Dec 2023, because of the 60-day lag.

### Real companies
`python scripts/calibrate.py --write-notes` (today) and `python scripts/calibrate.py --as-of 2023-10-01 --write-notes` (backtest)
append a table here: company, profiles, joiner rate, job posts, decision, right or wrong (about 7-10 credits per company, cached for the day).

## Build log (continued)
- 02:40 Calibrated signals in place: point-in-time `collect(..., as_of=T)`, parallel steps, policy v3, signal tests in tests/signals/.
  store.add_evidence now gives random ids to rows without one: count-based ids could be reused after clear_evidence and overwrite a
  row an older decision still cites.
- Full suite: 210 offline tests and 13 live end-to-end tests pass.

### Calibration run (live, 2026-11-06, policy v3.1, ask Rs 20 lakh on 45 days)

| Company | Label | Profiles | Joiners 12m | Job posts 12m | Red / amber | Could not verify | Decision | Right? | Credits |
|---|---|---|---|---|---|---|---|---|---|
| Razorpay #897127 | healthy | 3582 | 829 (23.1%) | 202 | none | none | approve Rs 20 lakh / 45d (R8 clean) | right | 4.2 |
| Zerodha #7275 | healthy | 2113 | 36 (1.7%) | 0 | joiner_rate amber, finance_team_exits amber, job_posts amber | funding_age | reduce Rs 20 lakh / 30d (R7 two or more amber signals) | right (caution) | 3.8 |
| Freshworks #6042422 | healthy | 7531 | 1024 (13.6%) | 1388 | none | none | approve Rs 20 lakh / 45d (R8 clean) | right | 8.0 |
| Swiggy #5702 | healthy | 16466 | 2173 (13.2%) | 2030 | none | none | approve Rs 20 lakh / 45d (R8 clean) | right | 10.0 |
| Zomato #6162764 | healthy | 13434 | 1245 (9.3%) | 1 | none | none | approve Rs 20 lakh / 45d (R8 clean) | right | 10.0 |
| Groww #5894 | healthy | 2119 | 437 (20.6%) | 97 | none | website | approve Rs 20 lakh / 45d (R8 clean) | right | 5.0 |
| Meesho #5300979 | healthy | 6300 | 1101 (17.5%) | 306 | none | none | approve Rs 20 lakh / 45d (R8 clean) | right | 9.0 |
| PhonePe #5729 | healthy | 9909 | 1021 (10.3%) | 540 | none | none | approve Rs 20 lakh / 45d (R8 clean) | right | 10.0 |
| Dunzo #5721 | distressed | 1490 | 0 (0.0%) | 0 | joiner_rate red, funding_age amber | job_posts | advance_only Rs 0 / 0d (R3 zombie (nobody joining)) | right | 1.7 |
| BYJU'S #5705 | distressed | 10682 | 3 (0.0%) | 0 | joiner_rate red, job_posts red, distress_news red, funding_age amber | none | advance_only Rs 0 / 0d (R3 zombie (nobody joining)) | right | 7.0 |
| Koo App USA #609146 | distressed | 0 | 0 (0.0%) | 0 | none | profiles, joiner_rate, funding_age, job_posts | reduce Rs 1 lakh / 15d (R5 thin data) | thin data | 5.2 |
| BluSmart #6214 | distressed | 738 | 0 (0.0%) | 0 | joiner_rate red, job_posts red, headcount_trend amber, funding_age amber | none | advance_only Rs 0 / 0d (R3 zombie (nobody joining)) | right | 4.6 |
| Builder.ai #632808 | distressed | 471 | 0 (0.0%) | 0 | joiner_rate red, job_posts red, headcount_trend amber, funding_age amber, website amber | none | advance_only Rs 0 / 0d (R3 zombie (nobody joining)) | right | 4.0 |

12 of 12 right. False alarms: none. Missed: none.

- 02:55 First live calibration (policy v3): 10 of 12 right. Zomato false alarm: "Cluster CEO", "City CEO", "City COO" counted as
  C-level exits -> regional titles excluded. Zerodha false alarm: 1.7% joiners called a zombie, while every dead company sat at
  0.0-0.03% -> zombie threshold 2% -> 1% (policy v3.1). Re-run: 12 of 12 right; Zerodha gets the full amount on 30 days (caution).
- 03:15 Hiring momentum added to the live policy (v3.2). First live calibration with it: 11 of 12 (PhonePe red) and most healthy
  companies amber: profiles are updated late, so the latest 6 months always look short of joiners. In the backtest, past windows
  seen today are complete, so it hid this. Fix: windows end 2 months back (same as the 60-day exit lag), live and in the backtest.
  Calibration back to 12 of 12. Honest backtest (v3.2 with lag): 5 of 6 collapses flagged early (2 of 6 without momentum);
  healthy controls 9 of 56 flagged (7 of 56 without), all reduced limits, never refusals.
- 04:25 Same Zepto request gave hold (04:01) and approve (04:05, 04:13): not randomness but code changes in between (the zeptonow.com
  domain fix and the Rs 4 lakh cap), while every decision still said "policy v3.2". Lesson: any change to decision rules bumps the
  policy version, so a reviewer can always tell why two decisions differ. Now policy v3.3.
