# `consumers` — summary statistics from `driver` Record streams

Pure-Python derivative-statistics package. **Strictly downstream of `driver`** —
this package never touches wire bytes; it only reads typed `Record` objects
produced by `driver.replay()` (offline) or `OuraRingClient.stream()` (live).

The split:

```
   driver        ←─────  decode bytes correctly (the driver)
       │
       ▼  Record stream
   consumers     ────  derive stats / aggregates / health metrics (consumer)
```

Keeping the decoder and the analytics in separate packages means the driver
can be vendored / re-used standalone, and `consumers` can grow new
modules without bloating the driver.

## Modules

| Module | Output | Function |
|---|---|---|
| `hr` | dict | HR distribution (median/p25/p75/p90/p99), resting HR, max HR, RMSSD per-record |
| `ble` | dict | Reconnect cadence (median/p90/max gap), per-type ingest rate |
| `battery` | dict | Voltage range, battery percentage range, drain rate (mV/h), charge cycles |
| `sleep` | dict | SleepPeriodInfo state distribution, sleep HR/breath/motion, skin temp during sleep |
| `coverage` | dict | Per-type record counts, counter-gap detection, decode-error counts |
| `activity` | dict | Motion event count, ACM magnitude distribution, orientation, active vs sedentary |
| `temperature` | dict | Per-channel range (ch1-7), daily skin-temp average, sleep-window temps |
| `plot` | matplotlib `Figure` (and PNG) | 5-panel HR / SpO2 / temp / motion / HRV+breath plot |

The seven `compute()` modules are pure, one-pass, JSON-serializable —
stdlib only. `plot` is the exception: it imports `matplotlib` lazily, so
the rest of the package stays light. Install matplotlib only if you
need the renderer (`pip install matplotlib`).

## Usage

```python
from consumers import hr, ble, battery, open_records

records = list(open_records("capture.log"))   # accepts .log OR .jsonl
print(hr.compute(records)["resting_hr_bpm"])           # → 68.09
print(ble.compute(records)["reconnect_count"])          # → 71
print(battery.compute(records)["mean_drain_mv_per_h"])  # → 11.62
```

```python
# Render a 5-panel biometric figure
from consumers import plot, open_records
plot.render(open_records("capture.log"), title="Tuesday session",
            out="tuesday.png")
```

CLIs for one-shot reports:

```sh
# JSON summary across all modules
python -m consumers.cli capture.log
python -m consumers.cli session.jsonl --module hr --json

# Plot to PNG
python -m consumers.plot capture.log
python -m consumers.plot session.jsonl --out /tmp/today.png
```

Both CLIs auto-detect input format by extension: `.log` is decoded via
`driver.replay` (btsnoop binary), `.jsonl` is read as already-decoded
driver output. The helpers `open_records(path)` and
`records_from_jsonl(path)` are exported for direct use.

## Time semantics — `event_time(r)` (v0.2.0)

The package exposes a single helper that every module uses to pick a record's
time axis:

```python
from consumers import event_time

def event_time(r) -> int | None:
    """Returns r.t_event_ms if available, else r.t."""
    return r.t_event_ms if r.t_event_ms is not None else r.t
```

`r.t` is the BLE-arrival timestamp (when the driver received the bytes).
`r.t_event_ms` is the ring-emit timestamp (when the ring actually generated
the event), interpolated by the driver's `RingTimeResolver` replica — see
`driver/PROTOCOL.md` §7. The two diverge by ≈200 ms for live records
and by hours / days for catchup-buffered records.

All real-record analytics (HR bursts, daily temperature, sleep-period
clustering, state-time accumulators, drain-rate regression) bin on
`event_time` so they reflect what happened on the **ring**, not the
arrival pattern of the BLE upload. Synthetic control-plane events
(`_HANDSHAKE_*`, `_TIME_SYNC_*`, `_DISCONNECT`, `_BATTERY`) keep `r.t`
because they ARE arrival-time events with no ring-side timestamp.

`event_time` falls back to `r.t` whenever the record predates a valid
time anchor (typically the first few records after a fresh connect, before
the first `0x42 API_TIME_SYNC_IND` lands), so no record is silently dropped.

### What changed in v0.2.0

| Module | Before (v0.1) | After (v0.2) |
|---|---|---|
| `hr` | Bursts grouped by **arrival** gaps; "history-fetch flattens timing" was a known limitation | Bursts grouped by **emission** gaps — physiologically correct; "HR at 3:15 PM" is now recoverable |
| `temperature` | `daily_skin_temp_c_mean` keyed by **receive** day | Keyed by **emission** day — multi-day catchups now produce one bucket per actual day |
| `sleep` | `n_sleep_arrival_sessions` (BLE upload bursts) | `n_sleep_sessions` (physio gaps in emission time) |
| `activity` | `state_time_h` measured arrival-pattern durations | Measures actual ring-state durations |
| `battery` | Drain regression skewed by upload bursting | Reflects real on-device drain |
| `ble` | Capture window = arrival span | Capture window = ring-emission span (typically much longer) |
| `coverage` | (unchanged — counter-based, no time axis) | (unchanged) |

Concrete shift on `samples/sunday_evening.log` (122,325 records, 69.8 h capture):

| Metric | v0.1 (r.t) | v0.2 (event_time) |
|---|---:|---:|
| `hr.n_bursts` | 12 | 8 |
| `hr.resting_hr_bpm` | 62.45 | 68.09 |
| `hr.max_hr_smoothed_bpm` | 94.32 | 92.45 |
| `temperature.daily_skin_temp_c_mean` keys | 1 | 4 |
| `battery.discharge_runs` | (often 1, distorted) | 4 (one per real day-cycle) |

## Validation across captures

RMSSD medians cluster in the 23-26 ms range across captures — the
expected adult resting-HRV range. **0 decode errors across all captures.**

Truth-table validation (in `tools/verify_claims.py § W`):

- `W.summary.{log}.rmssd_physiological` — RMSSD median ∈ [10..70] ms
- `W.summary.{log}.hr_median_physiological` — HR median ∈ [40..120] BPM
- `W.summary.{log}.zero_decode_errors` — 0 decoder errors per capture

## Caveats that survive v0.2.0

### Records before the first anchor have only arrival time

Until the first `API_TIME_SYNC_IND` (0x42) lands in a session, `t_event_ms`
is `None` and `event_time(r)` falls back to `r.t`. Typically this affects
the first ≈10–500 records of a fresh connect (handshake + capability
dance + first GetEvent burst). For long sessions this is negligible; for
short polls (e.g. Pattern C 5-write quick-refresh, see PROTOCOL.md §6.2)
much of the capture may lack anchors.

### Per-beat timestamps within a 0x60 record are still unresolved

`event_time(r)` gives one timestamp per record, but each `0x60
API_IBI_AND_AMPLITUDE_EVENT` carries ~12 contiguous beats. The native
ecore lib uses `nativeParseEvents(buf, ringTime, utcTime)` to produce
per-beat timestamps; we don't replicate this yet, so per-record RMSSD is
still the right HRV granularity (each record ≈ 10 s of contiguous
beats — meaningful within, not across).

### What's NOT here

By design, this package only computes what's directly aggregable from the
wire stream. It does NOT:

- Compute daily sleep / readiness / activity scores (that's `libappecore.so`)
- Estimate calories, MET minutes, or step counts (also `libappecore.so`)
- Run sleep-stage classification (REM / Deep / Light / Awake)
- Compute HRV baselines, body-temp deviation, or cardiovascular age

For those, read pre-computed values from the on-device DB — see
`driver.realm_dump` and `missing_statistics.md` for the full taxonomy.
