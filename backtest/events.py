"""Find a cited event date for each distressed company: the earliest credible article reporting unpaid salaries,
unpaid vendors, insolvency or shutdown. Search results carry no dates, so each article is fetched and its own
published date is read from the page metadata. Output: backtest/events.json (reviewed by a person before use)."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

from termsdesk import llm
from termsdesk.crustdata import Unavailable, client

HERE = Path(__file__).parent
DATE_PATTERNS = [
    r'property=["\']article:published_time["\']\s+content=["\']([0-9]{4}-[0-9]{2}-[0-9]{2})',
    r'content=["\']([0-9]{4}-[0-9]{2}-[0-9]{2})[^"\']*["\']\s+property=["\']article:published_time',
    r'"datePublished"\s*:\s*"([0-9]{4}-[0-9]{2}-[0-9]{2})',
    r'name=["\'](?:publish-date|pubdate|publishdate|date)["\']\s+content=["\']([0-9]{4}-[0-9]{2}-[0-9]{2})',
    r'<time[^>]+datetime=["\']([0-9]{4}-[0-9]{2}-[0-9]{2})',
]
JUNK = re.compile(r"tracxn|crunchbase|zoominfo|cbinsights|pitchbook|owler|glassdoor|ambitionbox|linkedin|wikipedia|youtube", re.I)
SCHEMA = {"type": "OBJECT", "properties": {"reports_distress": {"type": "BOOLEAN"}, "quote": {"type": "STRING"}},
          "required": ["reports_distress", "quote"]}


def published_date(html: str) -> str | None:
    for pat in DATE_PATTERNS:
        m = re.search(pat, html or "", re.I)
        if m:
            return m.group(1)
    return None


def find_event(name: str, max_fetch: int = 4, before: str | None = None) -> dict:
    """before='2024-06-01' searches only news published before that date (to reach the earliest reports)."""
    import datetime as dt
    c = client()
    found = []
    end = int(dt.datetime.fromisoformat(before).timestamp()) if before else None
    queries = (f"{name} salaries delayed unpaid", f"{name} unpaid vendors dues insolvency", f"{name} shut down layoffs")
    for query in queries:
        try:
            res = c.web_search(query, ["news"], start_date=int(dt.datetime(2019, 1, 1).timestamp()) if end else None, end_date=end)
        except Unavailable as e:
            print(f"  search failed: {e}")
            continue
        for r in (res.get("results") or [])[:6]:
            url, title, snippet = r.get("url") or "", r.get("title") or "", r.get("snippet") or ""
            if JUNK.search(url + title) or name.lower().split()[0].strip("'") not in (title + snippet).lower():
                continue
            if any(f["url"] == url for f in found):
                continue
            found.append({"url": url, "title": title, "snippet": snippet})
    articles = []
    for f in found[:max_fetch]:
        try:
            page = c.web_fetch([f["url"]])
        except Unavailable:
            continue
        item = page[0] if isinstance(page, list) and page else {}
        if not item.get("success"):
            continue
        date = published_date(item.get("content") or "")
        verdict, _ = llm.generate("event_distress_v1", f"Company: {name}\nHeadline: {f['title']}\nSnippet: {f['snippet']}\n"
                                  "Does this report that the company failed to pay salaries or vendors, or went insolvent or shut down? "
                                  "quote = the exact supporting words.", SCHEMA)
        distress = verdict.get("reports_distress") if isinstance(verdict, dict) else None
        articles.append({**f, "final_url": item.get("url") or f["url"], "published": date, "reports_distress": distress,
                         "quote": verdict.get("quote") if isinstance(verdict, dict) else None})
    dated = sorted([a for a in articles if a["published"] and a["reports_distress"]], key=lambda a: a["published"])
    return {"name": name, "event_date": dated[0]["published"] if dated else None, "source": dated[0] if dated else None,
            "candidates": articles}


if __name__ == "__main__":
    cohort = yaml.safe_load((HERE / "cohort.yaml").read_text())
    args = sys.argv[1:]
    before = next((a.split("=", 1)[1] for a in args if a.startswith("--before=")), None)
    only = {a for a in args if not a.startswith("--")}
    out_path = HERE / "events.json"
    out = json.loads(out_path.read_text()) if out_path.exists() else {}
    for co in cohort["distressed"]:
        if only and co["name"] not in only:
            continue
        print(f"== {co['name']}")
        ev = find_event(co["name"], before=co.get("search_before") or before)
        prev = out.get(co["name"])
        if prev and prev.get("event_date") and (not ev["event_date"] or prev["event_date"] < ev["event_date"]):
            ev = {**ev, "event_date": prev["event_date"], "source": prev["source"]}  # keep the earliest cited date
        out[co["name"]] = ev
        print(f"  event_date={ev['event_date']}  source={(ev['source'] or {}).get('title')}")
        for a in ev["candidates"]:
            print(f"   - {a['published']} distress={a['reports_distress']} {a['title'][:80]}")
        out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
