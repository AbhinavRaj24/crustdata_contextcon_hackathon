import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests/fakes.py
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))       # the repo root (termsdesk package)

from termsdesk import config, llm, signals, store  # noqa: E402

TODAY = dt.date(2026, 11, 6)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Fresh SQLite file per test, no model calls, and a fixed 'today'."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    store._local.__dict__.clear()
    monkeypatch.setattr(llm, "generate", lambda *a, **k: (None, None))
    monkeypatch.setattr(signals, "_today", lambda: TODAY)
    yield
    store._local.__dict__.clear()


@pytest.fixture
def use_client(monkeypatch):
    def _use(fake):
        monkeypatch.setattr(signals, "client", lambda: fake)
        return fake
    return _use
