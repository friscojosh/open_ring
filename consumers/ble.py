"""BLE link / connection summary statistics.

Inputs:
    - `_HANDSHAKE_NONCE` synthetic events — one per (re)connect
    - `_HANDSHAKE_OK` — handshake completion
    - `_DISCONNECT` — link drop (live mode only)
    - `_TIME_SYNC_REQ` / `_TIME_SYNC_REPLY` — drift / cadence
    - all inner records — for per-minute ingest rate

Outputs:
    - reconnect cadence: median / mean / max gap-time between handshakes
    - handshake count, time-sync count, ratio
    - per-record-type ingest rate (records/min) — overall and by type
    - capture coverage: total duration vs time spent connected
"""
from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Iterable
from typing import Any

from . import event_time


def compute(records: Iterable) -> dict[str, Any]:
    # Synthetic control-plane events stay on `r.t` (driver-receive time):
    # they ARE arrival-time events on our end, not ring-emitted records.
    # Inner records and the capture-window endpoints use event_time, so
    # the wall-clock window reflects when the ring actually generated the
    # data — not when we received it.
    handshake_ts: list[int] = []
    time_sync_req_ts: list[int] = []
    time_sync_reply_ts: list[int] = []
    disconnect_ts: list[int] = []
    inner_count_by_type: Counter[str] = Counter()
    earliest_t: int | None = None
    latest_t: int | None = None

    for r in records:
        et = event_time(r)
        if et is not None:
            earliest_t = et if earliest_t is None else min(earliest_t, et)
            latest_t = et if latest_t is None else max(latest_t, et)

        if r.type == "_HANDSHAKE_NONCE":
            handshake_ts.append(r.t)
        elif r.type == "_TIME_SYNC_REQ":
            time_sync_req_ts.append(r.t)
        elif r.type == "_TIME_SYNC_REPLY":
            time_sync_reply_ts.append(r.t)
        elif r.type == "_DISCONNECT":
            disconnect_ts.append(r.t)
        elif r.tag.startswith("0x"):
            inner_count_by_type[r.type] += 1

    out: dict[str, Any] = {
        "handshake_count": len(handshake_ts),
        "time_sync_req_count": len(time_sync_req_ts),
        "time_sync_reply_count": len(time_sync_reply_ts),
        "disconnect_count": len(disconnect_ts),
        "reconnect_count": max(0, len(handshake_ts) - 1),
        "inner_record_count": sum(inner_count_by_type.values()),
        "distinct_inner_types": len(inner_count_by_type),
    }

    if earliest_t and latest_t and latest_t > earliest_t:
        duration_h = (latest_t - earliest_t) / 3_600_000
        out["capture_duration_h"] = duration_h
        out["records_per_min_overall"] = (
            sum(inner_count_by_type.values()) / max(1, (latest_t - earliest_t) / 60_000)
        )

    # Reconnect cadence — gap-time between successive handshakes
    if len(handshake_ts) > 1:
        handshake_ts.sort()
        gaps_ms = [handshake_ts[i + 1] - handshake_ts[i] for i in range(len(handshake_ts) - 1)]
        gaps_s = sorted(g / 1000 for g in gaps_ms)
        out["reconnect_gap_s"] = {
            "min": gaps_s[0],
            "median": statistics.median(gaps_s),
            "mean": sum(gaps_s) / len(gaps_s),
            "p90": gaps_s[int(len(gaps_s) * 0.9)] if len(gaps_s) > 10 else gaps_s[-1],
            "max": gaps_s[-1],
        }

    # Time-sync cadence — gaps between successive time-syncs
    if len(time_sync_req_ts) > 1:
        time_sync_req_ts.sort()
        ts_gaps_s = sorted(
            (time_sync_req_ts[i + 1] - time_sync_req_ts[i]) / 1000
            for i in range(len(time_sync_req_ts) - 1)
        )
        out["time_sync_gap_s"] = {
            "min": ts_gaps_s[0],
            "median": statistics.median(ts_gaps_s),
            "max": ts_gaps_s[-1],
        }

    # Per-type ingest rate (records/min) — only for top types
    if earliest_t and latest_t and latest_t > earliest_t:
        duration_min = (latest_t - earliest_t) / 60_000
        if duration_min > 0:
            out["records_per_min_by_type"] = {
                t: round(c / duration_min, 2)
                for t, c in inner_count_by_type.most_common(15)
            }

    return out
