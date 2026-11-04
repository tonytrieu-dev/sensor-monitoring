"""Shared temperature/pressure behavior and temperature-specific alarm tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

from sensor_monitor.models import AlertStatus, AlertType, MonitoringConfig
from sensor_monitor.monitor import AlertStore, SensorMonitor
from sensor_monitor.parsing import QuarantineRecord

CONFIG = MonitoringConfig.from_raw(
    {
        "normal_range_c": [550.0, 650.0],
        "high_temperature_alarm_c": 675.0,
        "max_rate_of_rise_c_per_min": 10.0,
        "stale_data_threshold_s": 5,
    }
)
BASE = datetime(2026, 8, 20, 14, 5, 0, tzinfo=timezone.utc)


def reading(
    value, offset_seconds=0.0, sensor_id="T-101", measurement_field="temperature_c"
):
    moment = BASE + timedelta(seconds=offset_seconds)
    return {
        "timestamp": moment.isoformat().replace("+00:00", "Z"),
        "sensor_id": sensor_id,
        measurement_field: value,
    }


class HappyPath(unittest.TestCase):
    def test_steady_normal_stream_produces_no_alerts(self):
        for field, baseline in [("temperature_c", 600), ("pressure_bar", 3)]:
            with self.subTest(measurement=field):
                sample = partial(reading, measurement_field=field)
                monitor = SensorMonitor(CONFIG)
                values = [baseline + change for change in [0, 0.1, 0.2, 0.25, 0.3]]
                for i, value in enumerate(values):
                    monitor.process(sample(value, offset_seconds=i))

                self.assertEqual(monitor.store.alerts, [])
                accepted = monitor.state_for("T-101").last_accepted_reading
                assert accepted is not None
                self.assertEqual(accepted.value, baseline + 0.3)


class RegressionAnchors(unittest.TestCase):
    def test_duplicate_and_older_readings_preserve_the_rate_baseline(self):
        for field, baseline in [("temperature_c", 600), ("pressure_bar", 3)]:
            with self.subTest(measurement=field):
                sample = partial(reading, measurement_field=field)
                monitor = SensorMonitor(CONFIG)
                monitor.process(sample(baseline, 10), now=BASE + timedelta(seconds=10))
                anchor = monitor.state_for("T-101").last_accepted_reading
                cases = [
                    (sample(999, 10), "duplicate"),
                    (sample(100, 0), "out-of-order"),
                    (
                        sample(999) | {"timestamp": "2026-08-20T16:05:10+02:00"},
                        "duplicate",
                    ),
                ]
                for raw, reason in cases:
                    with self.subTest(reason=reason, timestamp=raw["timestamp"]):
                        result = monitor.process(raw, now=BASE + timedelta(seconds=10))
                        assert isinstance(result, QuarantineRecord), result
                        self.assertIn(reason, result.reason)
                        self.assertIs(
                            monitor.state_for("T-101").last_accepted_reading, anchor
                        )
                monitor.process(
                    sample(baseline + 5, 70), now=BASE + timedelta(seconds=70)
                )
                self.assertEqual(monitor.store.alerts, [])


class SensorIsolation(unittest.TestCase):
    def test_one_sensor_stale_does_not_affect_another(self):
        for stale_field, fresh_field in [
            ("temperature_c", "pressure_bar"),
            ("pressure_bar", "temperature_c"),
        ]:
            with self.subTest(stale=stale_field, fresh=fresh_field):
                monitor = SensorMonitor(CONFIG)
                monitor.process(
                    reading(
                        600 if stale_field == "temperature_c" else 3,
                        sensor_id="S-101",
                        measurement_field=stale_field,
                    ),
                    now=BASE,
                )
                monitor.process(
                    reading(
                        600 if fresh_field == "temperature_c" else 3,
                        6,
                        sensor_id="S-202",
                        measurement_field=fresh_field,
                    ),
                    now=BASE + timedelta(seconds=6),
                )
                stale = monitor.check_stale_sensors(BASE + timedelta(seconds=7))
                self.assertEqual(
                    [(a.sensor_id, a.alert_type) for a in stale],
                    [("S-101", AlertType.STALE_DATA)],
                )


class AuditRegressions(unittest.TestCase):
    def test_rate_at_limit_does_not_raise(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600), now=BASE)
        monitor.process(reading(610, 60), now=BASE + timedelta(seconds=60))
        self.assertEqual(monitor.store.alerts, [])

    def test_low_band_warning_records_lower_bound(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(500), now=BASE)
        (alert,) = monitor.store.alerts
        self.assertEqual(alert.alert_type, AlertType.OUT_OF_BAND)
        self.assertEqual(alert.threshold, 550)

    def test_flatline_uses_seconds_and_restarts_after_stale_gap(self):
        for field, baseline in [("temperature_c", 600), ("pressure_bar", 3)]:
            with self.subTest(measurement=field):
                sample = partial(reading, measurement_field=field)
                monitor = SensorMonitor(CONFIG)
                for seconds in [0, 11, 12, 13, 14, 15, 16]:
                    monitor.process(sample(baseline, seconds))
                    flat = [
                        a
                        for a in monitor.store.alerts
                        if a.alert_type == AlertType.FLATLINE
                    ]
                    with self.subTest(elapsed_seconds=seconds):
                        self.assertEqual(
                            [a.status for a in flat],
                            [AlertStatus.RAISED] if seconds == 16 else [],
                        )
                self.assertEqual(flat[0].observed_value, 5)
                self.assertEqual(flat[0].unit, "seconds")
                self.assertEqual(flat[0].threshold, 5)

                monitor.process(
                    sample(baseline + 1, 17), now=BASE + timedelta(seconds=17)
                )
                flat = [
                    a
                    for a in monitor.store.alerts
                    if a.alert_type == AlertType.FLATLINE
                ]
                self.assertEqual(
                    [a.status for a in flat], [AlertStatus.RAISED, AlertStatus.CLEARED]
                )

    def test_store_reopening_preserves_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.jsonl"
            SensorMonitor(CONFIG, store=AlertStore(path)).process(
                reading(700), now=BASE
            )
            original = path.read_bytes()
            SensorMonitor(CONFIG, store=AlertStore(path)).process(
                reading(710), now=BASE
            )
            self.assertTrue(path.read_bytes().startswith(original))
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([r["observed_value"] for r in records], [700, 710])

    def test_validation_reasons_and_stale_timer(self):
        for field, baseline in [("temperature_c", 600), ("pressure_bar", 3)]:
            with self.subTest(measurement=field):
                sample = partial(reading, measurement_field=field)
                cases = [
                    ({"timestamp": "2026-08-20T14:05:01"}, "naive timestamp"),
                    ({"timestamp": "2026-08-20T14:05:10Z"}, "future-dated"),
                    ({field: "hot"}, "nonnumeric"),
                    ({field: True}, "boolean value"),
                    (
                        {field: -500},
                        "physical range" if field == "temperature_c" else "pressure",
                    ),
                    ({"timestamp": None}, "nonstring timestamp"),
                    ({"timestamp": "bad"}, "unparseable timestamp"),
                    ({field: 10**400}, "unrepresentable"),
                    ({"timestamp": "0001-01-01T00:00:00+14:00"}, "unrepresentable"),
                ]
                monitor = SensorMonitor(CONFIG)
                anchor = monitor.process(sample(baseline), now=BASE)
                for changes, reason in cases:
                    with self.subTest(changes=changes):
                        result = monitor.process(
                            sample(baseline, 1) | changes, now=BASE
                        )
                        assert isinstance(result, QuarantineRecord), result
                        self.assertIn(reason, result.reason)
                        self.assertIs(
                            monitor.state_for("T-101").last_accepted_reading, anchor
                        )
                for raw, reason in [
                    (None, "not an object"),
                    ({}, "sensor_id"),
                    ({"sensor_id": "T-101"}, "measurement"),
                ]:
                    with self.subTest(raw=raw):
                        result = monitor.process(raw, now=BASE)
                        assert isinstance(result, QuarantineRecord), result
                        self.assertIn(reason, result.reason)
                self.assertEqual(
                    len(monitor.check_stale_sensors(BASE + timedelta(seconds=6))), 1
                )

    def test_persistent_high_and_rate_emit_only_transitions(self):
        monitor = SensorMonitor(CONFIG)
        for value, seconds in [
            (675, 0),
            (700, 60),
            (720, 120),
            (740, 180),
            (600, 240),
            (600, 241),
        ]:
            monitor.process(
                reading(value, seconds), now=BASE + timedelta(seconds=seconds)
            )
            if seconds == 0:
                self.assertEqual(
                    [a.alert_type for a in monitor.store.alerts],
                    [AlertType.OUT_OF_BAND],
                )
        for kind in [AlertType.HIGH_TEMPERATURE, AlertType.RATE_OF_RISE]:
            with self.subTest(condition=kind):
                self.assertEqual(
                    [a.status for a in monitor.store.alerts if a.alert_type == kind],
                    [AlertStatus.RAISED, AlertStatus.CLEARED],
                )

    def test_stale_raises_once_after_limit_and_clears_on_recovery(self):
        for field, baseline in [("temperature_c", 600), ("pressure_bar", 3)]:
            with self.subTest(measurement=field):
                sample = partial(reading, measurement_field=field)
                monitor = SensorMonitor(CONFIG)
                monitor.process(sample(baseline), now=BASE)
                self.assertEqual(
                    monitor.check_stale_sensors(BASE + timedelta(seconds=5)), []
                )
                first = monitor.check_stale_sensors(BASE + timedelta(seconds=6))
                self.assertEqual(
                    [(a.alert_type, a.status) for a in first],
                    [(AlertType.STALE_DATA, AlertStatus.RAISED)],
                )
                self.assertEqual(
                    monitor.check_stale_sensors(BASE + timedelta(seconds=6.5)), []
                )
                monitor.process(
                    sample(baseline + 1, 7), now=BASE + timedelta(seconds=7)
                )
                self.assertEqual(
                    [
                        a.status
                        for a in monitor.check_stale_sensors(
                            BASE + timedelta(seconds=7)
                        )
                    ],
                    [AlertStatus.CLEARED],
                )
                self.assertEqual(
                    monitor.check_stale_sensors(BASE + timedelta(seconds=8)), []
                )


if __name__ == "__main__":
    unittest.main()
