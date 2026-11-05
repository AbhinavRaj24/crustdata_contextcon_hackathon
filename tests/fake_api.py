"""A fake Crustdata client for the buyer-check tests: real response shapes, made-up people.

Company records copy the shape and values of live /company/identify replies (6 Nov 2026).
People are fictional. Person Search here understands the filters verify.py sends:
company_id '=', name '(.)', title '(.)' (optionally inside an 'or' group) and seniority 'in'.
"""
from __future__ import annotations

import copy

RAZORPAY = {"crustdata_company_id": 897127, "basic_info": {
    "name": "Razorpay", "crustdata_company_id": 897127, "primary_domain": "razorpay.com",
    "all_domains": ["curlec.com", "bitly.com", "razorpay.com"], "company_type": "Privately Held",
    "year_founded": 2014, "employee_count_range": "1001-5000"}}
DUNZO = {"crustdata_company_id": 5721, "basic_info": {
    "name": "Dunzo", "crustdata_company_id": 5721, "primary_domain": "dunzo.com", "all_domains": ["dunzo.com"],
    "company_type": "Privately Held", "year_founded": 2015, "employee_count_range": "5001-10000"}}
DUNZO_SMALL = {"crustdata_company_id": 1735338, "basic_info": {
    "name": "Dunzo", "crustdata_company_id": 1735338, "primary_domain": "getdunzo.com",
    "all_domains": ["getdunzo.com"], "company_type": "Privately Held", "year_founded": 2019,
    "employee_count_range": "11-50"}}
DUNZO_TINY = {"crustdata_company_id": 3062315, "basic_info": {
    "name": "Dunzo", "crustdata_company_id": 3062315, "primary_domain": "godunzo.com",
    "all_domains": ["godunzo.com"], "company_type": "Privately Held", "year_founded": None,
    "employee_count_range": "2-10"}}
DUNZO_SOLO = {"crustdata_company_id": 26948996, "basic_info": {
    "name": "Dunzo", "crustdata_company_id": 26948996, "primary_domain": None, "all_domains": [],
    "company_type": "Privately Held", "year_founded": None, "employee_count_range": "myself only"}}
GOMECH = {"crustdata_company_id": 700001, "basic_info": {
    "name": "GoMechanic", "crustdata_company_id": 700001, "primary_domain": "gomechanic.in",
    "all_domains": ["gomechanic.in"], "company_type": "Privately Held", "year_founded": 2016,
    "employee_count_range": "501-1000"}}
NOVA_A = {"crustdata_company_id": 800001, "basic_info": {
    "name": "Nova Foods", "crustdata_company_id": 800001, "primary_domain": "novafoods.in", "all_domains": [],
    "company_type": "Privately Held", "year_founded": 2012, "employee_count_range": "51-200"}}
NOVA_B = {"crustdata_company_id": 800002, "basic_info": {
    "name": "Nova Foods", "crustdata_company_id": 800002, "primary_domain": "novafoods.com", "all_domains": [],
    "company_type": "Privately Held", "year_founded": 2009, "employee_count_range": "201-500"}}
METACUBE = {"crustdata_company_id": 15595, "basic_info": {
    "name": "Metacube Software Pvt Ltd", "crustdata_company_id": 15595, "primary_domain": "metacube.com",
    "all_domains": [], "company_type": "Privately Held", "year_founded": 2004, "employee_count_range": "1001-5000"}}
KANPUR_INTL = {"crustdata_company_id": 26702196, "basic_info": {
    "name": "Kanpur Leather International", "crustdata_company_id": 26702196,
    "primary_domain": "kanpurleatherinternational.com", "all_domains": [], "company_type": "Sole Proprietorship",
    "year_founded": 2005, "employee_count_range": "201-500"}}
IITK = {"crustdata_company_id": 6052440, "basic_info": {
    "name": "Indian Institute of Technology Kanpur", "crustdata_company_id": 6052440, "primary_domain": "iitk.ac.in",
    "all_domains": [], "company_type": "Educational Institution", "year_founded": 1959,
    "employee_count_range": "1001-5000"}}

BY_DOMAIN = {"razorpay.com": [RAZORPAY], "dunzo.com": [DUNZO], "getdunzo.com": [DUNZO_SMALL],
             "gomechanic.in": [GOMECH], "bitly.com": [RAZORPAY]}
BY_NAME = {
    "razorpay": [(1.0, RAZORPAY)],
    "razorpay software pvt ltd": [(0.6, METACUBE), (0.6, RAZORPAY)],
    "dunzo": [(1.0, DUNZO), (1.0, DUNZO_TINY), (1.0, DUNZO_SMALL), (1.0, DUNZO_SOLO)],
    "gomechanic": [(1.0, GOMECH)],
    "kanpur leather exports": [(0.6, KANPUR_INTL), (0.6, IITK)],
    "nova foods": [(1.0, NOVA_A), (1.0, NOVA_B)],
}


