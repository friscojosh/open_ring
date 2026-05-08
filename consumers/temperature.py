"""Skin / ring temperature summary statistics.

Inputs:
    - 0x46 TempEvent — `temp1_c`..`temp7_c` (channels 1-3 are skin, 4-7 are
      typically firmware-disabled in current Gen 4 → all -327.68 sentinels)
    - 0x69 TempPeriod — periodic temperature snapshot
    - 0x75 SleepTempEvent — N×u16 skin temps at 30s intervals during sleep

Outputs:
    - per-channel temperature distribution (min/mean/max for ch 1-7)
    - rolling daily skin-temp average (first 3 channels combined)
    - sleep-window temperature distribution
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from . import event_time


# Sentinel value for disabled / saturated temperature channels (signed int16
# / 100 = -327.68 °C). Excluded from aggregations.
_SENTINEL = -327.68


def _stat(vals: list[float]) -> dict[str, float]:
    if not vals:
        return {}
    return {
        "n": len(vals),
        "min": round(min(vals), 2),
        "max": round(max(vals), 2),
        "mean": round(sum(vals) / len(vals), 2),
    }


def compute(records: Iterable) -> dict[str, Any]:
    per_channel: dict[int, list[float]] = {i: [] for i in range(1, 8)}
    sleep_temps: list[float] = []
    daily_buckets: dict[int, list[float]] = defaultdict(list)
    temp_period_count = 0

    for r in records:
        if r.type == "API_TEMP_EVENT":
            d = r.data
            et = event_time(r)
            for ch in range(1, 8):
                v = d.get(f"temp{ch}_c")
                if v is None or abs(v - _SENTINEL) < 0.01:
                    continue
                per_channel[ch].append(v)
                if ch <= 3 and et:
                    # Bucket by ring-emit day so catchup-uploaded readings
                    # land on the day they were actually recorded, not the
                    # day we received them.
                    day = et // (24 * 3_600_000)
                    daily_buckets[day].append(v)
        elif r.type == "API_TEMP_PERIOD":
            temp_period_count += 1
        elif r.type == "API_SLEEP_TEMP_EVENT":
            sleep_temps.extend(r.data.get("temps_c") or [])

    # All channels have the same N (one event per record); use channel 1.
    n_temp_events = len(per_channel[1])
    out: dict[str, Any] = {
        "temp_event_count": n_temp_events,
        "temp_period_count": temp_period_count,
        "per_channel_c": {f"ch{ch}": _stat(per_channel[ch]) for ch in range(1, 8)
                         if per_channel[ch]},
    }

    if sleep_temps:
        out["sleep_skin_temp_c"] = _stat(sleep_temps)

    if daily_buckets:
        out["daily_skin_temp_c_mean"] = {
            f"day_{day}": round(sum(v) / len(v), 2)
            for day, v in sorted(daily_buckets.items())
        }

    return out
