"""Coordinator: wires validation, per-sensor state, detection, the alert FSM,
and persistence together. SensorMonitor.process() is the single place that
catches validation errors (bubbled up from parsing) and turns it into a
QuarantineRecord. No other try/except exists in the codebase.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import (
    Alert,
    AlertStatus,
    AlertType,
    InvalidReadingError,
    MonitoringConfig,
    SensorReading,
    SensorState,
    Severity,
    _iso,
)
from .parsing import QuarantineRecord, parse_reading
from .rules import (
    DetectionRule,
    FlatlineRule,
    HighTemperatureRule,
    OutOfNormalBandRule,
    RateOfRiseRule,
)

_ALARM_TYPES = {
    AlertType.HIGH_TEMPERATURE,
    AlertType.RATE_OF_RISE,
    AlertType.STALE_DATA,
    AlertType.FLATLINE,
}

_RULE_VIOLATED = {
    AlertType.HIGH_TEMPERATURE: "high_temperature_alarm_celsius",
    AlertType.RATE_OF_RISE: "max_rise_degrees_celsius_per_minute",
    AlertType.OUT_OF_BAND: "normal_range_celsius",
    AlertType.STALE_DATA: "stale_data_threshold_seconds",
    AlertType.FLATLINE: "flatline_window_seconds",
}

# Single source of truth for per-reading conditions; STALE_DATA is excluded
# because it is time-driven (check_stale_sensors), not reading-driven.
_READING_CONDITIONS = (
    AlertType.HIGH_TEMPERATURE,
    AlertType.RATE_OF_RISE,
    AlertType.OUT_OF_BAND,
    AlertType.FLATLINE,
)


def default_rules() -> list[DetectionRule]:
    return [
        HighTemperatureRule(),
        RateOfRiseRule(),
        OutOfNormalBandRule(),
        FlatlineRule(),
    ]


class AlertStore:
    """Keeps alerts in memory and, if a path is given, appends each as one JSONL
    line. Append-only: a recorded alert is a fact, never rewritten."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.alerts: list[Alert] = []
        self._path = Path(path) if path else None

    def record(self, alert: Alert) -> None:
        if self._path:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(alert.to_dict()) + "\n")
        self.alerts.append(alert)


