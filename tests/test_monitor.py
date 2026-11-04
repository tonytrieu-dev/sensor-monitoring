"""Behavior tests for validation, chronology, detection, and alert transitions."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sensor_monitor.models import AlertStatus, AlertType, MonitoringConfig, SensorReading
from sensor_monitor.monitor import AlertStore, SensorMonitor, format_for_operator
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


def reading(value, offset_seconds=0.0, sensor_id="T-101"):
    moment = BASE + timedelta(seconds=offset_seconds)
    return {
        "timestamp": moment.isoformat().replace("+00:00", "Z"),
        "sensor_id": sensor_id,
        "temperature_c": value,
    }


class HappyPath(unittest.TestCase):
    def test_valid_reading_is_accepted_and_returned(self):
        monitor = SensorMonitor(CONFIG)
        result = monitor.process(reading(600.0, 0))

        assert isinstance(result, SensorReading), result
        self.assertEqual(result.sensor_id, "T-101")
        self.assertEqual(result.value, 600.0)
        self.assertEqual(result.unit, "celsius")

    def test_valid_reading_updates_per_sensor_state(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600.0, 0))

        state = monitor.state_for("T-101")
        assert state.last_accepted_reading is not None
        self.assertEqual(state.last_accepted_reading.value, 600.0)
        self.assertEqual(state.last_valid_timestamp, BASE)

    def test_steady_normal_stream_produces_no_alerts(self):
        # A realistic quiet stream: in-band values drifting slowly enough to stay
        # under the 10 C/min rate limit (at 1 Hz that means <= ~0.167 C/sec), and
        # changing just enough to avoid the flatline detector. Must stay silent.
        monitor = SensorMonitor(CONFIG)
        values = [600.0, 600.1, 600.2, 600.25, 600.3]
        for i, value in enumerate(values):
            monitor.process(reading(value, offset_seconds=i))

        self.assertEqual(monitor.store.alerts, [])
        accepted = monitor.state_for("T-101").last_accepted_reading
        assert accepted is not None
        self.assertEqual(accepted.value, 600.3)


class RegressionAnchors(unittest.TestCase):
    def test_duplicate_does_not_mutate_state(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600.0, 0))
        anchor = monitor.state_for("T-101").last_accepted_reading

        result = monitor.process(reading(999.0, 0))  # same timestamp, new value

        assert isinstance(result, QuarantineRecord), result
        self.assertIn("duplicate", result.reason)
        self.assertIs(monitor.state_for("T-101").last_accepted_reading, anchor)

    def test_stale_alert_not_re_emitted(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600.0, 0))

        first = monitor.check_stale_sensors(BASE + timedelta(seconds=7))
        second = monitor.check_stale_sensors(BASE + timedelta(seconds=8))

        self.assertEqual([(a.alert_type, a.status) for a in first],
                         [(AlertType.STALE_DATA, AlertStatus.RAISED)])
        self.assertEqual(second, [])


class OutOfOrderAndRate(unittest.TestCase):
    def test_out_of_order_does_not_feed_rate_of_rise(self):
        # A late low reading must not become the baseline for a later rate calc,
        # which would fabricate a huge, misleading rate of rise.
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600.0, 10))
        rejected = monitor.process(reading(100.0, 0))  # older than last accepted
        assert isinstance(rejected, QuarantineRecord), rejected
        self.assertIn("out-of-order", rejected.reason)

        monitor.process(reading(605.0, 70))  # 5 over 1 min from the 600 baseline
        rate_alerts = [
            a for a in monitor.store.alerts if a.alert_type == AlertType.RATE_OF_RISE
        ]
        self.assertEqual(rate_alerts, [])  # 5/min < 10/min; baseline stayed 600


class SensorIsolation(unittest.TestCase):
    def test_one_sensor_stale_does_not_affect_another(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600.0, 0, sensor_id="T-101"))
        monitor.process(reading(600.0, 6, sensor_id="T-202"))

        stale = monitor.check_stale_sensors(BASE + timedelta(seconds=7))
        stale_ids = {a.sensor_id for a in stale if a.status == AlertStatus.RAISED}
        self.assertEqual(stale_ids, {"T-101"})


class EndToEnd(unittest.TestCase):
    def test_stream_records_every_transition_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.jsonl"
            monitor = SensorMonitor(CONFIG, store=AlertStore(path))
            for value, seconds in [(600, 0), (660, 1), (700, 2), (600, 4)]:
                monitor.process(reading(value, seconds), now=BASE + timedelta(seconds=seconds))
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([(r["alert_type"], r["status"]) for r in records], [
                ("rate_of_rise", "raised"),
                ("out_of_band", "raised"),
                ("high_temperature", "raised"),
                ("out_of_band", "cleared"),
                ("high_temperature", "cleared"),
                ("rate_of_rise", "cleared"),
            ])


class AuditRegressions(unittest.TestCase):
    def test_high_temperature_at_limit_does_not_raise(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(675), now=BASE)
        self.assertEqual(
            [a.alert_type for a in monitor.store.alerts], [AlertType.OUT_OF_BAND]
        )

    def test_rate_at_limit_does_not_raise(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600), now=BASE)
        monitor.process(reading(610, 60), now=BASE + timedelta(seconds=60))
        self.assertEqual(monitor.store.alerts, [])

    def test_stale_starts_only_after_limit(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600), now=BASE)
        self.assertEqual(monitor.check_stale_sensors(BASE + timedelta(seconds=5)), [])
        alerts = monitor.check_stale_sensors(BASE + timedelta(seconds=5.001))
        self.assertEqual([a.alert_type for a in alerts], [AlertType.STALE_DATA])

    def test_low_band_warning_records_lower_bound(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(500), now=BASE)
        alert, = monitor.store.alerts
        self.assertEqual(alert.alert_type, AlertType.OUT_OF_BAND)
        self.assertEqual(alert.threshold, 550)

    def test_flatline_uses_seconds_and_restarts_after_stale_gap(self):
        monitor = SensorMonitor(CONFIG)
        for seconds in [0, 11, 12, 13, 14, 15, 16]:
            monitor.process(reading(600, seconds))
            flat = [a for a in monitor.store.alerts if a.alert_type == AlertType.FLATLINE]
            with self.subTest(elapsed_seconds=seconds):
                self.assertEqual([a.status for a in flat],
                                 [AlertStatus.RAISED] if seconds == 16 else [])
        self.assertEqual(flat[0].observed_value, 5)
        self.assertEqual(flat[0].unit, 'seconds')
        self.assertEqual(flat[0].threshold, 5)

    def test_operator_line_preserves_time_severity_sensor_condition_and_status_order(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(700), now=BASE)
        alert, = monitor.store.alerts
        fields = format_for_operator(alert).split()
        self.assertEqual(fields[:4], [
            "[2026-08-20T14:05:00Z]", "ALARM", "T-101", "high_temperature",
        ])
        self.assertEqual(fields[4:-2], alert.message.split())
        self.assertEqual(fields[-2:], ["->", "RAISED"])

    def test_store_reopening_preserves_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'alerts.jsonl'
            SensorMonitor(CONFIG, store=AlertStore(path)).process(reading(700), now=BASE)
            original = path.read_bytes()
            SensorMonitor(CONFIG, store=AlertStore(path)).process(reading(710), now=BASE)
            self.assertTrue(path.read_bytes().startswith(original))
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([r["observed_value"] for r in records], [700, 710])

    def test_validation_reasons_and_stale_timer(self):
        cases = [
            ({'timestamp': '2026-08-20T14:05:01'}, 'naive timestamp'),
            ({'timestamp': '2026-08-20T14:05:10Z'}, 'future-dated'),
            ({'temperature_c': 'hot'}, 'nonnumeric'),
            ({'temperature_c': True}, 'boolean value'),
            ({'temperature_c': -500}, 'physical range'),
            ({'timestamp': None}, 'nonstring timestamp'),
            ({'timestamp': 'bad'}, 'unparseable timestamp'),
            ({'temperature_c': 10**400}, 'unrepresentable'),
            ({'timestamp': '0001-01-01T00:00:00+14:00'}, 'unrepresentable'),
        ]
        monitor = SensorMonitor(CONFIG)
        anchor = monitor.process(reading(600), now=BASE)
        for changes, reason in cases:
            with self.subTest(changes=changes):
                result = monitor.process(reading(600, 1) | changes, now=BASE)
                assert isinstance(result, QuarantineRecord), result
                self.assertIn(reason, result.reason)
                self.assertIs(monitor.state_for('T-101').last_accepted_reading, anchor)
        for raw, reason in [(None, 'not an object'), ({}, 'sensor_id'),
                            ({'sensor_id': 'T-101'}, 'measurement')]:
            with self.subTest(raw=raw):
                result = monitor.process(raw, now=BASE)
                assert isinstance(result, QuarantineRecord), result
                self.assertIn(reason, result.reason)
        self.assertEqual(len(monitor.check_stale_sensors(BASE + timedelta(seconds=6))), 1)

    def test_timezone_normalization(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600), now=BASE)
        result = monitor.process(reading(601) | {'timestamp': '2026-08-20T16:05:00+02:00'}, now=BASE)
        assert isinstance(result, QuarantineRecord), result
        self.assertIn('duplicate', result.reason)

    def test_persistent_high_and_rate_emit_only_transitions(self):
        monitor = SensorMonitor(CONFIG)
        for value, seconds in [(600, 0), (700, 60), (720, 120), (740, 180), (600, 240), (600, 241)]:
            monitor.process(reading(value, seconds))
        for kind in [AlertType.HIGH_TEMPERATURE, AlertType.RATE_OF_RISE]:
            with self.subTest(condition=kind):
                self.assertEqual([a.status for a in monitor.store.alerts if a.alert_type == kind],
                                 [AlertStatus.RAISED, AlertStatus.CLEARED])

    def test_rate_rule_ignores_zero_and_negative_intervals(self):
        from dataclasses import replace
        from sensor_monitor.rules import RateOfRiseRule

        monitor = SensorMonitor(CONFIG)
        previous = monitor.process(reading(600), now=BASE)
        assert isinstance(previous, SensorReading), previous
        state = monitor.state_for("T-101")
        for seconds in [0, -.5]:
            with self.subTest(elapsed_seconds=seconds):
                candidate = replace(previous, value=900, timestamp=BASE + timedelta(seconds=seconds))
                self.assertIsNone(RateOfRiseRule().evaluate(candidate, state, CONFIG))

    def test_stale_clears_once_on_poll_after_recovery(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600))
        monitor.check_stale_sensors(BASE + timedelta(seconds=6))
        monitor.process(reading(601, 7))
        self.assertEqual([a.status for a in monitor.check_stale_sensors(BASE + timedelta(seconds=7))], [AlertStatus.CLEARED])
        self.assertEqual(monitor.check_stale_sensors(BASE + timedelta(seconds=8)), [])


if __name__ == "__main__":
    unittest.main()
