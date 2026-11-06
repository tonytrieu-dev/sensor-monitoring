"""Domain model: readings, per-sensor state, alerts, and configuration.

Plain data holders only. Logic lives in parsing.py, rules.py, and monitor.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from math import isfinite
from typing import Any


class MeasurementType(str, Enum):
    TEMPERATURE = "temperature"
    PRESSURE = "pressure"


class AlertType(str, Enum):
    HIGH_TEMPERATURE = "high_temperature"
    RATE_OF_RISE = "rate_of_rise"
    STALE_DATA = "stale_data"
    OUT_OF_BAND = "out_of_band"
    FLATLINE = "flatline"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ALARM = "alarm"


class AlertStatus(str, Enum):
    RAISED = "raised"
    CLEARED = "cleared"


class InvalidReadingError(Exception):
    """Raised by parsing validators; caught once in SensorMonitor.process()."""


@dataclass(frozen=True)
class SensorReading:
    timestamp: datetime
    sensor_id: str
    measurement_type: MeasurementType
    value: float
    unit: str


@dataclass
class SensorState:
    sensor_id: str
    last_accepted_reading: SensorReading | None = None
    last_valid_timestamp: datetime | None = None
    flatline_value: float | None = None
    flatline_since: datetime | None = None
    # Membership in this set is the per-condition FSM: present == ACTIVE.
    active_conditions: set[AlertType] = field(default_factory=set)


@dataclass(frozen=True)
class Alert:
    event_timestamp: datetime
    sensor_id: str
    alert_type: AlertType
    severity: Severity
    status: AlertStatus
    rule_violated: str
    observed_value: float
    threshold: float
    unit: str
    message: str
    previous_value: float | None = None
    previous_timestamp: datetime | None = None
    elapsed_minutes: float | None = None
    observed_rise_degrees_celsius_per_minute: float | None = None

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "event_timestamp": _iso(self.event_timestamp),
            "sensor_id": self.sensor_id,
            "alert_type": self.alert_type.value,
            "severity": self.severity.value,
            "status": self.status.value,
            "rule_violated": self.rule_violated,
            "observed_value": self.observed_value,
            "threshold": self.threshold,
            "unit": self.unit,
            "message": self.message,
        }
        if self.previous_value is not None:
            record["previous_value"] = self.previous_value
        if self.previous_timestamp is not None:
            record["previous_timestamp"] = _iso(self.previous_timestamp)
        if self.elapsed_minutes is not None:
            record["elapsed_minutes"] = self.elapsed_minutes
        if self.observed_rise_degrees_celsius_per_minute is not None:
            record["observed_rise_degrees_celsius_per_minute"] = (
                self.observed_rise_degrees_celsius_per_minute
            )
        return record


@dataclass(frozen=True)
class MonitoringConfig:
    normal_range_celsius: tuple[float, float]
    high_temperature_alarm_celsius: float
    max_rise_degrees_celsius_per_minute: float
    stale_data_threshold_seconds: float
    flatline_window_seconds: float = 5.0
    physical_range_celsius: tuple[float, float] = (-50.0, 2000.0)
    future_tolerance_seconds: float = 2.0
    assume_utc_if_naive: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.assume_utc_if_naive, bool):
            raise ValueError("assume_utc_if_naive must be a boolean")
        for name in ("normal_range_celsius", "physical_range_celsius"):
            low, high = getattr(self, name)
            if isinstance(low, bool) or isinstance(high, bool) or not isfinite(low) or not isfinite(high) or low > high:
                raise ValueError(f"{name} must have finite bounds in ascending order")
        for name in (
            "high_temperature_alarm_celsius",
            "max_rise_degrees_celsius_per_minute",
            "stale_data_threshold_seconds",
            "flatline_window_seconds",
            "future_tolerance_seconds",
        ):
            if isinstance(getattr(self, name), bool) or not isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        for name in ("max_rise_degrees_celsius_per_minute", "future_tolerance_seconds"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        for name in ("stale_data_threshold_seconds", "flatline_window_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> MonitoringConfig:
        # Map the assignment's ambiguous `_c`/`_s` wire keys to clear names here.
        # Absent optional keys are omitted so the field defaults stay authoritative.
        normal_low, normal_high = raw["normal_range_c"]
        kwargs: dict[str, Any] = {
            "normal_range_celsius": (_config_number(normal_low), _config_number(normal_high)),
            "high_temperature_alarm_celsius": _config_number(raw["high_temperature_alarm_c"]),
            "max_rise_degrees_celsius_per_minute": _config_number(raw["max_rate_of_rise_c_per_min"]),
            "stale_data_threshold_seconds": _config_number(raw["stale_data_threshold_s"]),
            "flatline_window_seconds": _config_number(
                raw.get("flatline_window_s", raw["stale_data_threshold_s"])
            ),
        }
        if "physical_range_c" in raw:
            low, high = raw["physical_range_c"]
            kwargs["physical_range_celsius"] = (_config_number(low), _config_number(high))
        if "future_tolerance_s" in raw:
            kwargs["future_tolerance_seconds"] = _config_number(raw["future_tolerance_s"])
        if "assume_utc_if_naive" in raw:
            kwargs["assume_utc_if_naive"] = raw["assume_utc_if_naive"]
        return cls(**kwargs)


def _config_number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("configuration numbers cannot be boolean")
    return float(value)


def _iso(moment: datetime) -> str:
    # Present UTC as a trailing Z to match the assignment wire format;
    # datetime.isoformat() renders it as +00:00.
    return moment.isoformat().replace("+00:00", "Z")
