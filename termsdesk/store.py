"""SQLite persistence. Every state change is written before the next step runs, so a crash can resume."""
from __future__ import annotations

import datetime as dt
import json
import secrets
import sqlite3
import threading
from typing import Any, Optional

from . import config
from .contracts import Decision, Evidence

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY, state TEXT NOT NULL, raw_text TEXT NOT NULL,
  request_json TEXT, company_id INTEGER, company_json TEXT, verify_json TEXT, signals_json TEXT,
  policy_version TEXT, error TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS companies (
  crustdata_id INTEGER PRIMARY KEY, name TEXT, primary_domain TEXT, aliases_json TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
  company_id INTEGER PRIMARY KEY, limit_inr INTEGER DEFAULT 0, terms_days INTEGER DEFAULT 0,
  outstanding_inr INTEGER DEFAULT 0, advance_pct INTEGER DEFAULT 0, status TEXT, last_case_id TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY, case_id TEXT, signal TEXT, value_json TEXT, unit TEXT, source TEXT, url TEXT,
  as_of TEXT, status TEXT, note TEXT, cost REAL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
  case_id TEXT, version INTEGER, decision_json TEXT, created_at TEXT, PRIMARY KEY (case_id, version)
);
CREATE TABLE IF NOT EXISTS letters (
  case_id TEXT, version INTEGER, subject TEXT, body TEXT, written_by TEXT, created_at TEXT, PRIMARY KEY (case_id, version)
);
CREATE TABLE IF NOT EXISTS outbox (
  key TEXT PRIMARY KEY, case_id TEXT, action TEXT, to_intended TEXT, to_actual TEXT, subject TEXT, body TEXT,
  status TEXT, created_at TEXT, sent_at TEXT
);
CREATE TABLE IF NOT EXISTS human_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, kind TEXT, reason TEXT, status TEXT DEFAULT 'open',
  reviewer TEXT, resolution TEXT, decided_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS watch_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, kind TEXT, payload_json TEXT, simulated INTEGER,
  processed INTEGER DEFAULT 0, received_at TEXT
);
CREATE TABLE IF NOT EXISTS cost_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, endpoint TEXT, credits REAL, note TEXT, at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, kind TEXT, message TEXT, at TEXT
);
CREATE TABLE IF NOT EXISTS api_cache (key TEXT PRIMARY KEY, path TEXT, body_json TEXT, response_json TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, model TEXT, response_json TEXT, at TEXT);
"""


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        _local.conn = c
    return c


def _j(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


# ---------- cases ----------

def create_case(raw_text: str) -> str:
    case_id = "c" + secrets.token_hex(3)
    conn().execute(
        "INSERT INTO cases (id, state, raw_text, created_at, updated_at) VALUES (?, 'RECEIVED', ?, ?, ?)",
        (case_id, raw_text, now(), now()),
    )
    log_event(case_id, "RECEIVED", "Request received")
    return case_id


def get_case(case_id: str) -> Optional[dict]:
    row = conn().execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    if not row:
        return None
    case = dict(row)
    for col in ("request_json", "company_json", "verify_json", "signals_json"):
        case[col.replace("_json", "")] = json.loads(case[col]) if case[col] else None
    return case


def list_cases(limit: int = 50) -> list[dict]:
    rows = conn().execute("SELECT id FROM cases ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [get_case(r["id"]) for r in rows]


def update_case(case_id: str, **fields: Any) -> None:
    sets, vals = [], []
    for k, v in fields.items():
        if k in ("request", "company", "verify", "signals"):
            k, v = k + "_json", _j(v)
        sets.append(f"{k} = ?")
        vals.append(v)
    sets.append("updated_at = ?")
    vals.append(now())
    conn().execute(f"UPDATE cases SET {', '.join(sets)} WHERE id = ?", (*vals, case_id))


def set_state(case_id: str, state: str, message: str = "") -> None:
    update_case(case_id, state=state)
    log_event(case_id, state, message or state)


def log_event(case_id: str, kind: str, message: str) -> None:
    conn().execute("INSERT INTO events (case_id, kind, message, at) VALUES (?, ?, ?, ?)", (case_id, kind, message, now()))


def list_events(case_id: str) -> list[dict]:
    return [dict(r) for r in conn().execute("SELECT * FROM events WHERE case_id = ? ORDER BY id", (case_id,))]


# ---------- evidence ----------

def add_evidence(ev: Evidence) -> Evidence:
    # Rows without an explicit id get a random one: a count-based id could be reused after clear_evidence
    # and silently overwrite a row an older decision still cites (found by B).
    ev.id = ev.id or f"{ev.case_id}-e{secrets.token_hex(4)}"
    conn().execute(
        "INSERT OR REPLACE INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (ev.id, ev.case_id, ev.signal, _j(ev.value), ev.unit, ev.source, ev.url, ev.as_of, ev.status, ev.note, ev.cost, now()),
    )
    return ev


def list_evidence(case_id: str) -> list[Evidence]:
    rows = conn().execute("SELECT * FROM evidence WHERE case_id = ? ORDER BY created_at, id", (case_id,)).fetchall()
    return [
        Evidence(id=r["id"], case_id=r["case_id"], signal=r["signal"], value=json.loads(r["value_json"]), unit=r["unit"],
                 source=r["source"], url=r["url"], as_of=r["as_of"], status=r["status"], note=r["note"], cost=r["cost"] or 0)
        for r in rows
    ]


def clear_evidence(case_id: str, signals: Optional[list[str]] = None) -> None:
    if signals:
        conn().execute(f"DELETE FROM evidence WHERE case_id = ? AND signal IN ({','.join('?' * len(signals))})", (case_id, *signals))
    else:
        conn().execute("DELETE FROM evidence WHERE case_id = ?", (case_id,))


# ---------- decisions and letters ----------

def save_decision(case_id: str, decision: Decision) -> int:
    version = conn().execute("SELECT COALESCE(MAX(version), 0) + 1 FROM decisions WHERE case_id = ?", (case_id,)).fetchone()[0]
    conn().execute("INSERT INTO decisions VALUES (?, ?, ?, ?)", (case_id, version, decision.model_dump_json(), now()))
    return version


def latest_decision(case_id: str) -> tuple[int, Optional[Decision]]:
    row = conn().execute("SELECT version, decision_json FROM decisions WHERE case_id = ? ORDER BY version DESC LIMIT 1", (case_id,)).fetchone()
    return (row["version"], Decision.model_validate_json(row["decision_json"])) if row else (0, None)


def save_letter(case_id: str, version: int, subject: str, body: str, written_by: str) -> None:
    conn().execute("INSERT OR REPLACE INTO letters VALUES (?, ?, ?, ?, ?, ?)", (case_id, version, subject, body, written_by, now()))


def latest_letter(case_id: str) -> Optional[dict]:
    row = conn().execute("SELECT * FROM letters WHERE case_id = ? ORDER BY version DESC LIMIT 1", (case_id,)).fetchone()
    return dict(row) if row else None


# ---------- outbox (send once) ----------

def outbox_enqueue(key: str, case_id: str, action: str, to_intended: str, to_actual: str, subject: str, body: str) -> bool:
    """Returns False if this key was already queued: the caller must not send again."""
    cur = conn().execute(
        "INSERT OR IGNORE INTO outbox VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, NULL)",
        (key, case_id, action, to_intended, to_actual, subject, body, now()),
    )
    return cur.rowcount == 1


def outbox_mark_sent(key: str) -> None:
    conn().execute("UPDATE outbox SET status = 'sent', sent_at = ? WHERE key = ? AND status != 'sent'", (now(), key))


def list_outbox(case_id: Optional[str] = None) -> list[dict]:
    if case_id:
        rows = conn().execute("SELECT * FROM outbox WHERE case_id = ? ORDER BY created_at", (case_id,))
    else:
        rows = conn().execute("SELECT * FROM outbox ORDER BY created_at DESC")
    return [dict(r) for r in rows]


# ---------- human queue ----------

def queue_human(case_id: str, kind: str, reason: str) -> None:
    exists = conn().execute("SELECT 1 FROM human_queue WHERE case_id = ? AND kind = ? AND status = 'open'", (case_id, kind)).fetchone()
    if not exists:
        conn().execute("INSERT INTO human_queue (case_id, kind, reason, created_at) VALUES (?, ?, ?, ?)", (case_id, kind, reason, now()))


def list_queue(status: str = "open") -> list[dict]:
    return [dict(r) for r in conn().execute("SELECT * FROM human_queue WHERE status = ? ORDER BY id DESC", (status,))]


def resolve_queue_item(item_id: int, reviewer: str, resolution: str) -> Optional[dict]:
    conn().execute(
        "UPDATE human_queue SET status = 'done', reviewer = ?, resolution = ?, decided_at = ? WHERE id = ? AND status = 'open'",
        (reviewer, resolution, now(), item_id),
    )
    row = conn().execute("SELECT * FROM human_queue WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


# ---------- companies and accounts ----------

def upsert_company(crustdata_id: int, name: str, primary_domain: Optional[str], alias: Optional[str]) -> list[str]:
    row = conn().execute("SELECT aliases_json FROM companies WHERE crustdata_id = ?", (crustdata_id,)).fetchone()
    aliases = set(json.loads(row["aliases_json"])) if row else set()
    for a in (name, alias):
        if a:
            aliases.add(a.strip())
    conn().execute("INSERT OR REPLACE INTO companies VALUES (?, ?, ?, ?, ?)", (crustdata_id, name, primary_domain, _j(sorted(aliases)), now()))
    return sorted(aliases)


def get_account(company_id: int) -> Optional[dict]:
    row = conn().execute("SELECT * FROM accounts WHERE company_id = ?", (company_id,)).fetchone()
    return dict(row) if row else None


def upsert_account(company_id: int, **fields: Any) -> None:
    acct = get_account(company_id) or {"company_id": company_id, "limit_inr": 0, "terms_days": 0, "outstanding_inr": 0,
                                       "advance_pct": 0, "status": "new", "last_case_id": None}
    acct.update(fields)
    acct["updated_at"] = now()
    conn().execute(
        "INSERT OR REPLACE INTO accounts VALUES (:company_id, :limit_inr, :terms_days, :outstanding_inr, :advance_pct, :status, :last_case_id, :updated_at)",
        acct,
    )


def list_accounts() -> list[dict]:
    return [dict(r) for r in conn().execute(
        "SELECT a.*, c.name FROM accounts a LEFT JOIN companies c ON c.crustdata_id = a.company_id ORDER BY a.updated_at DESC")]


# ---------- watch events ----------

def add_watch_event(company_id: int, kind: str, payload: dict, simulated: bool) -> int:
    cur = conn().execute(
        "INSERT INTO watch_events (company_id, kind, payload_json, simulated, received_at) VALUES (?, ?, ?, ?, ?)",
        (company_id, kind, _j(payload), int(simulated), now()),
    )
    return cur.lastrowid


# ---------- costs and caches ----------

def log_cost(case_id: Optional[str], endpoint: str, credits: float, note: str = "") -> None:
    conn().execute("INSERT INTO cost_log (case_id, endpoint, credits, note, at) VALUES (?, ?, ?, ?, ?)", (case_id, endpoint, credits, note, now()))


def case_cost(case_id: str) -> float:
    return conn().execute("SELECT COALESCE(SUM(credits), 0) FROM cost_log WHERE case_id = ?", (case_id,)).fetchone()[0]


def cache_get(key: str) -> Any:
    row = conn().execute("SELECT response_json FROM api_cache WHERE key = ?", (key,)).fetchone()
    return json.loads(row["response_json"]) if row else None


def cache_put(key: str, path: str, body: Any, response: Any) -> None:
    conn().execute("INSERT OR REPLACE INTO api_cache VALUES (?, ?, ?, ?, ?)", (key, path, _j(body), _j(response), now()))


def llm_cache_get(key: str) -> Any:
    row = conn().execute("SELECT model, response_json FROM llm_cache WHERE key = ?", (key,)).fetchone()
    return (json.loads(row["response_json"]), row["model"]) if row else None


def llm_cache_put(key: str, model: str, response: Any) -> None:
    conn().execute("INSERT OR REPLACE INTO llm_cache VALUES (?, ?, ?, ?)", (key, model, _j(response), now()))
