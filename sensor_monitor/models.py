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
        for name in ("normal_range_celsius", "physical_range_celsius"):
            low, high = getattr(self, name)
            if not isfinite(low) or not isfinite(high) or low > high:
                raise ValueError(f"{name} must have finite bounds in ascending order")
        for name in (
            "high_temperature_alarm_celsius",
            "max_rise_degrees_celsius_per_minute",
            "stale_data_threshold_seconds",
            "flatline_window_seconds",
            "future_tolerance_seconds",
        ):
            if not isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        for name in ("max_rise_degrees_celsius_per_minute", "future_tolerance_seconds"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        for name in ("stale_data_threshold_seconds", "flatline_window_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> MonitoringConfig:
        # Map the assignment's wire keys (the ambiguous `_c`/`_s` suffixes)
        # onto clear internal names so they die at this boundary.
        normal_low, normal_high = raw["normal_range_c"]
        physical_low, physical_high = raw.get("physical_range_c", (-50.0, 2000.0))
        return cls(
            normal_range_celsius=(float(normal_low), float(normal_high)),
            high_temperature_alarm_celsius=float(raw["high_temperature_alarm_c"]),
            max_rise_degrees_celsius_per_minute=float(raw["max_rate_of_rise_c_per_min"]),
            stale_data_threshold_seconds=float(raw["stale_data_threshold_s"]),
            flatline_window_seconds=float(
                raw.get("flatline_window_s", raw["stale_data_threshold_s"])
            ),
            physical_range_celsius=(float(physical_low), float(physical_high)),
            future_tolerance_seconds=float(raw.get("future_tolerance_s", 2.0)),
            assume_utc_if_naive=bool(raw.get("assume_utc_if_naive", False)),
        )


def _iso(moment: datetime) -> str:
    # Present UTC as a trailing Z to match the assignment wire format;
    # datetime.isoformat() renders it as +00:00.
    return moment.isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    import json
    from datetime import timezone

    now = datetime(2026, 8, 20, 14, 5, 3, tzinfo=timezone.utc)
    alert = Alert(
        event_timestamp=now,
        sensor_id="T-101",
        alert_type=AlertType.RATE_OF_RISE,
        severity=Severity.ALARM,
        status=AlertStatus.RAISED,
        rule_violated="max_rise_degrees_celsius_per_minute",
        observed_value=648.0,
        threshold=10.0,
        unit="celsius",
        message="Temperature on T-101 rose fast.",
        previous_value=612.4,
        previous_timestamp=datetime(2026, 8, 20, 14, 5, 0, tzinfo=timezone.utc),
        elapsed_minutes=0.05,
        observed_rise_degrees_celsius_per_minute=712.0,
    )

    line = json.dumps(alert.to_dict())
    restored = json.loads(line)
    assert restored["sensor_id"] == "T-101"
    assert restored["alert_type"] == "rate_of_rise"
    assert restored["event_timestamp"] == "2026-08-20T14:05:03Z"
    assert restored["previous_value"] == 612.4
    assert restored["observed_rise_degrees_celsius_per_minute"] == 712.0

    config = MonitoringConfig.from_raw(
        {
            "normal_range_c": [550.0, 650.0],
            "high_temperature_alarm_c": 675.0,
            "max_rate_of_rise_c_per_min": 10.0,
            "stale_data_threshold_s": 5,
        }
    )
    assert config.normal_range_celsius == (550.0, 650.0)
    assert config.flatline_window_seconds == 5.0

    print("models.py self-check passed:", line)
