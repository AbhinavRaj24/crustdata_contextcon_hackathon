"""Watch accounts for change. Real Crustdata watchers refresh at most daily, so the demo replays labelled,
simulated events; the re-decision path is the same either way."""
from __future__ import annotations

from . import store
from .contracts import Company, State


def register(case_id: str, company: Company) -> None:
    store.log_event(case_id, "WATCH", f"Watch registered on {company.name}: headcount, joiners, leadership exits, news")


def simulate(company_id: int, kind: str, payload: dict) -> list[str]:
    """Record a simulated watcher event and re-decide every monitored case for that company."""
    from . import pipeline  # avoid an import cycle

    store.add_watch_event(company_id, kind, payload, simulated=True)
    rows = store.conn().execute("SELECT id FROM cases WHERE company_id = ? AND state = ?", (company_id, State.MONITORING.value)).fetchall()
    done = []
    for r in rows:
        why = f"{payload.get('title', kind)} left ({kind}, simulated watcher event)"
        pipeline.redecide(r["id"], why)
        done.append(r["id"])
    return done
