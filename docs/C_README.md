# TermsDesk · Part C (intake, identity and words)

Owner C. Files: `termsdesk/intake.py`, `resolve.py`, `verify.py`, `explain.py`.
`termsdesk/contracts.py` is the spec's contract, field for field. **If A already committed one, keep A's.**

```
python -m pytest -q                       # 109 tests, no network, no model key needed
python scripts/c_try.py --all --parse-only
CRUSTDATA_API_KEY=... python scripts/c_try.py --case 3      # live; saves replies to fixtures/c/
python scripts/c_try.py --case 3 --replay                     # from the saved replies
```

## How A's pipeline calls Part C

```python
from termsdesk.intake import parse_request
from termsdesk.resolve import resolve_company, sqlite_lookup, save_company
from termsdesk.verify import verify_buyer, apply_verification_answer
from termsdesk.explain import explain

p = parse_request(raw_text, case_id)                       # PARSED
if p.needs_company: ask(p.question)                          # the only question intake ever asks
req = p.request                                              # store req.model_dump()

res = resolve_company(client, req, lookup=sqlite_lookup(conn), case_date=today)   # RESOLVED
if res.needs_human: human_queue(case_id, res.reason)         # ambiguous, not found or API down
save_company(conn, res); ledger += res.evidence              # company_identity, exposure

v = verify_buyer(client, req, res, case_date=today, hold_min_inr=policy["hold_unverified_min_inr"])
ledger += v.evidence                                         # domain_check, sender_check, finance_head
if v.hold:                                                   # HOLD_VERIFY
    outbox.send_once(v.verification_email)                   # key c3:verify:v1, to the sandbox
# ... B: signals + decide ...
out = explain(decision, ledger, req, res.name, case_id=case_id, version=decision_version, credits=spent)
if out.letter: outbox.send_once(out.letter)                  # key c42:terms:v2, to the sandbox
case_page(out.note)                                          # headline, reasons with sources, gaps, footer

# Confirm / Deny button on the case page:
r = apply_verification_answer(case_id, "confirm", reviewer, sender_domain)   # VERIFIED or DECLINED
```

`client` is A's Crustdata client. Part C only calls `client.post(path, body)` and expects the parsed JSON.
If A's method has another name, pass a callable instead: `lambda path, body: crustdata.call(path, body)`.
Errors raised by the client are caught and become `unavailable` evidence, never a guess.

## What B's decide.py reads from Part C

- Row 1 (fraud): `v.fraud_flags` is non-empty (`lookalike_domain:<kind>`, `name_collision`).
- Row 2 (unverified ≥ ₹5 lakh): `not v.buyer_verified and amount >= 500000`.
  `verify.hold_decision()` implements rows 1–2, so decide can call it and stay in step.
- Exposure: `res.exposure_inr` (outstanding on the account + this request).
- Suggestion: when the amount is missing and the buyer is unverified, cap the "maximum safe limit" at ₹5 lakh.

## Evidence rows Part C writes (ids are `<case_id>:<signal>`)

| Signal | Source | Value |
|---|---|---|
| `company_identity` | company/identify #id | name, id, primary_domain, match kind |
| `exposure` | accounts ledger | outstanding, requested, exposure (INR) |
| `domain_check` | company/identify primary_domain | pass / lookalike / unverified, kind, detail |
| `sender_check` | person/search company_id + name | pass / note / unverified, title, since |
| `finance_head` | person/search company_id + finance titles | name, title, since (only on hold) |
| `buyer_confirmation` | out-of-band check | confirmed / denied, reviewer |

## Where Part C differs from the spec, and why (from live checks on 6 Nov)

1. **Finance head query.** The cookbook query (title "finance" + seniority) missed Razorpay's CFO, whose title is
   "Chief Financial Officer". Part C asks for CFO titles first, then head/VP/director/controller, then any
   finance title, then founder/CEO as a labelled fallback. It stops at the first tier that finds someone.
2. **Duplicate company names.** Identify by name returns four companies called "Dunzo", all at confidence 1.0.
   Among exact core-name matches, the largest wins only if it is at least 10x the next; otherwise a human decides.
   The "within 0.1 → human" rule still applies to fuzzy matches.
3. **Legal names.** "Razorpay Software Pvt Ltd" scores 0.6, tied with "Metacube Software Pvt Ltd" and others.
   Part C compares core names (legal words like Pvt, Ltd, Software removed), so it merges into Razorpay.
4. **Different ending (dunzo.in vs dunzo.com).** Flagged as a lookalike, as the spec's unit test requires for
   razorpay.co, so the order is held and verified through the finance head. A real company using a regional
   domain is verified, not turned away. Unrelated domains stay "unverified", never fraud.
5. **Impersonation check (added).** If the email domain belongs to a tiny company that shares its name with a
   much larger one (getdunzo.com is a real 11–50 person "Dunzo"), the case gets a `name_collision` fraud flag.
   It costs one free identify call, made only in that situation.
6. **Person Search fields.** Requests send `"fields": ["basic_profile", "experience"]` (whole sections, about
   0.13 credits per result). On a 400 the call is retried once without `fields`.
7. **Model use.** Rules parse first. A model (Haiku) only fills fields the rules missed, and every value must appear
   in the request text. Amounts and terms are always re-derived by the normaliser. Letters come from templates
   unless `ANTHROPIC_API_KEY` is set; model drafts must pass the citation guard (one retry, then the template).
   Set `TERMSDESK_USE_MODEL=0` to force rules and templates.

## Honest limits

- Email-to-person lookup is disabled on our keys, so the domain check is what ties an email to the company.
- A sender not found by Person Search is "unverified", not fraud: missing coverage is not proof.
- The verification email names the finance head but has no real address (contact lookup is disabled);
  it always goes to the sandbox, which also keeps us inside the event rule.
- Test people in `tests/` are made up. For the live case 1, D swaps in the real employee chosen for the demo.
