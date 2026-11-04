# Sensor Monitoring and Operator Decision Support

A small Python component that checks timestamped temperature and pressure
readings, flags concerning conditions, and saves alerts for an operator to review. It does not
control equipment.

## Run the demo

Requires **Python 3.10+**. Uses only the standard library, so there is nothing
to install.

```bash
python3 run_demo.py
```

The demo processes a fixed list of sample readings from a temperature sensor and a
pressure sensor. It includes
a duplicate, a temperature outside the normal range, a high-temperature spike,
a nonnumeric value, an older reading, and a return to normal. It then checks
whether either sensor has stopped reporting. No sensor hardware is needed.

The output includes:

- Rejected readings with a reason for each rejection.
- Alerts marked `RAISED` when a condition starts and `CLEARED` when it ends.
  A continuing condition does not produce an alert on every reading.
- `alerts.jsonl`, with one JSON alert per line. Each demo run appends its alerts
  to this file and preserves earlier records.

## Reading format

Each reading contains a timestamp, a sensor ID, and exactly one measurement:

```json
[
  {"timestamp": "2026-08-20T14:05:00Z", "sensor_id": "T-101", "temperature_c": 612.4},
  {"timestamp": "2026-08-20T14:05:00Z", "sensor_id": "P-204", "pressure_bar": 3.0}
]
```

`pressure_bar` means absolute pressure in bar; values must be finite and
nonnegative. Both types support validation, duplicate handling, stale alerts,
and flatline detection. Temperature limits apply only to temperature readings.
The assignment supplies no pressure alarm limits, so none are assumed.

## Run the checks

```bash
python3 -m unittest discover -s tests

# Each module also has a small runnable check.
python3 -m sensor_monitor.parsing
python3 -m sensor_monitor.rules
python3 -m sensor_monitor.monitor
python3 sensor_monitor/models.py
```

## What it detects

| Condition | Result |
|---|---|
| Temperature above the alarm threshold | ALARM |
| Temperature rising faster than the configured limit | ALARM, including the values and time interval used |
| No accepted reading within the allowed time | ALARM, checked by calling `check_stale_sensors(now)` |
| Temperature outside the normal range, at or below the alarm threshold | WARNING |
| Temperature or pressure unchanged for the configured duration | Flatline ALARM; a possible stuck sensor |
| Malformed or implausible reading | Rejected with a reason |

See [design.md](design.md) for the architecture, trade-offs, and safety
considerations.
