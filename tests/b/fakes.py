"""A fake Crustdata client for B's tests. It evaluates the same filter trees signals.py sends, at the level of
one job entry (company, title and dates on the same entry), so the tests exercise the real query logic offline.

Two made-up worlds: a healthy company (with the traps that caused false alarms) and a zombie that looks alive.
"""
from __future__ import annotations

import copy
import datetime as dt

from termsdesk.crustdata import OutOfCredits, Unavailable

CUR, PAST = "experience.employment_details.current.", "experience.employment_details.past."
KEYS = {"company_id": "crustdata_company_id", "company_name": "name"}


def _test(op: str, have, want) -> bool:
    if op == "=":
        return have == want
    if op in ("=>", "=<"):
        if have is None:
            return False
        h, w = str(have)[:10], str(want)[:10]
        return h >= w if op == "=>" else h <= w
    if op == "(.)":
        return all(word in str(have or "").lower() for word in str(want).lower().split())
    if op == "[.]":
        return str(want).lower() == str(have or "").lower()
    if op == "in":
        return have in want
    raise AssertionError(f"operator {op} not supported by the fake")


def _entry_ok(node: dict, entry: dict, side: str) -> bool:
    if "op" in node:
        results = [_entry_ok(c, entry, side) for c in node["conditions"]]
        return all(results) if node["op"] == "and" else any(results)
    f = node["field"]
    if f.startswith(CUR) or f.startswith(PAST):
        if not f.startswith(CUR if side == "current" else PAST):
            return False
        key = f.rsplit(".", 1)[1]
    elif f == "experience.employment_details.end_date":
        key = "end_date"
    else:
        return True
    return _test(node["type"], entry.get(KEYS.get(key, key)), node["value"])


def matches(profile: dict, filters: dict) -> bool:
    ed = profile["experience"]["employment_details"]
    return any(_entry_ok(filters, e, side) for side in ("current", "past") for e in ed.get(side) or [])


def job(company_id: int, title: str, added: str) -> dict:
    return {"company_id": company_id, "job_details": {"title": title}, "metadata": {"date_added": added + "T00:00:00"}}


def _job_ok(node: dict, j: dict) -> bool:
    if "op" in node:
        results = [_job_ok(c, j) for c in node["conditions"]]
        return all(results) if node["op"] == "and" else any(results)
    have = j["company_id"] if node["field"].endswith("crustdata_company_id") else j["metadata"]["date_added"]
    return _test(node["type"], have, node["value"])


class FakeClient:
    def __init__(self, companies: dict, people: list, jobs: list, news: list | None = None, web: dict | None = None,
                 fail: dict | None = None):
        self.companies, self.people, self.jobs = companies, people, jobs
        self.news, self.web = news or [], web or {}
        self.fail = fail or {}
        self.calls: list[tuple] = []

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise self.fail[name]

    def enrich(self, company_id: int, fields: list[str]) -> dict:
        self.calls.append(("enrich", company_id, tuple(fields)))
        self._maybe_fail("enrich")
        assert "people" not in fields, "never request the enrich people section"
        return copy.deepcopy(self.companies[company_id])

    def person_search(self, filters: dict, limit: int = 10, sorts=None) -> dict:
        self.calls.append(("person_search", filters, limit))
        self._maybe_fail("person_search")
        hits = [p for p in self.people if matches(p, filters)]
        return {"profiles": copy.deepcopy(hits[:limit]), "total_count": len(hits)}

    def count_people(self, filters: dict) -> int:
        return int(self.person_search(filters, limit=1)["total_count"])

    def job_search(self, filters: dict, limit: int = 5, sorts=None) -> dict:
        self.calls.append(("job_search", filters, limit))
        self._maybe_fail("job_search")
        hits = sorted((j for j in self.jobs if _job_ok(filters, j)), key=lambda j: j["metadata"]["date_added"], reverse=True)
        return {"job_listings": copy.deepcopy(hits[:limit]), "total_count": len(hits)}

    def web_search(self, query, sources, start_date=None, end_date=None) -> dict:
        self.calls.append(("web_search", query, start_date, end_date))
        self._maybe_fail("web_search")
        return {"results": copy.deepcopy(self.news)}

    def web_fetch(self, urls: list[str]):
        self.calls.append(("web_fetch", tuple(urls)))
        self._maybe_fail("web_fetch")
        return copy.deepcopy(self.web.get("first", [{"success": True, "status_code": 200, "title": "Home"}]))

    def post(self, path: str, body: dict, *, cacheable: bool = True):
        self.calls.append(("post", path))
        return copy.deepcopy(self.web.get("retry", self.web.get("first", [{"success": True}])))


