"""Run the buyer checks (intake -> resolve -> verify -> explain) on one request, live or from recordings.

    python scripts/try_buyer_checks.py --case 3                 # one of the 12 test requests, live API
    python scripts/try_buyer_checks.py --text "Need ₹20 lakh ..."
    python scripts/try_buyer_checks.py --all --parse-only       # parse all 12 requests, no API calls
    python scripts/try_buyer_checks.py --case 3 --replay        # use responses saved in fixtures/c/

Needs CRUSTDATA_API_KEY for live runs. This tiny client is for quick local testing only;
the product uses crustdata.py (throttle, cache, cost log, FAULT injection).
The decision printed here is a PLACEHOLDER until decide.py is merged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from request_cases import CASES  # noqa: E402
from termsdesk.contracts import Decision, Reason  # noqa: E402
from termsdesk.explain import explain  # noqa: E402
from termsdesk.intake import parse_request  # noqa: E402
from termsdesk.resolve import resolve_company  # noqa: E402
from termsdesk.verify import verify_buyer  # noqa: E402

FIXTURES = ROOT / "fixtures" / "c"


class DevClient:
    def __init__(self, key: str | None, replay: bool = False):
        self.key, self.replay, self.calls = key, replay, 0
        FIXTURES.mkdir(parents=True, exist_ok=True)

    def _path(self, path: str, body: dict) -> Path:
        h = hashlib.sha1((path + json.dumps(body, sort_keys=True)).encode()).hexdigest()[:16]
        return FIXTURES / f"{path.strip('/').replace('/', '_')}_{h}.json"

    def post(self, path: str, body: dict):
        f = self._path(path, body)
        if f.exists():
            return json.loads(f.read_text())
        if self.replay:
            raise RuntimeError(f"no recording for {path} (run once live first)")
        if not self.key:
            raise RuntimeError("CRUSTDATA_API_KEY is not set")
        req = urllib.request.Request(
            "https://api.crustdata.com" + path, data=json.dumps(body).encode(), method="POST",
            headers={"authorization": f"Bearer {self.key}", "content-type": "application/json",
                     "x-api-version": "2025-11-01"})
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode())
                    self.calls += 1
                    f.write_text(json.dumps(data, ensure_ascii=False))
                    return data
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 0:
                    time.sleep(min(float(e.headers.get("x-ratelimit-reset", 5)), 10))
                    continue
                if e.code >= 500 and attempt == 0:
                    time.sleep(2)
                    continue
                raise RuntimeError(f"HTTP {e.code} on {path}: {e.read().decode()[:300]}") from None
            except (urllib.error.URLError, TimeoutError):
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise


def run(text: str, case_id: str, client: DevClient | None) -> None:
    parsed = parse_request(text, case_id=case_id)
    print(f"\n=== {case_id} ===")
    print("Request:", json.dumps(parsed.request.model_dump(exclude={"raw_text"}), ensure_ascii=False))
    for n in parsed.notes:
        print("  note:", n)
    if parsed.question:
        print("  ASK:", parsed.question)
    if client is None or parsed.needs_company:
        return
    today = time.strftime("%Y-%m-%d")
    res = resolve_company(client, parsed.request, case_date=today)
    print(f"Resolve: {res.status} {res.name} #{res.company_id} ({res.match}) exposure={res.exposure_inr} "
          f"flags={res.flags}")
    for n in res.notes:
        print("  note:", n)
    if res.needs_human:
        print("  HUMAN:", res.reason)
        return
    v = verify_buyer(client, parsed.request, res, case_date=today)
    print(f"Verify: domain={v.domain.status}/{v.domain.kind} sender={v.sender.status} "
          f"verified={v.buyer_verified} hold={v.hold} ({v.hold_reason})")
    print("  ", v.domain.detail, "|", v.sender.detail)
    if v.verification_email:
        print("Verification email (sandbox) ->", v.verification_email["to_intended"])
        print(v.verification_email["body"])
    kind = "hold_verify" if v.hold else "approve"
    placeholder = Decision(decision=kind, limit_inr=None if v.hold else parsed.request.amount_inr,
                           terms_days=None if v.hold else (parsed.request.terms_days or 30),
                           reasons=[Reason(text="PLACEHOLDER decision until decide.py is merged")],
                           exposure_inr=res.exposure_inr, policy_version="v2")
    out = explain(placeholder, res.evidence + v.evidence, parsed.request, res.name, case_id=case_id)
    print("Note:", out.note.headline)
    for r in out.note.reasons:
        print("  -", r["display"])
    for line in out.note.could_not_verify:
        print("  -", line)
    if out.letter:
        print(f"Letter ({out.letter.method}, key {out.letter.key}) -> {out.letter.to_intended}\n"
              f"Subject: {out.letter.subject}\n{out.letter.body}")
    print(f"API calls this run: {client.calls}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", type=int)
    ap.add_argument("--text")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--parse-only", action="store_true")
    ap.add_argument("--replay", action="store_true")
    a = ap.parse_args()
    client = None if a.parse_only else DevClient(os.environ.get("CRUSTDATA_API_KEY"), replay=a.replay)
    if a.text:
        run(a.text, "cli", client)
    for n, c in CASES.items():
        if (a.all or a.case == n) and "text" in c:
            run(c["text"], f"c{n}", client)


if __name__ == "__main__":
    main()
