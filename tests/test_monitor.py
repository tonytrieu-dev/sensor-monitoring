"""Shared temperature/pressure behavior and temperature-specific alarm tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

from sensor_monitor.models import (
    AlertStatus,
    AlertType,
    MeasurementType,
    MonitoringConfig,
    SensorReading,
    SensorState,
)
from sensor_monitor.monitor import AlertStore, SensorMonitor
from sensor_monitor.parsing import QuarantineRecord
from sensor_monitor.rules import HighTemperatureRule, RateOfRiseRule

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


def temp_reading(value, offset_seconds=0.0):
    return SensorReading(
        timestamp=BASE + timedelta(seconds=offset_seconds),
        sensor_id="T-101",
        measurement_type=MeasurementType.TEMPERATURE,
        value=value,
        unit="celsius",
    )


class HappyPath(unittest.TestCase):
    def test_subsecond_timestamps_are_accepted(self):
        # Sub-second timestamps are valid input; Python 3.10's fromisoformat is
        # picky about fractional-second length, so cover 1, 3, and 6 digits.
        monitor = SensorMonitor(CONFIG)
        for ts in [
            "2026-08-20T14:05:00.5Z",
            "2026-08-20T14:05:01.250Z",
            "2026-08-20T14:05:02.123456Z",
        ]:
            with self.subTest(timestamp=ts):
                result = monitor.process(
                    {"timestamp": ts, "sensor_id": "T-9", "temperature_c": 600.0},
                    now=BASE + timedelta(seconds=5),
                )
                self.assertNotIsInstance(result, QuarantineRecord)

    def test_steady_normal_stream_produces_no_alerts(self):
        for field, baseline in [("temperature_c", 600), ("pressure_bar", 3)]:
            with self.subTest(measurement=field):
                sample = partial(reading, measurement_field=field)
                monitor = SensorMonitor(CONFIG)
                values = [baseline + change for change in [0, 0.1, 0.2, 0.25, 0.3]]
                for i, value in enumerate(values):
                    monitor.process(
                        sample(value, offset_seconds=i), now=BASE + timedelta(seconds=i)
                    )

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

    def test_high_temperature_boundary_is_strict(self):
        # Exactly at the alarm threshold must not raise; just above must.
        high = HighTemperatureRule()
        empty = SensorState(sensor_id="T-101")
        self.assertIsNone(high.evaluate(temp_reading(675.0), empty, CONFIG))
        self.assertEqual(
            high.evaluate(temp_reading(675.01), empty, CONFIG),
            AlertType.HIGH_TEMPERATURE,
        )

    def test_rate_rule_guards_non_positive_elapsed(self):
        # A reading sharing the previous timestamp would divide by zero; the rule
        # must return None instead. Only reachable at the rule level, since the
        # monitor rejects same-timestamp readings as duplicates upstream.
        rate = RateOfRiseRule()
        state = SensorState(
            sensor_id="T-101", last_accepted_reading=temp_reading(600.0)
        )
        self.assertIsNone(rate.evaluate(temp_reading(900.0), state, CONFIG))

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
                    monitor.process(
                        sample(baseline, seconds), now=BASE + timedelta(seconds=seconds)
                    )
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


class EndToEndScenario(unittest.TestCase):
    def test_realistic_mixed_stream_produces_expected_timeline(self):
        # Temperature and pressure drive the engine in one run: temp spikes and
        # recovers, pressure flatlines, both go stale; asserts the full timeline.
        monitor = SensorMonitor(CONFIG)
        now = BASE + timedelta(seconds=6)

        def temp(value, seconds):
            return reading(value, seconds)

        def press(seconds):
            return reading(
                3.0, seconds, sensor_id="P-204", measurement_field="pressure_bar"
            )

        stream = [
            temp(600.0, 0),
            press(0),
            press(1),
            temp(700.0, 2),
            press(2),
            press(3),
            temp("bad", 3),
            temp(600.0, 4),
            press(4),
            press(5),
        ]
        quarantines = []
        for raw in stream:
            out = monitor.process(raw, now=now)
            if isinstance(out, QuarantineRecord):
                quarantines.append(out.reason)
        monitor.check_stale_sensors(BASE + timedelta(seconds=12))

        self.assertEqual(len(quarantines), 1)
        self.assertIn("nonnumeric", quarantines[0])

        timeline = [
            (a.sensor_id, a.alert_type, a.status) for a in monitor.store.alerts
        ]
        self.assertEqual(
            timeline,
            [
                ("T-101", AlertType.HIGH_TEMPERATURE, AlertStatus.RAISED),
                ("T-101", AlertType.RATE_OF_RISE, AlertStatus.RAISED),
                ("T-101", AlertType.HIGH_TEMPERATURE, AlertStatus.CLEARED),
                ("T-101", AlertType.RATE_OF_RISE, AlertStatus.CLEARED),
                ("P-204", AlertType.FLATLINE, AlertStatus.RAISED),
                ("T-101", AlertType.STALE_DATA, AlertStatus.RAISED),
                ("P-204", AlertType.STALE_DATA, AlertStatus.RAISED),
            ],
        )


if __name__ == "__main__":
    unittest.main()
