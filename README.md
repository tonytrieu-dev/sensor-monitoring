# Sensor Monitoring and Operator Decision Support

A small Python component that checks timestamped temperature and pressure
readings, flags concerning conditions, and saves alerts for an operator to
review. It does not control equipment.

## Run the demo

Requires **Python 3.10+**. Uses only the standard library, so there is nothing
to install.

```bash
python3 run_demo.py
```

The demo processes a fixed list of sample readings from a temperature sensor
and a pressure sensor. It includes a duplicate, a temperature outside the normal range, a high-temperature spike,
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
Pressure has no alarm thresholds by design; see [What it detects](#what-it-detects) for why.

## Run the checks

```bash
python3 -m unittest discover -s tests

# Each module also has a small runnable check.
python3 -m sensor_monitor.parsing
python3 -m sensor_monitor.rules
python3 -m sensor_monitor.monitor
python3 sensor_monitor/models.py
```

### Test coverage

`tests/test_monitor.py` runs shared behaviors for **both temperature and
pressure** using named subtests. `tests/test_pressure.py` checks pressure-specific
rules and invalid inputs. The suite has 16 test methods, with multiple input
cases inside the shared tests.

| Category | Pressure coverage |
|---|---|
| Happy path | Changing, valid readings produce no alerts and update sensor history. |
| Edge cases | Invalid timestamps, boolean/nonnumeric values, negative pressure, NaN/infinity, and ambiguous measurements are rejected. Sub-second timestamps are accepted. Stale timing is checked at the exact threshold. |
| Regressions | Duplicate, older, and timezone-equivalent readings preserve history. Celsius thresholds never apply to pressure, and a sensor cannot change measurement type. |
| Alert lifecycle | Stale and flatline conditions raise once, remain quiet, and clear on recovery. Missing readings restart the flatline window. |
| Integration | Real parsing, rules, and sensor state work together; temperature and pressure sensors remain independent. |

Temperature also has tests for its high-temperature and rate-of-rise rules.
`tests/test_persistence.py` uses real files to check stored rate evidence and
failed-write retry. The shared monitor test file also checks that reopening
storage preserves earlier records. `tests/test_config.py` rejects invalid settings.

## What it detects

| Condition | Applies to | Result |
|---|---|---|
| Temperature above the alarm threshold | Temperature | ALARM |
| Temperature rising faster than the configured limit | Temperature | ALARM, including the values and time interval used |
| No accepted reading within the allowed time | Temperature and pressure | ALARM, checked by calling `check_stale_sensors(now)` |
| Temperature outside the normal range, at or below the alarm threshold | Temperature | WARNING |
| Value unchanged for the configured duration | Temperature and pressure | Flatline ALARM; a possible stuck sensor |
| Malformed or implausible reading | Temperature and pressure | Rejected with a reason |

Temperature ships with alarm thresholds, a rate-of-rise limit, and a normal
band because those limits are known. Pressure deliberately has none: the alarm
and rate engine is type-agnostic and ready for pressure limits, but inventing
high-pressure or pressure-rise thresholds without real equipment data would risk
false alarms or masked faults in a safety context. Until those limits are
defined, pressure is covered by validation, stale, and flatline detection, and
adding them later is a small, localized change: a pressure limit in the config
plus a short rule alongside the temperature ones, with no change to the alert
lifecycle, storage, or coordinator.

See [design.md](design.md) for the architecture, trade-offs, and safety
considerations.
