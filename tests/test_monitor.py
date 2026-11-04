"""Cross-module behavior tests.

Per-module contracts are covered by each module's own self-check (run
`python -m sensor_monitor.<module>`). This suite covers what those cannot: the
monitor coordinating validation + state + detection + FSM + persistence
end-to-end, plus regression anchors for the two invariants most likely to
silently break (duplicate must not mutate state; stale must not re-emit).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sensor_monitor.models import AlertStatus, AlertType, MonitoringConfig
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
    """Mission-critical normal operation. If these break, the component's core
    job is dead even when the failure-mode tests still pass, so they are
    asserted explicitly."""

    def test_valid_reading_is_accepted_and_returned(self):
        monitor = SensorMonitor(CONFIG)
        result = monitor.process(reading(600.0, 0))

        self.assertNotIsInstance(result, QuarantineRecord)
        self.assertEqual(result.sensor_id, "T-101")
        self.assertEqual(result.value, 600.0)
        self.assertEqual(result.unit, "celsius")

    def test_valid_reading_updates_per_sensor_state(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600.0, 0))

        state = monitor.state_for("T-101")
        self.assertIsNotNone(state.last_accepted_reading)
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
        self.assertEqual(monitor.state_for("T-101").last_accepted_reading.value, 600.3)

    def test_not_stale_within_threshold_emits_nothing(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600.0, 0))
        # Checked 3s later, within the 5s stale threshold: healthy, no alert.
        emitted = monitor.check_stale_sensors(BASE + timedelta(seconds=3))
        self.assertEqual(emitted, [])


class RegressionAnchors(unittest.TestCase):
    """These guard invariants that are easy to break with an innocent-looking
    refactor and hard to notice in production. Each fails loudly if violated."""

    def test_duplicate_does_not_mutate_state(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600.0, 0))
        anchor = monitor.state_for("T-101").last_accepted_reading

        result = monitor.process(reading(999.0, 0))  # same timestamp, new value

        self.assertIsInstance(result, QuarantineRecord)
        self.assertIn("duplicate", result.reason)
        self.assertIs(monitor.state_for("T-101").last_accepted_reading, anchor)

    def test_stale_alert_not_re_emitted(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600.0, 0))

        first = monitor.check_stale_sensors(BASE + timedelta(seconds=7))
        second = monitor.check_stale_sensors(BASE + timedelta(seconds=8))

        raised_first = [a for a in first if a.status == AlertStatus.RAISED]
        raised_second = [a for a in second if a.status == AlertStatus.RAISED]
        self.assertEqual(len(raised_first), 1)
        self.assertEqual(raised_second, [])


class OutOfOrderAndRate(unittest.TestCase):
    def test_out_of_order_does_not_feed_rate_of_rise(self):
        # A late low reading must not become the baseline for a later rate calc,
        # which would fabricate a huge, misleading rate of rise.
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600.0, 10))
        rejected = monitor.process(reading(100.0, 0))  # older than last accepted
        self.assertIsInstance(rejected, QuarantineRecord)
        self.assertIn("out-of-order", rejected.reason)

        monitor.process(reading(605.0, 70))  # 5 over 1 min from the 600 baseline
        rate_alerts = [
            a for a in monitor.store.alerts if a.alert_type == AlertType.RATE_OF_RISE
        ]
        self.assertEqual(rate_alerts, [])  # 5/min < 10/min; baseline stayed 600

    def test_very_short_interval_rate_uses_real_elapsed(self):
        # Edge case: a sub-second interval produces a large but honest rate, and
        # the alert records the exact interval it used so an operator can judge it.
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600.0, 0))
        monitor.process(reading(601.0, 0.5))  # +1 over 0.5s = 120/min
        rate_alerts = [
            a for a in monitor.store.alerts if a.alert_type == AlertType.RATE_OF_RISE
        ]
        self.assertEqual(len(rate_alerts), 1)
        self.assertAlmostEqual(rate_alerts[0].elapsed_minutes, 0.5 / 60.0)
        self.assertAlmostEqual(
            rate_alerts[0].observed_rise_degrees_celsius_per_minute, 120.0
        )


class SensorIsolation(unittest.TestCase):
    def test_one_sensor_stale_does_not_affect_another(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600.0, 0, sensor_id="T-101"))
        monitor.process(reading(600.0, 6, sensor_id="T-202"))

        stale = monitor.check_stale_sensors(BASE + timedelta(seconds=7))
        stale_ids = {a.sensor_id for a in stale if a.status == AlertStatus.RAISED}
        self.assertEqual(stale_ids, {"T-101"})


class EndToEnd(unittest.TestCase):
    def test_realistic_stream_produces_expected_alerts_state_and_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alerts.jsonl"
            monitor = SensorMonitor(CONFIG, store=AlertStore(path))

            stream = [
                reading(600.0, 0),       # normal baseline
                reading(600.0, 0),       # duplicate -> quarantine
                reading(660.0, 1),       # out of band (warning)
                reading(700.0, 2),       # spike: high-temp + rate-of-rise
                reading("hot", 3),       # malformed -> quarantine
                reading(500.0, 1),       # out-of-order -> quarantine
                reading(600.0, 4),       # back in band -> clears band + high-temp
            ]
            outcomes = [monitor.process(r) for r in stream]

            quarantined = [o for o in outcomes if isinstance(o, QuarantineRecord)]
            self.assertEqual(len(quarantined), 3)

            raised = {
                a.alert_type
                for a in monitor.store.alerts
                if a.status == AlertStatus.RAISED
            }
            self.assertIn(AlertType.OUT_OF_BAND, raised)
            self.assertIn(AlertType.HIGH_TEMPERATURE, raised)
            self.assertIn(AlertType.RATE_OF_RISE, raised)

            cleared = {
                a.alert_type
                for a in monitor.store.alerts
                if a.status == AlertStatus.CLEARED
            }
            self.assertIn(AlertType.HIGH_TEMPERATURE, cleared)

            # Final state reflects the last accepted reading, not the rejected ones.
            final = monitor.state_for("T-101")
            self.assertEqual(final.last_accepted_reading.value, 600.0)
            self.assertEqual(final.last_accepted_reading.timestamp, BASE + timedelta(seconds=4))

            # Every alert was persisted as one valid JSONL line.
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), len(monitor.store.alerts))
            for line in lines:
                record = json.loads(line)
                self.assertIn("event_timestamp", record)
                self.assertIn("message", record)

    def test_operator_line_is_human_readable(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600.0, 0))
        monitor.process(reading(700.0, 1))
        high = next(
            a for a in monitor.store.alerts if a.alert_type == AlertType.HIGH_TEMPERATURE
        )
        line = format_for_operator(high)
        # Assert the line's shape, not just that keywords appear somewhere: it
        # must lead with the bracketed event time, carry the severity and sensor,
        # end with the status, and embed the alert's own structured message.
        self.assertTrue(line.startswith("[2026-08-20T14:05:01Z]"), line)
        self.assertIn("ALARM", line)
        self.assertIn("T-101", line)
        self.assertIn(high.message, line)
        self.assertTrue(line.rstrip().endswith("RAISED"), line)


class AuditRegressions(unittest.TestCase):
    def test_exact_boundaries(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(665, 0))
        monitor.process(reading(675, 60))
        self.assertFalse(any(a.alert_type in {AlertType.HIGH_TEMPERATURE, AlertType.RATE_OF_RISE} for a in monitor.store.alerts))
        self.assertEqual(monitor.check_stale_sensors(BASE + timedelta(seconds=65)), [])

    def test_band_boundary_and_lower_threshold(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(500))
        self.assertEqual(monitor.store.alerts[0].threshold, 550)
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(675))
        self.assertEqual([a.alert_type for a in monitor.store.alerts], [AlertType.OUT_OF_BAND])

    def test_flatline_uses_seconds_and_restarts_after_stale_gap(self):
        monitor = SensorMonitor(CONFIG)
        for seconds in [0, 11, 12, 13, 14, 15, 16]:
            monitor.process(reading(600, seconds))
            flat = [a for a in monitor.store.alerts if a.alert_type == AlertType.FLATLINE]
            self.assertEqual(len(flat), int(seconds == 16))
        self.assertEqual(flat[0].observed_value, 5)
        self.assertEqual(flat[0].unit, 'seconds')
        self.assertEqual(flat[0].threshold, 5)

    def test_operator_order(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(700))
        alert = monitor.store.alerts[0]
        self.assertEqual(format_for_operator(alert),
            f"[2026-08-20T14:05:00Z] ALARM   T-101  high_temperature {alert.message} -> RAISED")

    def test_store_reopening_preserves_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'alerts.jsonl'
            for _ in range(2):
                SensorMonitor(CONFIG, store=AlertStore(path)).process(reading(700))
            self.assertEqual(len(path.read_text().splitlines()), 2)

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
                self.assertIsInstance(result, QuarantineRecord)
                self.assertIn(reason, result.reason)
                self.assertIs(monitor.state_for('T-101').last_accepted_reading, anchor)
        for raw, reason in [(None, 'not an object'), ({}, 'sensor_id'),
                            ({'sensor_id': 'T-101'}, 'measurement')]:
            self.assertIn(reason, monitor.process(raw, now=BASE).reason)
        self.assertEqual(len(monitor.check_stale_sensors(BASE + timedelta(seconds=6))), 1)

    def test_timezone_normalization(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600), now=BASE)
        result = monitor.process(reading(601) | {'timestamp': '2026-08-20T16:05:00+02:00'}, now=BASE)
        self.assertIn('duplicate', result.reason)

    def test_persistent_high_and_rate_emit_only_transitions(self):
        monitor = SensorMonitor(CONFIG)
        for value, seconds in [(600, 0), (700, 60), (720, 120), (740, 180), (600, 240), (600, 241)]:
            monitor.process(reading(value, seconds))
        for kind in [AlertType.HIGH_TEMPERATURE, AlertType.RATE_OF_RISE]:
            self.assertEqual([a.status for a in monitor.store.alerts if a.alert_type == kind],
                             [AlertStatus.RAISED, AlertStatus.CLEARED])

    def test_rate_guard_and_fields(self):
        from sensor_monitor.rules import RateOfRiseRule
        monitor = SensorMonitor(CONFIG)
        previous = monitor.process(reading(600))
        monitor.process(reading(601, .5))
        alert = next(a for a in monitor.store.alerts if a.alert_type == AlertType.RATE_OF_RISE)
        self.assertEqual(alert.previous_value, 600)
        self.assertEqual(alert.previous_timestamp, BASE)
        self.assertEqual(alert.observed_value, 601)
        self.assertEqual(alert.elapsed_minutes, .5 / 60)
        self.assertEqual(alert.observed_rise_degrees_celsius_per_minute, 120)
        state = monitor.state_for('T-101')
        for offset in [0, -.5]:
            candidate = monitor.process(reading(900, offset))
            self.assertIsInstance(candidate, QuarantineRecord)
        self.assertIsNone(RateOfRiseRule().evaluate(previous, state, CONFIG))
        self.assertIsNone(RateOfRiseRule().evaluate(state.last_accepted_reading, state, CONFIG))

    def test_stale_clears_once_on_poll_after_recovery(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(reading(600))
        monitor.check_stale_sensors(BASE + timedelta(seconds=6))
        monitor.process(reading(601, 7))
        self.assertEqual([a.status for a in monitor.check_stale_sensors(BASE + timedelta(seconds=7))], [AlertStatus.CLEARED])
        self.assertEqual(monitor.check_stale_sensors(BASE + timedelta(seconds=8)), [])


if __name__ == "__main__":
    unittest.main()
