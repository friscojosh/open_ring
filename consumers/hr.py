"""Heart-rate summary statistics from the IBI stream.

Inputs (from the driver):
    - 0x60 IBI_AND_AMPLITUDE_EVENT — `data.ibi_ms[]` per beat (ms)
    - 0x6e SPO2_IBI_AND_AMPLITUDE_EVENT — same shape, SpO2-derived
    - 0x5d HRV_EVENT — pre-computed `hr_bpm` and `hrv_ms` from the ring

Outputs:
    - HR distribution (mean / median / percentiles / min / max)
    - Resting HR (lowest mean across IBI bursts)
    - Max HR (highest mean across bursts, smoothed)
    - RMSSD over the entire capture
    - Per-burst HR/RMSSD stats

All HR values are derived from IBI as `60_000 / ibi_ms`. The driver's IBI
decoder is verified to match the on-device DB to within 1.3 ms on average.

**Time semantics:** burst-binning uses `event_time(r)` (= `r.t_event_ms`
when available, else `r.t`). `t_event_ms` is the ring-emitted timestamp
interpolated by the driver's `RingTimeResolver` replica (PROTOCOL.md §7),
so catchup-buffered records get their actual emission time, not the
arrival time. The "burst" concept still makes sense — a burst is a
contiguous physio-time slice — but now bursts also align to wall-clock,
making "HR at 3:15 PM" recoverable with a downstream filter on
`t_event_ms`.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from typing import Any

from . import event_time

# A new burst starts when more than this many ms elapse between consecutive
# IBI records. 60s is generous — IBI records normally arrive every ~5s.
_BURST_GAP_MS = 60_000


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolated percentile (p in 0..100)."""
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def _rmssd(ibi_ms: list[int]) -> float:
    """Root-mean-square of successive differences (the standard HRV metric).
    Returns NaN if fewer than 2 IBIs."""
    if len(ibi_ms) < 2:
        return float("nan")
    diffs = [(ibi_ms[i + 1] - ibi_ms[i]) ** 2 for i in range(len(ibi_ms) - 1)]
    return math.sqrt(sum(diffs) / len(diffs))


def _burst_split(ibi_records: list[tuple[int, list[int]]],
                 gap_ms: int = _BURST_GAP_MS) -> list[list[int]]:
    """Group IBI records into bursts. A burst is a maximal sequence of records
    whose `r.t` values are within `gap_ms` of the previous record. Returns a
    list-of-lists of IBI ms values (one inner list per burst, all IBIs flat).
    """
    if not ibi_records:
        return []
    ibi_records.sort(key=lambda x: x[0])
    bursts: list[list[int]] = [[]]
    last_t: int | None = None
    for t, ibis in ibi_records:
        if last_t is not None and (t - last_t) > gap_ms:
            bursts.append([])
        bursts[-1].extend(ibis)
        last_t = t
    return [b for b in bursts if b]