def _cur(company_id, title, seniority, start, function=None, company_name=None):
    return {"crustdata_company_id": company_id, "name": company_name or "", "title": title,
            "seniority_level": seniority, "start_date": start + "T00:00:00", "end_date": None,
            "function_category": function, "company_status": "active"}


PEOPLE = [
    {"crustdata_person_id": 1, "basic_profile": {"name": "Asha Rao"}, "current": [
        _cur(897127, "Procurement Manager", "Experienced Manager", "2023-08-01", "Operations", "Razorpay")]},
    {"crustdata_person_id": 2, "basic_profile": {"name": "Meera Iyer"}, "current": [
        _cur(897127, "Chief Financial Officer", "CXO", "2019-11-01", "Finance", "Razorpay")]},
    {"crustdata_person_id": 3, "basic_profile": {"name": "Karan Shah"}, "current": [
        _cur(897127, "Head of Core Finance", "Director", "2024-10-01", "Finance", "Razorpay")]},
    {"crustdata_person_id": 4, "basic_profile": {"name": "Vikram Shah"}, "current": [
        _cur(897127, "Software Engineer", "Senior", "2022-01-01", "Engineering", "Razorpay")]},
    {"crustdata_person_id": 5, "basic_profile": {"name": "Rahul Mehta"}, "current": [
        _cur(897127, "Procurement Lead", "Experienced Manager", "2021-05-01", "Operations", "Razorpay")]},
    {"crustdata_person_id": 6, "basic_profile": {"name": "Neel Das"}, "current": [
        _cur(111, "Procurement Head", "Director", "2020-01-01", "Operations", "Other Co"),
        _cur(897127, "Software Engineer", "Senior", "2024-01-01", "Engineering", "Razorpay")]},
    {"crustdata_person_id": 7, "basic_profile": {"name": "Kiran Nair"}, "current": [
        _cur(5721, "Operations Manager", "Experienced Manager", "2021-03-01", "Operations", "Dunzo")]},
    {"crustdata_person_id": 8, "basic_profile": {"name": "Arjun Bose"}, "current": [
        _cur(5721, "Co-founder & CEO", "CXO", "2015-01-01", "Entrepreneurship", "Dunzo")]},
]


def _words_in(value: str, text: str) -> bool:
    return all(w in (text or "").lower() for w in value.lower().split())


def _match_entry(entry: dict, cond: dict) -> bool:
    if "op" in cond:
        results = [_match_entry(entry, c) for c in cond["conditions"]]
        return all(results) if cond["op"] == "and" else any(results)
    f, t, v = cond["field"], cond["type"], cond["value"]
    if f.endswith("current.company_id"):
        return entry["crustdata_company_id"] == v
    if f.endswith("current.title"):
        return _words_in(v, entry["title"])
    if f.endswith("current.seniority_level"):
        return entry["seniority_level"] in v
    return True                                     # person-level fields are checked separately


def _name_ok(person: dict, cond: dict) -> bool:
    if "op" in cond:
        return all(_name_ok(person, c) for c in cond["conditions"])
    if cond.get("field") == "basic_profile.name":
        return _words_in(cond["value"], person["basic_profile"]["name"])
    return True


class FakeClient:
    def __init__(self, fail: set | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail or set()

    def post(self, path: str, body: dict):
        self.calls.append((path, copy.deepcopy(body)))
        if path in self.fail:
            raise TimeoutError("timed out after 3 retries")
        if path == "/company/identify":
            if "domains" in body:
                return [{"matched_on": d, "match_type": "domain",
                         "matches": [{"confidence_score": 1.0, "company_data": c} for c in BY_DOMAIN.get(d, [])]}
                        for d in body["domains"]]
            return [{"matched_on": n, "match_type": "name",
                     "matches": [{"confidence_score": s, "company_data": c} for s, c in BY_NAME.get(n.lower(), [])]}
                    for n in body["names"]]
        if path == "/person/search":
            flt = body["filters"]
            out = []
            for p in PEOPLE:
                if not _name_ok(p, flt):
                    continue
                if any(_match_entry(e, flt) for e in p["current"]):
                    out.append({"crustdata_person_id": p["crustdata_person_id"], "basic_profile": p["basic_profile"],
                                "experience": {"employment_details": {"current": p["current"]}}})
            return {"profiles": out[: body.get("limit", 20)], "next_cursor": None, "total_count": len(out)}
        raise AssertionError(f"unexpected call {path}")
