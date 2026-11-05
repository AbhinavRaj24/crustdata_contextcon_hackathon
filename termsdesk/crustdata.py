"""Crustdata client: per-endpoint throttle, retries, day cache, cost log, fault injection, replay.

FAULT=enrich:timeout,person_search:429,enrich:garbage   simulate failures (never cached)
REPLAY=1                                               answer only from the local cache
"""
from __future__ import annotations

import contextvars
import datetime as dt
import hashlib
import json
import os
import threading
import time
from collections import deque
from typing import Any, Optional

import httpx

from . import config, store

BASE = "https://api.crustdata.com"
API_VERSION = "2025-11-01"

RPM = {
    "/company/identify": 30, "/company/enrich": 15, "/company/search": 30, "/person/search": 30,
    "/person/enrich": 15, "/job/search": 30, "/web/search/live": 10, "/web/enrich/live": 10,
}
SHORT = {
    "/company/identify": "identify", "/company/enrich": "enrich", "/company/search": "company_search",
    "/person/search": "person_search", "/person/enrich": "person_enrich", "/job/search": "job_search",
    "/web/search/live": "web_search", "/web/enrich/live": "web_fetch",
}

current_case: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("current_case", default=None)


class CrustdataError(Exception):
    pass


class Unavailable(CrustdataError):
    """The call failed after retries (timeout, 5xx, rate limit, dropped connection) or replay had no answer."""


class OutOfCredits(CrustdataError):
    pass


class Forbidden(CrustdataError):
    pass


class BadRequest(CrustdataError):
    pass


