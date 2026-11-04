"""Pressure shares sensor tracking, without inheriting temperature limits."""

import unittest
from datetime import datetime, timedelta, timezone

from sensor_monitor.models import (
    AlertStatus,
    AlertType,
    MonitoringConfig,
    SensorReading,
)
from sensor_monitor.monitor import SensorMonitor
from sensor_monitor.parsing import QuarantineRecord

BASE = datetime(2026, 8, 20, 14, 5, tzinfo=timezone.utc)
CONFIG = MonitoringConfig.from_raw(
    {
        "normal_range_c": [550, 650],
        "high_temperature_alarm_c": 675,
        "max_rate_of_rise_c_per_min": 10,
        "stale_data_threshold_s": 5,
    }
)


def pressure(value, seconds=0):
    return {
        "sensor_id": "P-101",
        "timestamp": (BASE + timedelta(seconds=seconds)).isoformat(),
        "pressure_bar": value,
    }


class PressureMonitoring(unittest.TestCase):
    def test_mixed_sensors_keep_their_own_rules_and_stale_state(self):
        monitor = SensorMonitor(CONFIG)
        monitor.process(
            {"sensor_id": "T-101", "timestamp": BASE.isoformat(), "temperature_c": 700},
            now=BASE,
        )
        first = monitor.process(pressure(3), now=BASE)
        assert isinstance(first, SensorReading), first
        self.assertEqual(first.unit, "bar")
        monitor.process(pressure(700, 4), now=BASE + timedelta(seconds=4))
        self.assertEqual(
            [(a.sensor_id, a.alert_type) for a in monitor.store.alerts],
            [("T-101", AlertType.HIGH_TEMPERATURE)],
        )
        self.assertEqual(
            [
                a.sensor_id
                for a in monitor.check_stale_sensors(BASE + timedelta(seconds=6))
            ],
            ["T-101"],
        )
        stale = monitor.check_stale_sensors(BASE + timedelta(seconds=10))
        self.assertEqual(
            [(a.sensor_id, a.status) for a in stale], [("P-101", AlertStatus.RAISED)]
        )
        monitor.process(pressure(4, 11), now=BASE + timedelta(seconds=11))
        clear = monitor.check_stale_sensors(BASE + timedelta(seconds=11))
        self.assertEqual(
            [(a.sensor_id, a.status) for a in clear], [("P-101", AlertStatus.CLEARED)]
        )

    def test_bad_pressure_or_changed_measurement_does_not_replace_history(self):
        monitor = SensorMonitor(CONFIG)
        first = monitor.process(pressure(3), now=BASE)
        assert isinstance(first, SensorReading), first
        invalid = [
            (pressure(float("inf"), 1), "pressure"),
            (pressure(float("nan"), 1), "pressure"),
            (pressure(4, 1) | {"temperature_c": 600}, "measurement field"),
            (
                {
                    "sensor_id": "P-101",
                    "timestamp": (BASE + timedelta(seconds=1)).isoformat(),
                    "temperature_c": 600,
                },
                "measurement type",
            ),
        ]
        for raw, reason in invalid:
            with self.subTest(reason=reason, raw=raw):
                result = monitor.process(raw, now=BASE + timedelta(seconds=1))
                assert isinstance(result, QuarantineRecord), result
                self.assertIn(reason, result.reason)
                self.assertIs(monitor.state_for("P-101").last_accepted_reading, first)
        self.assertEqual(
            [
                a.sensor_id
                for a in monitor.check_stale_sensors(BASE + timedelta(seconds=6))
            ],
            ["P-101"],
        )