def compute(records: Iterable, *, include_hrv_records: bool = True) -> dict[str, Any]:
    """One-pass HR aggregation. Returns HR distribution + resting/max HR
    derived from per-burst means + overall RMSSD + per-burst summary.

    `include_hrv_records` (default True) merges 0x5d HRV records' pre-computed
    `hr_bpm` into a cross-check field. Set False to use IBI-derived only.
    """
    ibi_records: list[tuple[int, list[int]]] = []   # (event_ms, [ibi_ms, ...])
    hrv_pairs: list[tuple[int, int, int]] = []      # from 0x5d (event_ms, hr_bpm, hrv_ms)
    earliest_t: int | None = None
    latest_t: int | None = None

    for r in records:
        et = event_time(r)
        if et is not None:
            earliest_t = et if earliest_t is None else min(earliest_t, et)
            latest_t = et if latest_t is None else max(latest_t, et)

        if r.type in ("API_IBI_AND_AMPLITUDE_EVENT", "API_SPO2_IBI_AND_AMPLITUDE_EVENT"):
            ibis = r.data.get("ibi_ms") or []
            if ibis and et is not None:
                ibi_records.append((et, ibis))
        elif r.type == "API_HRV_EVENT" and include_hrv_records:
            d = r.data
            hr_bpm = d.get("hr_bpm")
            hrv_ms = d.get("hrv_ms")
            if hr_bpm is not None and hrv_ms is not None:
                hrv_pairs.append((et, hr_bpm, hrv_ms))

    bursts = _burst_split(ibi_records)
    ibi_ms = [v for b in bursts for v in b]

    if not ibi_ms:
        return {"n_ibis": 0, "n_hrv_records": len(hrv_pairs), "n_bursts": 0}

    # IBI-derived HR per beat
    hr_per_beat = sorted(60_000 / ms for ms in ibi_ms if ms > 0)

    # Per-burst stats: mean HR per burst (≥30 beats to be counted).
    # Per-RECORD RMSSD: each 0x60 carries ~12 contiguous beats (~10s of physio
    # time), so RMSSD WITHIN a record is physiologically meaningful. We still
    # don't compute RMSSD on the concatenated IBI array because adjacent
    # records may be from non-contiguous physio segments even after time-
    # anchoring (e.g. the ring stops emitting IBIs between bursts).
    burst_means: list[float] = []
    for b in bursts:
        if len(b) < 30:
            continue
        burst_means.append(60_000 / (sum(b) / len(b)))

    per_record_rmssds: list[float] = []
    for _, ibis in ibi_records:
        if len(ibis) >= 5:
            r = _rmssd(ibis)
            if not math.isnan(r):
                per_record_rmssds.append(r)

    out: dict[str, Any] = {
        "n_ibis": len(ibi_ms),
        "n_bursts": len(bursts),
        "n_hrv_records": len(hrv_pairs),
        "ibi_ms_distribution": {
            "min": min(ibi_ms),
            "max": max(ibi_ms),
            "mean": round(sum(ibi_ms) / len(ibi_ms), 2),
            "median": statistics.median(ibi_ms),
        },
        "hr_bpm_distribution": {
            "min": round(hr_per_beat[0], 2),
            "p25": round(_percentile(hr_per_beat, 25), 2),
            "median": round(_percentile(hr_per_beat, 50), 2),
            "mean": round(sum(hr_per_beat) / len(hr_per_beat), 2),
            "p75": round(_percentile(hr_per_beat, 75), 2),
            "p90": round(_percentile(hr_per_beat, 90), 2),
            "p99": round(_percentile(hr_per_beat, 99), 2),
            "max": round(hr_per_beat[-1], 2),
        },
        "capture_duration_h": round((latest_t - earliest_t) / 3_600_000, 2)
                              if earliest_t and latest_t else None,
        "_note": "RMSSD computed PER RECORD (each 0x60 ≈ 10s of contiguous beats) "
                 "then averaged — DON'T compute RMSSD on the concatenated IBI "
                 "array, history-fetch flattens timing across hours and produces "
                 "spurious values.",
    }
    if burst_means:
        out["resting_hr_bpm"] = round(min(burst_means), 2)
        out["max_hr_smoothed_bpm"] = round(max(burst_means), 2)
        out["mean_burst_hr_bpm"] = round(sum(burst_means) / len(burst_means), 2)
    if per_record_rmssds:
        per_record_rmssds.sort()
        out["rmssd_per_record_ms"] = {
            "n": len(per_record_rmssds),
            "median": round(_percentile(per_record_rmssds, 50), 2),
            "mean": round(sum(per_record_rmssds) / len(per_record_rmssds), 2),
            "p25": round(_percentile(per_record_rmssds, 25), 2),
            "p75": round(_percentile(per_record_rmssds, 75), 2),
        }

    # HRV cross-check (driver's IBI vs on-device pre-computed HRV record)
    if hrv_pairs:
        hrv_hr_mean = sum(p[1] for p in hrv_pairs) / len(hrv_pairs)
        hrv_ms_mean = sum(p[2] for p in hrv_pairs) / len(hrv_pairs)
        out["hrv_record_hr_mean_bpm"] = round(hrv_hr_mean, 2)
        out["hrv_record_hrv_mean_ms"] = round(hrv_ms_mean, 2)
        out["hr_consistency_delta_bpm"] = round(
            abs(hrv_hr_mean - out["hr_bpm_distribution"]["mean"]), 2)

    return out
