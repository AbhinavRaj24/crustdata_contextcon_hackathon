# TermsDesk

An AI credit desk for B2B sellers. A new buyer asks for goods on credit; TermsDesk verifies the buyer is real,
sets credit terms with a cited reason for each one, sends the terms letter, and keeps watching the account.

> "Credit tools tell you how a company paid last year. Crustdata tells you what's happening to it this week."

Built at ContextCon 04 (Crustdata x YC x Techkriti, IIT Kanpur), 5–6 Nov 2026. Theme: Trust and compliance.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # add CRUSTDATA_API_KEY and GEMINI_API_KEY
.venv/bin/python -m termsdesk.cli "Need 50 laptops, Rs 20 lakh on 60 days. procurement@dunzo.com"
.venv/bin/python -m termsdesk.cli --resume <case_id>     # resume after a crash
```

Fault injection: `FAULT=enrich:timeout` · `FAULT=person_search:429` · `FAULT=enrich:garbage`. Replay without network: `REPLAY=1`.

_Full README (service, how it works, approaches, backtest, what's next) is being written: see NOTES.md for the build log._
