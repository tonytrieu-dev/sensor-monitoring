"""Regression tests for persistence failures and configuration safety."""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sensor_monitor.models import AlertStatus, AlertType, MonitoringConfig
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




class ConfigurationValidation(unittest.TestCase):
    def test_nonfinite_limits_rejected_in_both_construction_paths(self):
        fields = {
            "high_temperature_alarm_c": "high_temperature_alarm_celsius",
            "max_rate_of_rise_c_per_min": "max_rise_degrees_celsius_per_minute",
            "stale_data_threshold_s": "stale_data_threshold_seconds",
            "flatline_window_s": "flatline_window_seconds",
            "future_tolerance_s": "future_tolerance_seconds",
        }
        for wire, internal in fields.items():
            for value in [float("nan"), float("inf"), -float("inf")]:
                with self.subTest(field=wire, value=value):
                    with self.assertRaises(ValueError):
                        MonitoringConfig.from_raw(RAW_CONFIG | {wire: value})
                    with self.assertRaises(ValueError):
                        replace(CONFIG, **{internal: value})

    def test_invalid_ranges_and_durations_rejected(self):
        invalid = [
            {"normal_range_c": [650, 550]},
            {"normal_range_c": [550, float("nan")]},
            {"physical_range_c": [2000, -50]},
            {"physical_range_c": [float("inf"), 2000]},
            {"max_rate_of_rise_c_per_min": -1},
            {"stale_data_threshold_s": -1},
            {"stale_data_threshold_s": 0},
            {"flatline_window_s": -1},
            {"flatline_window_s": 0},
            {"future_tolerance_s": -1},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                MonitoringConfig.from_raw(RAW_CONFIG | changes)

    def test_zero_rise_and_future_tolerance_are_valid(self):
        config = MonitoringConfig.from_raw(RAW_CONFIG | {
            "max_rate_of_rise_c_per_min": 0, "future_tolerance_s": 0,
        })
        self.assertEqual(config.max_rise_degrees_celsius_per_minute, 0)
        self.assertEqual(config.future_tolerance_seconds, 0)