class _Throttle:
    """Keeps each endpoint below its per-minute limit across threads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: dict[str, deque] = {}

    def wait(self, path: str) -> None:
        limit = max(1, RPM.get(path, 30) - 2)
        while True:
            with self.lock:
                q = self.calls.setdefault(path, deque())
                t = time.monotonic()
                while q and t - q[0] > 60:
                    q.popleft()
                if len(q) < limit:
                    q.append(t)
                    return
                sleep_for = 60 - (t - q[0]) + 0.05
            time.sleep(min(sleep_for, 5))


_throttle = _Throttle()


def faults() -> dict[str, str]:
    out = {}
    for part in os.environ.get("FAULT", "").split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _estimate_cost(path: str, body: dict, data: Any) -> float:
    if path == "/company/identify":
        return 0.0
    if path == "/company/enrich":
        return 2.0 * max(1, len(body.get("crustdata_company_ids") or body.get("domains") or [1]))
    if path == "/person/search":
        n = len(data.get("profiles", [])) if isinstance(data, dict) else 0
        return round(0.03 + 0.2 * max(n, 1), 2)  # base + premium groups, rough upper bound
    if path == "/job/search":
        n = len(data.get("job_listings", data.get("jobs", []))) if isinstance(data, dict) else 0
        return round(0.03 * max(n, 1), 2)
    if path == "/web/search/live":
        return 1.0
    if path == "/web/enrich/live":
        return 1.0 * len(body.get("urls", [1]))
    return 1.0


def _garble(path: str, data: Any) -> Any:
    """FAULT=<endpoint>:garbage returns nonsense values that sanity checks must catch."""
    if path == "/company/enrich" and isinstance(data, list):
        for item in data:
            for m in item.get("matches", []):
                cd = m.get("company_data", {})
                hc = cd.get("headcount") or {}
                if hc.get("timeseries"):
                    hc["timeseries"][-1]["employee_count"] = -500
                if cd.get("funding"):
                    cd["funding"]["last_fundraise_date"] = "2031-01-01"
    return data


class Client:
    def __init__(self, api_key: Optional[str] = None, use_cache: bool = True, timeout: float = 30.0) -> None:
        self.api_key = api_key or config.CRUSTDATA_API_KEY
        self.use_cache = use_cache
        self.http = httpx.Client(base_url=BASE, timeout=timeout)

    def _headers(self) -> dict:
        return {"authorization": f"Bearer {self.api_key}", "content-type": "application/json", "x-api-version": API_VERSION}

    @staticmethod
    def cache_key(path: str, body: Any) -> str:
        day = dt.date.today().isoformat()  # same company, same day -> same evidence
        raw = json.dumps({"p": path, "b": body, "d": day}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def post(self, path: str, body: dict, *, cacheable: bool = True) -> Any:
        fault = faults().get(SHORT.get(path, path))
        key = self.cache_key(path, body)
        if cacheable and self.use_cache and not fault:
            hit = store.cache_get(key)
            if hit is not None:
                return hit
        if config.REPLAY:
            raise Unavailable(f"replay mode: no cached answer for {SHORT.get(path, path)}")

        case_id = current_case.get()
        retries_429, retries_net = 0, 0
        while True:
            _throttle.wait(path)
            try:
                if fault == "timeout":
                    raise httpx.ReadTimeout("injected timeout (FAULT)")
                if fault == "429":
                    resp = httpx.Response(429, headers={"x-ratelimit-reset": "1"}, json={"error": "injected 429 (FAULT)"})
                else:
                    resp = self.http.post(path, headers=self._headers(), json=body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                retries_net += 1
                store.log_event(case_id, "RETRY", f"{SHORT.get(path, path)}: {type(exc).__name__}, attempt {retries_net}") if case_id else None
                if retries_net > 2:
                    store.log_cost(case_id, path, 0.0, "dropped/timeout: may have been charged")
                    raise Unavailable(f"{SHORT.get(path, path)}: {type(exc).__name__} after {retries_net} attempts") from exc
                time.sleep(1.5 * retries_net)
                continue

            if resp.status_code == 429:
                retries_429 += 1
                wait = min(float(resp.headers.get("x-ratelimit-reset", "5") or 5), 30)
                store.log_event(case_id, "RETRY", f"{SHORT.get(path, path)}: rate limited, waiting {wait:.0f}s") if case_id else None
                if retries_429 > 3:
                    raise Unavailable(f"{SHORT.get(path, path)}: still rate limited after 3 retries")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                retries_net += 1
                if retries_net > 2:
                    raise Unavailable(f"{SHORT.get(path, path)}: HTTP {resp.status_code}")
                time.sleep(1.5 * retries_net)
                continue
            if resp.status_code == 402:
                raise OutOfCredits("Crustdata credits exhausted")
            if resp.status_code == 403:
                raise Forbidden(f"{path} not enabled for this key")
            if resp.status_code >= 400:
                raise BadRequest(f"{path} HTTP {resp.status_code}: {resp.text[:300]}")

            data = resp.json()
            if fault == "garbage":
                data = _garble(path, data)
            else:
                store.cache_put(key, path, body, data)
            store.log_cost(case_id, path, _estimate_cost(path, body, data))
            return data

    # ---------- typed helpers ----------

    def identify(self, domain: Optional[str] = None, name: Optional[str] = None) -> list[dict]:
        body = {"domains": [domain]} if domain else {"names": [name]}
        data = self.post("/company/identify", body)
        matches = []
        for item in data if isinstance(data, list) else []:
            matches.extend(item.get("matches", []))
        return matches

    def enrich(self, company_id: int, fields: list[str]) -> dict:
        data = self.post("/company/enrich", {"crustdata_company_ids": [company_id], "fields": fields})
        for item in data if isinstance(data, list) else []:
            for m in item.get("matches", []):
                return m.get("company_data", {})
        return {}

    def person_search(self, filters: dict, limit: int = 10, sorts: Optional[list] = None) -> dict:
        body: dict = {"filters": filters, "limit": limit}
        if sorts:
            body["sorts"] = sorts
        return self.post("/person/search", body)

    def count_people(self, filters: dict) -> int:
        data = self.person_search(filters, limit=1)
        return int(data.get("total_count") or 0)

    def job_search(self, filters: dict, limit: int = 5, sorts: Optional[list] = None) -> dict:
        body: dict = {"filters": filters, "limit": limit}
        if sorts:
            body["sorts"] = sorts
        return self.post("/job/search", body)

    def web_search(self, query: str, sources: list[str], start_date: Optional[int] = None, end_date: Optional[int] = None) -> dict:
        body: dict = {"query": query, "sources": sources}
        if start_date:
            body["start_date"] = start_date
        if end_date:
            body["end_date"] = end_date
        return self.post("/web/search/live", body)

    def web_fetch(self, urls: list[str]) -> Any:
        return self.post("/web/enrich/live", {"urls": urls})


def cond(field: str, op: str, value: Any) -> dict:
    return {"field": field, "type": op, "value": value}


def all_of(*conditions: dict) -> dict:
    return {"op": "and", "conditions": list(conditions)}


def any_of(*conditions: dict) -> dict:
    return {"op": "or", "conditions": list(conditions)}


_client: Optional[Client] = None


def client() -> Client:
    global _client
    if _client is None:
        _client = Client()
    return _client
