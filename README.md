# Sensor Monitoring & Operator Decision Support

A small component that ingests timestamped sensor readings, validates data
quality, detects abnormal conditions, and emits structured, auditable alerts
for an operator and for later inspection.

## Run it in 30 seconds

Requires **Python 3.10+** and **no dependencies** (standard library only, no
install, no virtualenv needed).

```bash
# See the whole thing work: feeds a realistic stream, prints the operator
# console, and writes alerts.jsonl.
python3 run_demo.py

# Run the test suite.
python3 -m unittest discover -s tests

# Each module also has its own runnable self-check:
python3 -m sensor_monitor.parsing
python3 -m sensor_monitor.rules
python3 -m sensor_monitor.monitor
python3 sensor_monitor/models.py
```

### What you'll see

`run_demo.py` pushes a stream containing a duplicate, an out-of-band reading, a
spike, a malformed reading, and an out-of-order reading through the monitor, then
checks for stale sensors. It prints:

- **Quarantine lines**, rejected readings with the reason why.
- **Operator console**, one line per alert, severity-prefixed, de-duplicated so
  a persistent condition is reported once (RAISED) and once when it resolves
  (CLEARED).
- **`alerts.jsonl`**, the same alerts as structured, append-only JSON Lines for
  later analysis (`jq`, pandas, etc.).

## Layout

```
sensor_monitor/
  models.py     data model: readings, state, alerts, config (plain data holders)
  parsing.py    validation at the trust boundary (raw dict -> SensorReading or reject)
  rules.py      detection rules (one algorithm per condition, independently testable)
  monitor.py    coordinator: state, detection, alert state machine, persistence
tests/
  test_monitor.py   cross-module + regression tests
run_demo.py     one-command demo
design.md       design note (boundaries, assumptions, handling, patterns, tests)
```

## What it detects

| Condition | Severity | Notes |
|---|---|---|
| Temperature above the alarm threshold | ALARM | |
| Rate of rise above the limit | ALARM | alert records the values and interval used |
| No valid reading within the stale threshold | ALARM | via an explicit `check_stale_sensors(now)` call |
| Reading outside the normal band (below alarm) | WARNING | pre-alarm; both directions |
| A value stuck unchanged (flatline / wedged sensor) | ALARM | distinct from stale: data arrives but is frozen |
| Malformed / implausible input | none | quarantined with a reason, never reaches detection |

See `design.md` for the assumptions, policies (duplicate / out-of-order / repeated
alerts), alert schema, and what would change for a production, safety-significant
deployment.
