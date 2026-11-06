"""Process sample readings, print alerts, and append them to alerts.jsonl next to this file."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sensor_monitor.models import MonitoringConfig
from sensor_monitor.monitor import AlertStore, SensorMonitor, format_for_operator
from sensor_monitor.parsing import QuarantineRecord

CONFIG = {
    "normal_range_c": [550.0, 650.0],
    "high_temperature_alarm_c": 675.0,
    "max_rate_of_rise_c_per_min": 10.0,
    "stale_data_threshold_s": 5,
}

BASE = datetime(2026, 8, 20, 14, 5, 0, tzinfo=timezone.utc)


def _at(offset_seconds: float) -> str:
    return (BASE + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


def build_stream() -> list[dict]:
    return [
        {"timestamp": _at(0), "sensor_id": "T-101", "temperature_c": 600.0},
        {"timestamp": _at(0), "sensor_id": "T-101", "temperature_c": 600.0},   # duplicate
        {"timestamp": _at(1), "sensor_id": "T-101", "temperature_c": 660.0},   # out of band
        {"timestamp": _at(2), "sensor_id": "T-101", "temperature_c": 700.0},   # spike
        {"timestamp": _at(3), "sensor_id": "T-101", "temperature_c": "n/a"},   # malformed
        {"timestamp": _at(1), "sensor_id": "T-101", "temperature_c": 500.0},   # out of order
        {"timestamp": _at(4), "sensor_id": "T-101", "temperature_c": 600.0},   # recovers
        {"timestamp": _at(0), "sensor_id": "P-204", "pressure_bar": 3.0},      # pressure sensor
    ]


def main() -> None:
    config = MonitoringConfig.from_raw(CONFIG)
    output_path = Path(__file__).with_name("alerts.jsonl")
    monitor = SensorMonitor(config, store=AlertStore(output_path))

    print("=== Ingesting readings ===")
    for raw in build_stream():
        outcome = monitor.process(raw)
        if isinstance(outcome, QuarantineRecord):
            print(f"  QUARANTINED {raw.get('sensor_id')} @ {raw.get('timestamp')}: {outcome.reason}")

    # P-204 reported only at t=0; by t=10 it is stale.
    monitor.check_stale_sensors(BASE + timedelta(seconds=10))

    print("\n=== Operator console ===")
    for alert in monitor.store.alerts:
        print(format_for_operator(alert))

    print(f"\n{len(monitor.store.alerts)} alert(s) written to {output_path.name}")


if __name__ == "__main__":
    main()
