"""Validation at the trust boundary: raw dict -> SensorReading or raise."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any

from .models import (
    InvalidReadingError,
    MeasurementType,
    MonitoringConfig,
    SensorReading,
)

_MEASUREMENT_KEYS: dict[str, tuple[MeasurementType, str]] = {
    "temperature_c": (MeasurementType.TEMPERATURE, "celsius"),
    "pressure_bar": (MeasurementType.PRESSURE, "bar"),
}


@dataclass(frozen=True)
class QuarantineRecord:
    raw: Any
    reason: str


def parse_reading(
    raw: Any, config: MonitoringConfig, *, now: datetime | None = None
) -> SensorReading:
    if not isinstance(raw, dict):
        raise InvalidReadingError("reading is not an object")

    sensor_id = raw.get("sensor_id")
    if not isinstance(sensor_id, str) or not sensor_id:
        raise InvalidReadingError("missing or empty sensor_id")

    measurement_type, unit, value = _extract_measurement(raw)
    timestamp = _parse_timestamp(raw.get("timestamp"), config, now)
    _check_physical_bound(value, measurement_type, config)

    return SensorReading(
        timestamp=timestamp,
        sensor_id=sensor_id,
        measurement_type=measurement_type,
        value=value,
        unit=unit,
    )


def _extract_measurement(raw: dict[str, Any]) -> tuple[MeasurementType, str, float]:
    present = [key for key in _MEASUREMENT_KEYS if key in raw]
    if not present:
        raise InvalidReadingError("no recognized measurement field")

    if len(present) != 1:
        raise InvalidReadingError("reading must contain exactly one measurement field")

    key = present[0]
    measurement_type, unit = _MEASUREMENT_KEYS[key]
    return measurement_type, unit, _coerce_value(raw[key], key)


def _coerce_value(candidate: Any, key: str) -> float:
    # bool is an int subclass; reject it so True does not silently become 1.0.
    if isinstance(candidate, bool):
        raise InvalidReadingError(f"boolean value for {key}: {candidate!r}")
    if not isinstance(candidate, (int, float)):
        raise InvalidReadingError(f"nonnumeric value for {key}: {candidate!r}")
    return float(candidate)


def _parse_timestamp(
    candidate: Any, config: MonitoringConfig, now: datetime | None
) -> datetime:
    if not isinstance(candidate, str):
        raise InvalidReadingError(f"missing or nonstring timestamp: {candidate!r}")

    parsed = _to_utc(_parse_iso(candidate), candidate, config)
    _reject_if_future(parsed, candidate, config, now)
    return parsed


def _parse_iso(candidate: str) -> datetime:
    # Python 3.10 requires normalization of the wire format's trailing Z.
    return datetime.fromisoformat(candidate.removesuffix("Z") + ("+00:00" if candidate.endswith("Z") else ""))


def _to_utc(parsed: datetime, candidate: str, config: MonitoringConfig) -> datetime:
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)
    if config.assume_utc_if_naive:
        return parsed.replace(tzinfo=timezone.utc)
    raise InvalidReadingError(f"naive timestamp (no timezone): {candidate!r}")


def _reject_if_future(
    parsed: datetime, candidate: str, config: MonitoringConfig, now: datetime | None
) -> None:
    ahead = (parsed - (now if now is not None else datetime.now(timezone.utc))).total_seconds()
    if ahead > config.future_tolerance_seconds:
        raise InvalidReadingError(f"future-dated timestamp: {candidate!r}")


def _check_physical_bound(
    value: float, measurement_type: MeasurementType, config: MonitoringConfig
) -> None:
    if measurement_type is MeasurementType.PRESSURE:
        if not isfinite(value) or value < 0:
            raise InvalidReadingError("absolute pressure must be finite and nonnegative (bar)")
        return
    low, high = config.physical_range_celsius
    if not low <= value <= high:
        raise InvalidReadingError(f"value {value} outside physical range [{low}, {high}]")


def _self_check() -> None:
    config = MonitoringConfig.from_raw(
        {
            "normal_range_c": [550.0, 650.0],
            "high_temperature_alarm_c": 675.0,
            "max_rate_of_rise_c_per_min": 10.0,
            "stale_data_threshold_s": 5,
        }
    )

    good = parse_reading(
        {"timestamp": "2026-08-20T14:05:00Z", "sensor_id": "T-101", "temperature_c": 612.4},
        config,
    )
    assert good.value == 612.4 and good.unit == "celsius"
    assert good.timestamp.tzinfo is not None

    rejected = {
        "nonnumeric": {"timestamp": "2026-08-20T14:05:00Z", "sensor_id": "T-101", "temperature_c": "hot"},
        "missing measurement": {"timestamp": "2026-08-20T14:05:00Z", "sensor_id": "T-101"},
        "naive timestamp": {"timestamp": "2026-08-20T14:05:00", "sensor_id": "T-101", "temperature_c": 600.0},
        "unparseable timestamp": {"timestamp": "not-a-time", "sensor_id": "T-101", "temperature_c": 600.0},
        "non-physical": {"timestamp": "2026-08-20T14:05:00Z", "sensor_id": "T-101", "temperature_c": -500.0},
        "boolean value": {"timestamp": "2026-08-20T14:05:00Z", "sensor_id": "T-101", "temperature_c": True},
    }
    from .monitor import SensorMonitor
    from .parsing import QuarantineRecord as CanonicalQuarantineRecord

    for label, raw in rejected.items():
        result = SensorMonitor(config).process(raw)
        assert isinstance(result, CanonicalQuarantineRecord), label

    print("parsing.py self-check passed")


if __name__ == "__main__":
    _self_check()
