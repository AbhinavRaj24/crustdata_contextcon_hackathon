# Backtest summary

Policy v2. Each month uses only data dated on or before that month.

## Distressed companies

| Company | Event (cited) | Headcount history from | v2: status at event | v2: months flagged before event | v3: status at event | v3: months flagged before event |
| --- | --- | --- | --- | --- | --- | --- |
| Dunzo | [2023-07-20](https://www.google.com/goto?url=CAESlQEBxCJF154SatXjCZGXOHuzt5bD8DGDkOlxudhf3bYqoPNLyPrQQSjuBhp-V-htfK7Q1AVJbaYRvg0TFS_PwqJJVITKOr7RnXb7rk2BzZY5v5_iH1_XWcJfNQic5h7P95Q51y-VrPso93yfJmmaUEizSW5OQf-WcFe4v5qKEg51dzUNpvur_Cuhgr-1bYWgNAUH9J5jEQ) | 2019-10-28 | approve (R8) | 0 | reduce_25 (R6) | 8 |
| BYJU'S | [2023-06-27](https://timesofindia.indiatimes.com/business/india-business/byjus-ex-staff-allege-pf-unpaid-company-says-no-dues/articleshow/101293728.cms) | 2023-02-27 | reduce_25 (R6) | 4 | advance_only (R4) | 4 |
| GoMechanic | [2023-02-13](https://m.economictimes.com/tech/startups/how-founders-threw-a-spanner-in-the-works-of-gomechanic/articleshow/97844373.cms) | 2023-01-30 | approve (R8) | 0 | approve (R8) | 0 |
| BluSmart | [2025-06-03](https://www.livemint.com/companies/news/nclt-issues-notices-to-blusmart-mobility-and-gensol-on-three-fresh-insolvency-pleas-11748945362132.html) | 2019-10-28 | advance_only (R4) | 3 | advance_only (R4) | 3 |
| Koo | [2024-07-03](https://techcrunch.com/2024/07/02/indian-social-network-koo-to-shut-down/) | 2023-05-01 | approve (R8) | 0 | reduce_25 (R6) | 7 |
| Bira 91 | [2025-02-19](https://www.business-standard.com/companies/news/bira-b9-beverages-tax-trouble-salary-vendor-payment-delays-financial-crisis-125021900251_1.html) | 2019-10-28 | reduce_25 (R6) | 1 | reduce_25 (R6) | 1 |

## Healthy controls (quarterly, Mar 2023 to Sep 2026)

| Company | Points checked | v2 false alarms | v3 false alarms |
| --- | --- | --- | --- |
| Razorpay | 15 | 7 | 7 |
| Zerodha | 15 | 5 | 7 |
| Groww | 15 | 6 | 6 |
| Zepto | 15 | 10 | 10 |
| PhysicsWallah | 15 | 8 | 9 |
| CRED | 15 | 3 | 3 |
| Freshworks | 15 | 9 | 9 |

v2 = the live policy. v3 = v2 plus a hiring-momentum signal (joiners in the last 6 months vs the best 6 months of the prior year; red <= -65%, amber <= -50%). v3 was designed after looking at Dunzo, so Dunzo is in-sample; the other companies are the test.

Caveats: small sample (case studies, not an accuracy figure); companies chosen with hindsight; profiles are updated late and deleted profiles are missing (survivorship); job posts, websites and news are not replayed; event dates are the earliest credible article we found, not necessarily the first ever.
