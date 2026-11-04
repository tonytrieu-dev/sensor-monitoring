# Sensor Monitoring and Operator Decision Support

A small Python component that checks timestamped temperature readings, flags
concerning conditions, and saves alerts for an operator to review. It does not
control equipment.

## Run the demo

Requires **Python 3.10+**. Uses only the standard library, so there is nothing
to install.

```bash
python3 run_demo.py
```

The demo processes a fixed list of sample readings from two sensors. It includes
a duplicate, a temperature outside the normal range, a high-temperature spike,
a nonnumeric value, an older reading, and a return to normal. It then checks
whether either sensor has stopped reporting. No sensor hardware is needed.

The output includes:

- Rejected readings with a reason for each rejection.
- Alerts marked `RAISED` when a condition starts and `CLEARED` when it ends.
  A continuing condition does not produce an alert on every reading.
- `alerts.jsonl`, with one JSON alert per line. Each demo run appends its alerts
  to this file and preserves earlier records.

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
| Temperature unchanged for the configured duration | Flatline ALARM; a possible stuck sensor |
| Malformed or implausible reading | Rejected with a reason |

## Files

```text
sensor_monitor/
  models.py       readings, sensor state, alerts, and configuration
  parsing.py      input validation and timestamp handling
  rules.py        condition detection
  monitor.py      processing, alert transitions, and storage
tests/
  test_monitor.py       reading validation, sensor history, and alerts
  test_persistence.py   stored records and retry after a failed write
  test_config.py        invalid configuration
run_demo.py       sample readings and console output
design.md         assumptions, detection rules, storage, and safety considerations
```

See [design.md](design.md) for the decisions behind the implementation and its
limits.