class SensorMonitor:
    def __init__(
        self,
        config: MonitoringConfig,
        rules: list[DetectionRule] | None = None,
        store: AlertStore | None = None,
    ) -> None:
        self._config = config
        self._rules = rules if rules is not None else default_rules()
        self._store = store if store is not None else AlertStore()
        self._states: dict[str, SensorState] = {}

    @property
    def store(self) -> AlertStore:
        return self._store

    def state_for(self, sensor_id: str) -> SensorState:
        return self._states[sensor_id]

    def process(self, raw: Any, *, now: datetime | None = None) -> SensorReading | QuarantineRecord:
        try:
            reading = parse_reading(raw, self._config, now=now)
        except (InvalidReadingError, ValueError, OverflowError) as error:
            reason = str(error)
            if not isinstance(error, InvalidReadingError):
                reason = f"unparseable timestamp or unrepresentable numeric value: {error}"
            return QuarantineRecord(raw=raw, reason=reason)

        state = self._states.setdefault(
            reading.sensor_id, SensorState(sensor_id=reading.sensor_id)
        )

        rejection = self._reject_by_history(raw, reading, state)
        if rejection is not None:
            return rejection

        self._run_rules(reading, state)
        self._update_state(reading, state)
        return reading

    def check_stale_sensors(self, now: datetime) -> list[Alert]:
        emitted: list[Alert] = []
        for state in self._states.values():
            if state.last_valid_timestamp is None:
                continue
            idle_seconds = (now - state.last_valid_timestamp).total_seconds()
            is_stale = idle_seconds > self._config.stale_data_threshold_seconds
            alert = self._transition(
                state=state,
                condition=AlertType.STALE_DATA,
                active=is_stale,
                event_timestamp=now,
                observed_value=idle_seconds,
                threshold=self._config.stale_data_threshold_seconds,
                unit="seconds",
                message=(
                    f"No valid reading from {state.sensor_id} for "
                    f"{idle_seconds:.1f}s (limit {self._config.stale_data_threshold_seconds}s)."
                ),
            )
            if alert is not None:
                emitted.append(alert)
        return emitted

    def _reject_by_history(
        self, raw: dict, reading: SensorReading, state: SensorState
    ) -> QuarantineRecord | None:
        previous = state.last_accepted_reading
        if previous is None:
            return None
        if reading.measurement_type is not previous.measurement_type:
            return QuarantineRecord(raw=dict(raw), reason="sensor measurement type cannot change")
        if reading.timestamp == previous.timestamp:
            return QuarantineRecord(raw=dict(raw), reason="duplicate timestamp")
        if reading.timestamp < previous.timestamp:
            return QuarantineRecord(
                raw=dict(raw), reason="out-of-order (older than last accepted)"
            )
        return None

    def _run_rules(self, reading: SensorReading, state: SensorState) -> None:
        fired = {rule.evaluate(reading, state, self._config) for rule in self._rules}
        fired.discard(None)
        for condition in _READING_CONDITIONS:
            self._transition(
                state=state,
                condition=condition,
                active=condition in fired,
                event_timestamp=reading.timestamp,
                observed_value=(
                    (reading.timestamp - state.flatline_since).total_seconds()
                    if condition is AlertType.FLATLINE and state.flatline_since is not None
                    else reading.value
                ),
                threshold=self._threshold_for(condition, reading),
                unit="seconds" if condition is AlertType.FLATLINE else reading.unit,
                message=self._message_for(condition, reading, state),
                previous=state.last_accepted_reading,
            )

    def _transition(
        self,
        *,
        state: SensorState,
        condition: AlertType,
        active: bool,
        event_timestamp: datetime,
        observed_value: float,
        threshold: float,
        unit: str,
        message: str,
        previous: SensorReading | None = None,
    ) -> Alert | None:
        already_active = condition in state.active_conditions
        if active == already_active:
            return None

        if active:
            status = AlertStatus.RAISED
            severity = Severity.ALARM if condition in _ALARM_TYPES else Severity.WARNING
        else:
            status = AlertStatus.CLEARED
            severity = Severity.INFO
            message = f"{condition.value} cleared on {state.sensor_id}."

        rate = elapsed_minutes = previous_value = previous_timestamp = None
        if condition is AlertType.RATE_OF_RISE and previous is not None:
            elapsed_minutes = (event_timestamp - previous.timestamp).total_seconds() / 60.0
            previous_value = previous.value
            previous_timestamp = previous.timestamp
            if elapsed_minutes > 0:
                rate = (observed_value - previous.value) / elapsed_minutes

        alert = Alert(
            event_timestamp=event_timestamp,
            sensor_id=state.sensor_id,
            alert_type=condition,
            severity=severity,
            status=status,
            rule_violated=_RULE_VIOLATED[condition],
            observed_value=observed_value,
            threshold=threshold,
            unit=unit,
            message=message,
            previous_value=previous_value,
            previous_timestamp=previous_timestamp,
            elapsed_minutes=elapsed_minutes,
            observed_rise_degrees_celsius_per_minute=rate,
        )
        self._store.record(alert)
        # A failed write must leave the transition available for retry.
        if active:
            state.active_conditions.add(condition)
        else:
            state.active_conditions.discard(condition)
        return alert

    def _update_state(self, reading: SensorReading, state: SensorState) -> None:
        # Flatline run starts at the first of a repeated value; a stale gap restarts it.
        previous = state.last_accepted_reading
        gap = previous is not None and (
            reading.timestamp - previous.timestamp
        ).total_seconds() > self._config.stale_data_threshold_seconds
        if gap or state.flatline_value != reading.value or state.flatline_since is None:
            state.flatline_value = reading.value
            state.flatline_since = reading.timestamp
        state.last_accepted_reading = reading
        state.last_valid_timestamp = reading.timestamp

    def _threshold_for(self, condition: AlertType, reading: SensorReading) -> float:
        return {
            AlertType.HIGH_TEMPERATURE: self._config.high_temperature_alarm_celsius,
            AlertType.RATE_OF_RISE: self._config.max_rise_degrees_celsius_per_minute,
            AlertType.OUT_OF_BAND: self._config.normal_range_celsius[
                0 if reading.value < self._config.normal_range_celsius[0] else 1
            ],
            AlertType.FLATLINE: self._config.flatline_window_seconds,
        }[condition]

    def _message_for(
        self, condition: AlertType, reading: SensorReading, state: SensorState
    ) -> str:
        sensor = reading.sensor_id
        if condition is AlertType.HIGH_TEMPERATURE:
            return (
                f"{sensor} at {reading.value} {reading.unit}, "
                f"alarm at {self._config.high_temperature_alarm_celsius}."
            )
        if condition is AlertType.OUT_OF_BAND:
            low, high = self._config.normal_range_celsius
            return f"{sensor} at {reading.value} {reading.unit}, normal {low}-{high}."
        if condition is AlertType.RATE_OF_RISE:
            previous = state.last_accepted_reading
            if previous is None:
                return f"{sensor} rate of rise exceeded."
            minutes = (reading.timestamp - previous.timestamp).total_seconds() / 60.0
            rate = (reading.value - previous.value) / minutes if minutes else 0.0
            return (
                f"{sensor} rose {rate:.1f} {reading.unit}/min "
                f"(limit {self._config.max_rise_degrees_celsius_per_minute}); "
                f"{previous.value}->{reading.value} over {minutes:.3f} min."
            )
        if condition is AlertType.FLATLINE:
            return f"{sensor} value {reading.value} {reading.unit} unchanged (stuck sensor)."
        raise ValueError(f"no message defined for condition {condition}")


def format_for_operator(alert: Alert) -> str:
    when = _iso(alert.event_timestamp)
    return (
        f"[{when}] {alert.severity.value.upper():7} {alert.sensor_id:6} "
        f"{alert.alert_type.value:16} {alert.message} -> {alert.status.value.upper()}"
    )
