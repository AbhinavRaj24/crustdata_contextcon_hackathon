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
