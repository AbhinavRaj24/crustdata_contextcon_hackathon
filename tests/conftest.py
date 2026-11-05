"""Tests run against a private copy of the local database, so cached Crustdata and model replies are reused
(cheap, same-day) and test cases never mix with real ones. Live tests: pytest -m live."""
import os
import sqlite3
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_tmp = Path(tempfile.mkdtemp(prefix="termsdesk-test-")) / "test.db"
_src = ROOT / "termsdesk.db"
if _src.exists():
    src, dst = sqlite3.connect(_src), sqlite3.connect(_tmp)
    src.backup(dst)  # consistent copy even while the app or backtest is writing
    dst.execute("DELETE FROM cases"), dst.execute("DELETE FROM outbox"), dst.execute("DELETE FROM accounts")
    dst.commit()
    src.close(), dst.close()
os.environ["TERMSDESK_DB"] = str(_tmp)


def pytest_configure(config):
    config.addinivalue_line("markers", "live: calls Crustdata/Gemini (uses the same-day cache when possible)")