def person(name: str, current: list[dict] = (), past: list[dict] = ()) -> dict:
    return {"basic_profile": {"name": name},
            "experience": {"employment_details": {"current": list(current), "past": list(past)}}}


def entry(company_id: int, company: str, title: str, start: str, end: str | None = None) -> dict:
    return {"crustdata_company_id": company_id, "name": company, "title": title,
            "start_date": start + "T00:00:00", "end_date": (end + "T00:00:00") if end else None}


def monthly(start: dt.date, values: list[int]) -> list[dict]:
    out, y, m = [], start.year, start.month
    for v in values:
        out.append({"date": f"{y:04d}-{m:02d}-01", "employee_count": v})
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


# ---------- a healthy company with every trap that caused false alarms ----------

HEALTHY_ID, HEALTHY = 101, "Healthyco"


def healthy_world(**overrides) -> FakeClient:
    series = monthly(dt.date(2023, 1, 1), [250 + i for i in range(46)])          # Jan 2023 .. Oct 2026, growing
    company = {"basic_info": {"name": HEALTHY, "year_founded": 2014},
               "headcount": {"timeseries": series, "growth_percent": {"yoy": 5}},
               "funding": {"last_fundraise_date": "2022-05-10", "last_round_type": "series_d",
                           "milestones": [{"date": "2022-05-10", "round_type": "series_d", "amount_usd": 1e8}]}}
    people = []
    for i in range(260):
        title = "Accounts Executive" if i < 20 else "Engineer"                      # 20 current finance staff
        people.append(person(f"Old {i}", [entry(HEALTHY_ID, HEALTHY, title, "2021-03-01")]))
    for i in range(40):
        people.append(person(f"New {i}", [entry(HEALTHY_ID, HEALTHY, "Engineer", f"2026-0{1 + i % 8}-01")]))
    people += [
        person("Real Leaver", [entry(999, "Elsewhere", "Finance Manager", "2026-07-01")],
               [entry(HEALTHY_ID, HEALTHY, "Finance Manager", "2020-01-01", "2026-07-01")]),
        person("Promoted", [entry(HEALTHY_ID, HEALTHY, "Senior Finance Manager", "2026-06-01")],
               [entry(HEALTHY_ID, HEALTHY, "Finance Manager", "2019-01-01", "2026-06-01")]),
        person("Sales Leaver", [entry(998, "Other", "Account Executive", "2026-08-01")],
               [entry(HEALTHY_ID, HEALTHY, "Key Accounts Manager", "2022-01-01", "2026-08-01")]),   # found by "accounts"
        person("Founders Office", [entry(997, "Other", "Analyst", "2026-09-01")],
               [entry(HEALTHY_ID, HEALTHY, "Associate - Founder's Office", "2024-01-01", "2026-09-01")]),
        person("Chief of Staff", [entry(996, "Other", "Manager", "2026-06-01")],
               [entry(HEALTHY_ID, HEALTHY, "Chief of Staff", "2023-01-01", "2026-06-01")]),
        person("Senior Director", [entry(995, "Other", "CFO", "2026-08-01")],
               [entry(HEALTHY_ID, HEALTHY, "Senior Director Finance", "2021-01-01", "2026-08-01")]),
    ]
    jobs = [job(HEALTHY_ID, "Engineer", "2024-03-01") for _ in range(5)]          # posts in the past, none this year
    kw = dict(companies={HEALTHY_ID: company}, people=people, jobs=jobs, news=[],
              web={"first": [{"success": False, "status_code": 403, "retryable": False, "error": "forbidden"}]})
    kw.update(overrides)
    return FakeClient(**kw)


