# Backtest on the live signal code

Policy v3.2 via `signals.collect(..., as_of=T)`: each month sees only data dated on or before it. News and websites are not replayed. (v3.3 changed only the buyer checks, not the signals, so these results stand.)

## Distressed companies (12 months before the cited event)

| Company | Event (cited) | Policy (with momentum): status at event | months flagged before event | Without momentum: status | months flagged |
| --- | --- | --- | --- | --- | --- |
| Dunzo | [2023-07-20](https://editorji.com/amp/business-news/more-trouble-for-dunzo-legal-notices-by-facebook-nilenso-over-unpaid-dues-1689853237898) | reduce_25 (R6) | 4 | approve (R8) | 0 |
| BYJU'S | [2023-06-27](https://timesofindia.indiatimes.com/business/india-business/byjus-ex-staff-allege-pf-unpaid-company-says-no-dues/articleshow/101293728.cms) | reduce_25 (R6) | 2 | approve (R8) | 0 |
| GoMechanic | [2023-02-13](https://m.economictimes.com/tech/startups/how-founders-threw-a-spanner-in-the-works-of-gomechanic/articleshow/97844373.cms) | approve (R8) | 0 | approve (R8) | 0 |
| BluSmart | [2025-06-03](https://www.livemint.com/companies/news/nclt-issues-notices-to-blusmart-mobility-and-gensol-on-three-fresh-insolvency-pleas-11748945362132.html) | reduce_25 (R6) | 2 | reduce_25 (R6) | 2 |
| Koo | [2024-07-03](https://techcrunch.com/2024/07/02/indian-social-network-koo-to-shut-down/) | reduce_25 (R6) | 7 | approve (R8) | 0 |
| Bira 91 | [2025-02-19](https://www.business-standard.com/companies/news/bira-b9-beverages-tax-trouble-salary-vendor-payment-delays-financial-crisis-125021900251_1.html) | reduce_25 (R6) | 1 | reduce_25 (R6) | 1 |

## Healthy controls (every six months, Mar 2023 to Sep 2026)

| Company | Points | Policy (with momentum) false alarms | Without momentum | First backtest (pre-calibration rules) |
| --- | --- | --- | --- | --- |
| Razorpay | 8 | 0 | 0 | 7 of 15 (quarterly) |
| Zerodha | 8 | 4 | 3 | 5 of 15 (quarterly) |
| Groww | 8 | 0 | 0 | 6 of 15 (quarterly) |
| Zepto | 8 | 2 | 1 | 10 of 15 (quarterly) |
| PhysicsWallah | 8 | 1 | 1 | 8 of 15 (quarterly) |
| CRED | 8 | 0 | 0 | 3 of 15 (quarterly) |
| Freshworks | 8 | 2 | 2 | 9 of 15 (quarterly) |

Caveats: small sample (case studies, not an accuracy figure); companies chosen with hindsight; profiles are updated late and deleted profiles are missing (survivorship); event dates are the earliest credible article we found. The momentum signal was designed after looking at Dunzo, so Dunzo is in-sample for it.
