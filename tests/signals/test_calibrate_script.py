"""Smoke test: the calibration script runs end to end on the fake worlds (so it does not break at 2 AM)."""
import datetime as dt
import importlib.util
from pathlib import Path

from fakes import FakeClient, healthy_world, zombie_world

spec = importlib.util.spec_from_file_location("calibrate", Path(__file__).resolve().parents[2] / "scripts" / "calibrate.py")
cal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cal)


def _merged() -> FakeClient:
    h, z = healthy_world(), zombie_world()
    return FakeClient(companies={**h.companies, **z.companies}, people=h.people + z.people, jobs=h.jobs + z.jobs,
                      news=[], web=h.web)


def test_calibration_table_scores_both_worlds(use_client):
    use_client(_merged())
    rows = [cal.run_one("Healthyco", "#101", "healthy", None), cal.run_one("Zombieco", "#202", "distressed", None)]
    assert [r["verdict"] for r in rows] == ["right", "right"]
    md = cal.table(rows, None)
    assert "| Healthyco #101 | healthy |" in md and "2 of 2 right. False alarms: none. Missed: none." in md


def test_calibration_point_in_time_and_false_alarm_scoring(use_client):
    use_client(_merged())
    row = cal.run_one("Zombieco", "#202", "healthy", dt.date(2023, 10, 1))     # deliberately mislabelled
    assert row["verdict"] == "FALSE ALARM" and "joiner_rate red" in row["flags"]
    assert row["profiles"] == 2750