# ---------- a zombie that looks alive ----------

ZOMBIE_ID, ZOMBIE = 202, "Zombieco"


def zombie_world(**overrides) -> FakeClient:
    rise = [2000 + round(i * 1422 / 23) for i in range(24)]                       # Jan 2021 .. Dec 2022 -> 3422
    fall = [3400, 3390, 3376, 3300, 3220, 3154, 3050, 2950, 2833, 2750, 2680, 2594]  # 2023
    flat = [2560, 2530, 2510, 2491, 2470, 2450, 2430, 2410, 2400, 2395, 2390, 2383] + [2400] * 21  # 2024 .. Sep 2026
    series = monthly(dt.date(2021, 1, 1), rise + fall + flat)
    company = {"basic_info": {"name": ZOMBIE, "year_founded": 2015},
               "headcount": {"timeseries": series, "growth_percent": {"yoy": 25}},   # API says +25%, series is flat
               "funding": {"last_fundraise_date": "2023-04-06", "last_round_type": "series_f",
                           "milestones": [{"date": "2021-06-01", "round_type": "series_e", "amount_usd": 2e8},
                                          {"date": "2023-04-06", "round_type": "series_f", "amount_usd": 7.5e7}]}}
    people = []
    for i in range(296):
        people.append(person(f"Stale {i}", [entry(ZOMBIE_ID, ZOMBIE, "Delivery Executive", "2021-05-01")]))
    for i in range(4):
        people.append(person(f"Stale finance {i}", [entry(ZOMBIE_ID, ZOMBIE, "Finance Executive", "2020-01-01")]))
    for i in range(10):                                                           # joined late 2022, gone by mid 2023
        people.append(person(f"Short stay {i}", [entry(990, "Elsewhere", "Rider", "2023-07-01")],
                             [entry(ZOMBIE_ID, ZOMBIE, "Delivery Associate", f"2022-1{1 + i % 2}-01", "2023-06-01")]))
    people.append(person("Gone CFO", [entry(991, "Elsewhere", "CFO", "2023-10-01")],
                         [entry(ZOMBIE_ID, ZOMBIE, "Head of Finance", "2019-01-01", "2023-09-01")]))
    for i, end in enumerate(["2023-03-01", "2023-04-01", "2023-05-01", "2023-06-01", "2023-07-01", "2023-07-01"]):
        people.append(person(f"Finance leaver {i}", [entry(992, "Elsewhere", "Accountant", end)],
                             [entry(ZOMBIE_ID, ZOMBIE, "Finance Manager", "2019-06-01", end)]))
    jobs = [job(ZOMBIE_ID, "Rider", "2023-02-10") for _ in range(3)]
    news = [{"title": "Zombieco delays salaries again as vendors go unpaid", "snippet": "The delivery startup has unpaid dues",
             "url": "https://news.example/zombieco"}]
    kw = dict(companies={ZOMBIE_ID: company}, people=people, jobs=jobs, news=news,
              web={"first": [{"success": False, "status_code": 504, "retryable": True, "error": "ORIGIN_UNAVAILABLE"}],
                   "retry": [{"success": False, "status_code": 504, "retryable": True, "error": "ORIGIN_UNAVAILABLE"}]})
    kw.update(overrides)
    return FakeClient(**kw)


__all__ = ["FakeClient", "healthy_world", "zombie_world", "HEALTHY_ID", "HEALTHY", "ZOMBIE_ID", "ZOMBIE", "person",
           "entry", "monthly", "job", "Unavailable", "OutOfCredits"]
