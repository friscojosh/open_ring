"""Sleep-related summary statistics.

Inputs:
    - 0x6a SleepPeriodInfo2 — `average_hr`, `breath`, `breath_v`,
      `motion_count`, `sleep_state` (0/1/2), `cv`, `mzci`, `dzci` per record
    - 0x76 BedtimePeriod — start/end markers of sleep windows
    - 0x75 SleepTempEvent — N×u16 skin temperatures at 30s intervals

Outputs:
    - sleep state distribution (state 0/1/2 fraction)
    - mean HR / breath rate / motion during sleep records
    - skin temperature stats during sleep
    - tracked-sleep-time estimate (assumes 5-min period per record — verify
      empirically before relying on absolute hours)

**Note:** sleep state is tracked through `0x6a SleepPeriodInfo2` records
emitted by the ring, NOT through the BLE-link `StateChange` enum. The
StateChange enum tracks sensor / wear state (in-finger, charging, etc.) and
its `IN_REST` values cover sedentary periods more broadly than just sleep.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

from . import event_time


# A new sleep "session" starts when emission time gaps by > 10 min between
# consecutive SleepPeriodInfo records. Using event_time (`t_event_ms`) so
# this measures actual physiological gaps on the ring, not BLE-arrival
# bursting from history-fetch.
_SESSION_GAP_MS = 10 * 60 * 1000


def compute(records: Iterable) -> dict[str, Any]:
    spi_states: Counter[int] = Counter()
    spi_hrs: list[float] = []
    spi_breaths: list[float] = []
    spi_motion_counts: list[int] = []
    spi_ts: list[int] = []
    sleep_temps: list[float] = []
    bedtime_period_count = 0

    for r in records:
        if r.type == "API_SLEEP_PERIOD_INFO_2":
            d = r.data
            spi_states[d["sleep_state"]] += 1
            spi_hrs.append(d["average_hr"])
            spi_breaths.append(d["breath"])
            spi_motion_counts.append(d["motion_count"])
            et = event_time(r)
            if et:
                spi_ts.append(et)
        elif r.type == "API_SLEEP_TEMP_EVENT":
            sleep_temps.extend(r.data.get("temps_c") or [])
        elif r.type == "API_BEDTIME_PERIOD":
            bedtime_period_count += 1

    # Cluster sleep records into arrival-bursts: each burst likely corresponds
    # to one BLE sync that uploaded one analysis-window's worth of records.
    spi_ts.sort()
    sleep_sessions = 1 if spi_ts else 0
    for i in range(len(spi_ts) - 1):
        if spi_ts[i + 1] - spi_ts[i] > _SESSION_GAP_MS:
            sleep_sessions += 1

    out: dict[str, Any] = {
        "n_sleep_period_records": sum(spi_states.values()),
        "bedtime_period_records": bedtime_period_count,
        "n_sleep_sessions": sleep_sessions,
        "_note": "n_sleep_sessions counts gaps of >10 min in ring-emit time "
                 "(t_event_ms) between consecutive SleepPeriodInfo records. "
                 "With proper time-anchoring this should approximate distinct "
                 "physical sleep periods, but the ring's own sleep-stage "
                 "detection (DbDailySleep via oura_ring.realm_dump) is still "
                 "authoritative for absolute sleep duration.",
    }

    if spi_states:
        total = sum(spi_states.values())
        out["sleep_state_distribution"] = {
            f"state_{k}": {"count": v, "fraction": round(v / total, 3)}
            for k, v in sorted(spi_states.items())
        }

    if spi_hrs:
        out["sleep_hr_bpm"] = {
            "min": round(min(spi_hrs), 1),
            "max": round(max(spi_hrs), 1),
            "mean": round(sum(spi_hrs) / len(spi_hrs), 1),
        }
    if spi_breaths:
        out["sleep_breath_per_min"] = {
            "min": round(min(spi_breaths), 2),
            "max": round(max(spi_breaths), 2),
            "mean": round(sum(spi_breaths) / len(spi_breaths), 2),
        }
    if spi_motion_counts:
        out["sleep_motion_count"] = {
            "min": min(spi_motion_counts),
            "max": max(spi_motion_counts),
            "mean": round(sum(spi_motion_counts) / len(spi_motion_counts), 1),
            "total_motion_events": sum(spi_motion_counts),
        }

    if sleep_temps:
        out["sleep_skin_temp_c"] = {
            "n_samples": len(sleep_temps),
            "min": round(min(sleep_temps), 2),
            "max": round(max(sleep_temps), 2),
            "mean": round(sum(sleep_temps) / len(sleep_temps), 2),
        }

    return out
