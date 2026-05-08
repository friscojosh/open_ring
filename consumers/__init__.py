"""oura_summary — summary statistics computed from `driver.Record` streams.

This package is **a downstream consumer** of `driver`. It NEVER touches the
wire format directly; it only reads the typed `Record` objects produced by the
driver. The intent is to keep the driver focused on "decode bytes correctly"
and have all derivative analytics live here.

Architecture:

    oura_ring.replay/transport
            │  (yields Record stream)
            ▼
    oura_summary.<module>.compute(records) → dict   ← pure function, one pass
            │
            ▼
    oura_summary.cli                                  ← runs all modules, JSONs

Each `compute()` is a one-pass aggregator that takes any iterable of `Record`
and returns a JSON-serializable dict of summary statistics. Callers can mix
and match — run only `hr.compute()`, or `cli.compute_all()` for the full set.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Union

__version__ = "0.3.0"


def event_time(r) -> int | None:
    """Best-available event-generation time in unix-ms.

    Prefers `t_event_ms` (interpolated time the ring emitted the event,
    derived from the `RingTimeResolver` anchor — see driver/PROTOCOL.md
    §7) and falls back to `t` (driver-receive time) when no anchor was
    active. Synthetic events (`_HANDSHAKE_*`, `_BATTERY`, etc.) always
    fall back to `t` since they have no `ring_time`.

    Use this anywhere the analytics need "when did this happen on the
    ring", not "when did my driver get the bytes". The two diverge by
    seconds for live records and by hours/days for catchup-buffered ones.
    """
    return r.t_event_ms if r.t_event_ms is not None else r.t


def records_from_jsonl(path: Union[str, Path]) -> Iterator:
    """Yield `driver.Record` objects from a JSONL file produced by the
    driver (`driver.cli replay/live`). Skips malformed lines silently.
    """
    from driver import Record  # local import to avoid hard cycle
    with Path(path).open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield Record(
                t=d.get("t"),
                rt=d.get("rt"),
                ctr=d.get("ctr"),
                sess=d.get("sess"),
                tag=d.get("tag", ""),
                type=d.get("type", ""),
                data=d.get("data", {}),
                t_event_ms=d.get("t_event_ms"),
            )


def open_records(path: Union[str, Path]) -> Iterable:
    """Open any input the driver can produce — `.log` (btsnoop) or
    `.jsonl` (already-decoded driver output) — and return a Record
    iterable. Auto-detects by extension.
    """
    p = Path(path)
    if p.suffix.lower() == ".jsonl":
        return records_from_jsonl(p)
    from driver.replay import replay
    return replay(str(p))


# Module imports must follow `event_time` so submodules can use it
# without a circular dependency.
from . import hr, ble, battery, sleep, coverage, activity, temperature  # noqa: E402

__all__ = ["hr", "ble", "battery", "sleep", "coverage", "activity",
           "temperature", "event_time", "records_from_jsonl", "open_records"]
