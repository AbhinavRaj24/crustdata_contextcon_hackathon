"""Command line: python -m termsdesk.cli "request text"   |   python -m termsdesk.cli --resume <case_id>"""
import json
import sys

from . import pipeline, store
from .money import inr


def show(case_id: str) -> None:
    case = store.get_case(case_id)
    print(f"\n== case {case_id}: {case['state']}")
    for e in store.list_events(case_id):
        print(f"  [{e['kind']}] {e['message']}")
    for s in case.get("signals") or []:
        print(f"  signal {s['level']:5} {s['signal']:18} {s['summary']}")
    _, d = store.latest_decision(case_id)
    if d:
        print(f"  DECISION {d.decision} {inr(d.limit_inr)} on {d.terms_days} days ({d.rule}); could not verify: {len(d.could_not_verify)}")
    for o in store.list_outbox(case_id):
        print(f"  OUTBOX {o['key']} -> intended {o['to_intended']} | actual {o['to_actual']} | {o['status']}")
    print(f"  credits (estimated): {store.case_cost(case_id):.2f}")
    if case.get("error"):
        print("  ERROR:", case["error"][-600:])


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--resume":
        pipeline.resume(sys.argv[2])
        show(sys.argv[2])
    else:
        cid = pipeline.submit(" ".join(sys.argv[1:]))
        show(cid)
