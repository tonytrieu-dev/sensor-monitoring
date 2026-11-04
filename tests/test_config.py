"""Configuration mistakes must fail at startup, never disable alarms silently."""

import unittest

from sensor_monitor.models import MonitoringConfig

RAW_CONFIG = {
    "normal_range_c": [550.0, 650.0],
    "high_temperature_alarm_c": 675.0,
    "max_rate_of_rise_c_per_min": 10.0,
    "stale_data_threshold_s": 5,
}


class ConfigurationValidation(unittest.TestCase):
    def test_invalid_configuration_is_rejected(self):
        invalid = [
            {"high_temperature_alarm_c": float("nan")},
            {"stale_data_threshold_s": float("nan")},
            {"max_rate_of_rise_c_per_min": float("inf")},
            {"normal_range_c": [650, 550]},
            {"physical_range_c": [float("inf"), 2000]},
            {"max_rate_of_rise_c_per_min": -1},
            {"stale_data_threshold_s": 0},
            {"flatline_window_s": -1},
            {"future_tolerance_s": -1},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                MonitoringConfig.from_raw(RAW_CONFIG | changes)
