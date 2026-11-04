"""Regression tests for persistence failures and configuration safety."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sensor_monitor.models import AlertStatus, MonitoringConfig
from sensor_monitor.monitor import AlertStore, SensorMonitor

BASE = datetime(2026, 8, 20, 14, 5, tzinfo=timezone.utc)
RAW_CONFIG = {
    "normal_range_c": [550.0, 650.0],
    "high_temperature_alarm_c": 675.0,
    "max_rate_of_rise_c_per_min": 10.0,
    "stale_data_threshold_s": 5,
}
CONFIG = MonitoringConfig.from_raw(RAW_CONFIG)


def reading(value: float, seconds: float = 0) -> dict:
    return {
        "timestamp": (BASE + timedelta(seconds=seconds)).isoformat(),
        "sensor_id": "T-101",
        "temperature_c": value,
    }


class PersistenceFailures(unittest.TestCase):
    def test_failed_raise_is_written_when_same_reading_is_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "records"
            path = parent / "alerts.jsonl"
            monitor = SensorMonitor(CONFIG, store=AlertStore(path))
            with self.assertRaises(FileNotFoundError):
                monitor.process(reading(700), now=BASE)
            self.assertEqual(monitor.store.alerts, [])
            self.assertIsNone(monitor.state_for("T-101").last_accepted_reading)

            parent.mkdir()
            monitor.process(reading(700), now=BASE)
            self.assertTrue(path.is_file(), "Expected a persisted alert file")
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(records), 1, "Expected exactly one persisted alert")
            record = records[0]
            self.assertEqual((record["alert_type"], record["status"]),
                             ("high_temperature", "raised"))
            self.assertEqual([a.status for a in monitor.store.alerts], [AlertStatus.RAISED])

    def test_failed_clear_is_written_when_recovery_reading_is_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.jsonl"
            monitor = SensorMonitor(CONFIG, store=AlertStore(path))
            monitor.process(reading(700), now=BASE)
            saved = path.read_text()
            path.unlink()
            path.mkdir()
            with self.assertRaises(IsADirectoryError):
                monitor.process(reading(600, 1), now=BASE)
            accepted = monitor.state_for("T-101").last_accepted_reading
            assert accepted is not None
            self.assertEqual(accepted.value, 700)
            self.assertEqual([a.status for a in monitor.store.alerts], [AlertStatus.RAISED])

            path.rmdir()
            path.write_text(saved)
            monitor.process(reading(600, 1), now=BASE)
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([(r["alert_type"], r["status"]) for r in records],
                             [("high_temperature", "raised"), ("high_temperature", "cleared")])
            self.assertEqual([a.status for a in monitor.store.alerts],
                             [AlertStatus.RAISED, AlertStatus.CLEARED])

    def test_stored_rate_evidence_reconstructs_subsecond_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.jsonl"
            monitor = SensorMonitor(CONFIG, store=AlertStore(path))
            monitor.process(reading(600), now=BASE)
            monitor.process(reading(601, .5), now=BASE)
            self.assertTrue(path.is_file(), "Expected a persisted alert file")
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(records), 1, "Expected exactly one persisted alert")
            record = records[0]
            self.assertEqual(record["alert_type"], "rate_of_rise")
            self.assertEqual(record["previous_value"], 600)
            self.assertEqual(record["previous_timestamp"], "2026-08-20T14:05:00Z")
            self.assertEqual(record["observed_value"], 601)
            self.assertEqual(record["elapsed_minutes"], .5 / 60)
            self.assertEqual(record["observed_rise_degrees_celsius_per_minute"], 120)
