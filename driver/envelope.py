"""JSONL envelope for decoded records.

Each record on the wire produces one line of JSON of the form:

  {"t": <utc_time_ms>, "rt": <ring_time>, "ctr": <counter>, "sess": <session>,
   "tag": "0xNN", "type": "<canonical name>",
   "t_event_ms": <interpolated unix-ms when the ring generated this event>,
   "data": { ... }}

`t` is the wall-clock when the driver received the notification (or replay
read it). `t_event_ms` is the ring's own view of when the event happened,
computed by linear extrapolation from the most recent `API_TIME_SYNC_IND`
(0x42) anchor — see `persistence.SyncState.to_utc_ms`. `t_event_ms` is
omitted when no valid anchor has been observed yet.

Synthetic driver-side events use the same envelope with `tag` and `type`
prefixed by underscore (e.g., `_HANDSHAKE_OK`, `_BATTERY`, `_TIME_SYNC`).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Record:
    t: int                  # utc_time_ms — Unix epoch ms (driver receive time)
    rt: int | None          # ring_time — uint32 from TLV header (None for synthetic events)
    ctr: int | None         # per-type counter — uint16 from TLV header (None for synthetic)
    sess: int | None        # session_id — uint16 from TLV header (None for synthetic)
    tag: str                # wire byte hex string ("0x60") OR underscore-prefixed for synthetic
    type: str               # canonical name (API_* or _SYNTHETIC_*)
    data: dict[str, Any] = field(default_factory=dict)
    t_event_ms: int | None = None   # interpolated unix-ms of event generation; None if no anchor

    def to_json(self) -> str:
        d = {"t": self.t, "tag": self.tag, "type": self.type}
        if self.rt is not None:   d["rt"] = self.rt
        if self.ctr is not None:  d["ctr"] = self.ctr
        if self.sess is not None: d["sess"] = self.sess
        if self.t_event_ms is not None: d["t_event_ms"] = self.t_event_ms
        d["data"] = self.data
        return json.dumps(d, separators=(",", ":"), allow_nan=False, default=_default)


def _default(o):
    """JSON encoder fallback for NaN floats and bytes."""
    if isinstance(o, float):
        # NaN and infinities → null (per envelope contract: "missing → null")
        return None
    if isinstance(o, (bytes, bytearray)):
        return o.hex()
    raise TypeError(f"Cannot serialize {type(o).__name__}")
