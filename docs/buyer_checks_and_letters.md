# Buyer checks and letters

Files: `termsdesk/intake.py`, `resolve.py`, `verify.py`, `explain.py`. Each module keeps its core functions
(`parse_request`, `resolve_company`, `verify_buyer`, `explain`) and ends with a small adapter that the pipeline calls
(`intake.parse`, `resolve.resolve`, `verify.verify`, `explain.write`).

```bash
.venv/bin/pytest -q tests/test_intake.py tests/test_resolve.py tests/test_verify.py tests/test_explain.py   # no network
.venv/bin/python scripts/try_buyer_checks.py --all --parse-only     # parse the 12 test requests, no API calls
.venv/bin/python scripts/try_buyer_checks.py --case 3               # one request, live API
```

## How each step works

- **Intake.** Rules parse first: amounts (lakh, crore, Indian digit grouping, USD refused), credit terms (only with a credit cue,
  so "deliver in 2 months" is not terms), emails, domains, sender, goods, ship-to city. Sentences that try to instruct the
  agent are removed. Gemini only fills fields the rules missed, and every value it returns must appear in the request text.
- **Resolve.** Identify by domain first; only an exact primary-domain match is trusted (Razorpay's domain list holds bitly.com).
  Then by core name, with legal words removed, so "Razorpay Software Pvt Ltd" merges into Razorpay. Crustdata has four
  companies called "Dunzo"; the largest wins only when it is at least 10x the next, otherwise a person decides.
- **Verify.** Domain check against the primary domain only: lookalikes (changed letters, extra words or hyphens,
  look-alike characters, the real domain hidden inside another) are fraud. The same name with another ending (dunzo.in)
  is unverified, since it may be the company's own. Sender check: a current employee with that name, company, title and
  "current" on the same job entry. A role mailbox at the company's own domain counts as verified, with a note. On a hold,
  the finance head is found in tiers (CFO, then head/VP/director/controller, then any finance title, then founder or CEO).
- **Impersonation check.** If the email domain belongs to a tiny company sharing its name with a much larger one
  (getdunzo.com is a real 11–50 person "Dunzo"), the case gets a `name_collision` fraud flag.
- **Explain.** The letter is built from the decision, the evidence ledger and the parsed request, never the raw text.
  Gemini may re-word it; the citation guard checks every amount, number, percentage, date and name, and a completeness
  check rejects drafts that drop the limit or a "Could not verify" line. One retry, then the template.

## Evidence rows (ids are `<case_id>:<signal>`)

| Signal | Source | Value |
|---|---|---|
| `company_identity` | company/identify #id | name, id, primary_domain, match kind |
| `exposure` | accounts ledger | outstanding, requested, exposure (INR) |
| `domain_check` | company/identify primary_domain | pass / lookalike / unverified, kind, detail |
| `sender_check` | person/search company_id + name | pass / note / unnamed / unverified, title, since |
| `finance_head` | person/search company_id + finance titles | name, title, since (only on hold) |
| `buyer_confirmation` | out-of-band check | confirmed / denied, reviewer |

## Honest limits

- Email-to-person lookup is disabled on our keys, so the domain check is what ties an email to the company.
- A sender not found by Person Search is "unverified", not fraud: missing coverage is not proof.
- The verification email names the finance head but has no real address (contact lookup is disabled);
  it always goes to the sandbox, which also keeps us inside the event rule.
- Test people in `tests/` are made up; the live demo uses a real, public employee name with all email sandboxed.
