"""Detection rules: one algorithm per abnormal condition.

Reading-driven rules implement the DetectionRule protocol and return an
AlertType when their condition holds, else None. The monitor coordinates them
and builds the Alert; rules stay pure and independently testable. Stale-data is
deliberately NOT a rule here: it is time-driven, not reading-driven, so it lives
as a monitor method rather than being forced into this per-reading signature.
"""

from __future__ import annotations

from typing import Protocol

from .models import (
    AlertType,
    MeasurementType,
    MonitoringConfig,
    SensorReading,
    SensorState,
)


class DetectionRule(Protocol):
    def evaluate(
        self,
        reading: SensorReading,
        state: SensorState,
        config: MonitoringConfig,
    ) -> AlertType | None: ...


class HighTemperatureRule:
    def evaluate(
        self, reading: SensorReading, state: SensorState, config: MonitoringConfig
    ) -> AlertType | None:
        if reading.measurement_type is not MeasurementType.TEMPERATURE:
            return None
        if reading.value > config.high_temperature_alarm_celsius:
            return AlertType.HIGH_TEMPERATURE
        return None


class OutOfNormalBandRule:
    def evaluate(
        self, reading: SensorReading, state: SensorState, config: MonitoringConfig
    ) -> AlertType | None:
        if reading.measurement_type is not MeasurementType.TEMPERATURE:
            return None
        low, high = config.normal_range_celsius
        # Above the alarm threshold this is a HIGH_TEMPERATURE alarm, not a
        # band warning; stay silent so the two conditions do not double-fire.
        if reading.value > config.high_temperature_alarm_celsius:
            return None
        if reading.value < low or reading.value > high:
            return AlertType.OUT_OF_BAND
        return None


class RateOfRiseRule:
    def evaluate(
        self, reading: SensorReading, state: SensorState, config: MonitoringConfig
    ) -> AlertType | None:
        if reading.measurement_type is not MeasurementType.TEMPERATURE:
            return None
        previous = state.last_accepted_reading
        if previous is None:
            return None

        elapsed_minutes = (
            reading.timestamp - previous.timestamp
        ).total_seconds() / 60.0
        # Non-positive elapsed cannot occur for accepted readings (duplicates
        # and out-of-order are filtered upstream), but guard anyway: a bad clock
        # must never cause a divide-by-zero or a nonsense rate.
        if elapsed_minutes <= 0:
            return None

        rate = (reading.value - previous.value) / elapsed_minutes
        if rate > config.max_rise_degrees_celsius_per_minute:
            return AlertType.RATE_OF_RISE
        return None


class FlatlineRule:
    def evaluate(
        self, reading: SensorReading, state: SensorState, config: MonitoringConfig
    ) -> AlertType | None:
        if state.flatline_since is None or state.flatline_value != reading.value:
            return None
        previous = state.last_accepted_reading
        if previous is not None and (
            reading.timestamp - previous.timestamp
        ).total_seconds() > config.stale_data_threshold_seconds:
            return None
        held_seconds = (reading.timestamp - state.flatline_since).total_seconds()
        if held_seconds >= config.flatline_window_seconds:
            return AlertType.FLATLINE
        return None
