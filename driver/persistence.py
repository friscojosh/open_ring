"""Persistent sync state for delta-resume across process restarts.

The ring streams events tagged with a monotonic 32-bit `ringTimestamp`. On
reconnect we send `GetEvent(eventStartTimestamp = T, maxEvents = 255,
flags = 0xFFFFFFFF)` and the ring streams every event newer than `T`. The
ring's response (`0x11 ...`) carries the latest timestamp it delivered; we
save that as the next session's `T`. If `T` is forgotten, the next reconnect
starts from `T = 0` (full history dump if the ring still has it, otherwise
nothing).

That single timestamp is the ONLY piece of state whose loss has real
consequences. Everything else — identity, params, current state, RPA — is
either ephemeral (per-link) or re-acquired from the ring on reconnect.

Format (current = v4):
    {
      "_format_version": 4,
      "ring_serial": "40170D2607008061",     // null until first observed
      "last_saved_at_ms": 1777258601685,
      "last_ring_timestamp": 8638609,        // u32: cursor for next GetEvent
      "anchor_ring_time": 8638042,           // ringTime of last valid 0x42
      "anchor_utc_ms": 1777258345000,        // utc_ms (= unix_s * 1000)
      "anchor_factor_flag": 0                // 0 = 100ms/tick, 1 = 1ms/tick
    }

The anchor triple mirrors `event_time_mapping_v2_t` from libappecore.so's
`RingTimeResolver`. Each fresh `API_TIME_SYNC_IND` (0x42) updates it; on
reconnect it seeds `to_utc_ms()` so events arriving before any new 0x42
still get an interpolated wall-clock.

Migration:
- v1 / v2 files (with `cursors: {sub_op→cursor}` and optional `fetch_plan`)
  are auto-migrated. Each (sub_op, cursor) pair is a 32-bit timestamp split
  as `(low_byte, high_24_bits)`, so the migrated `last_ring_timestamp` is
  `max(sub_op | (cursor << 8) for sub_op, cursor in cursors.items())`. We
  pick the MAX because each saved entry is a real past fetch timestamp; the
  largest is our most-recent sync point.
- v3 files load with zero anchor (invalid); first observed 0x42 fills it.

Atomicity: written via tmp-file + rename so a crash mid-write never corrupts
the existing file.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


_FORMAT_VERSION = 4
_DEFAULT_PATH = "~/.local/share/oura_ring/cursors.json"

# Anchor validation window: candidate utc_ms must be within ±48h of system
# clock to be accepted. Mirrors the (less explicit) sanity check in
# `RingTimeResolver::handle_api_time_sync_ind`.
_ANCHOR_VALIDATION_WINDOW_MS = 48 * 3600 * 1000


class SyncState:
    """Persists the cursor + ring_time→utc anchor across runs.

    Two pieces of state:
      - `last_ring_timestamp` (u32): catchup cursor for next `GetEvent`.
        0 means no prior sync.
      - `(anchor_ring_time, anchor_utc_ms, anchor_factor_flag)`: the time
        anchor used to interpolate per-event wall-clock from `ring_time`.
        Mirrors `event_time_mapping_v2_t` in libappecore.so. All zeros = no
        valid anchor (interpolation returns None).
    """

    __slots__ = ("path", "last_ring_timestamp", "ring_serial", "last_saved_at_ms",
                 "anchor_ring_time", "anchor_utc_ms", "anchor_factor_flag")

    def __init__(self, path: str | Path = _DEFAULT_PATH) -> None:
        self.path: Path = Path(path).expanduser()
        self.last_ring_timestamp: int = 0
        self.ring_serial: str | None = None
        self.last_saved_at_ms: int | None = None
        self.anchor_ring_time: int = 0
        self.anchor_utc_ms: int = 0
        self.anchor_factor_flag: int = 0

    def load(self) -> int:
        """Load state from disk. Returns the loaded `last_ring_timestamp`
        (0 if unset / file missing / corrupt). Auto-migrates v1/v2 files.
        """
        if not self.path.exists():
            return 0
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return 0
        if not isinstance(data, dict):
            return 0

        self.ring_serial = data.get("ring_serial")
        self.last_saved_at_ms = data.get("last_saved_at_ms")

        version = data.get("_format_version", 1)
        if version >= 3:
            try:
                self.last_ring_timestamp = max(0, int(data.get("last_ring_timestamp", 0)))
            except (TypeError, ValueError):
                self.last_ring_timestamp = 0
            if version >= 4:
                try:
                    self.anchor_ring_time = max(0, int(data.get("anchor_ring_time", 0)))
                    self.anchor_utc_ms = max(0, int(data.get("anchor_utc_ms", 0)))
                    self.anchor_factor_flag = int(data.get("anchor_factor_flag", 0)) & 0xff
                except (TypeError, ValueError):
                    self.anchor_ring_time = self.anchor_utc_ms = self.anchor_factor_flag = 0
        else:
            # Migrate: each old (sub_op, cursor) pair encodes timestamp =
            # sub_op | (cursor << 8). Take the max — that's the most-recent
            # past fetch timestamp recorded.
            raw = data.get("cursors") or {}
            best = 0
            for k, v in raw.items():
                try:
                    sub_op = int(k); cursor = int(v)
                except (TypeError, ValueError):
                    continue
                ts = (sub_op & 0xff) | ((cursor & 0xffffff) << 8)
                if ts > best:
                    best = ts
            self.last_ring_timestamp = best
        return self.last_ring_timestamp

    def save(self) -> None:
        """Serialize state to disk atomically (write-tmp + rename)."""
        self.last_saved_at_ms = int(time.time() * 1000)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "_format_version": _FORMAT_VERSION,
            "ring_serial": self.ring_serial,
            "last_saved_at_ms": self.last_saved_at_ms,
            "last_ring_timestamp": self.last_ring_timestamp,
            "anchor_ring_time": self.anchor_ring_time,
            "anchor_utc_ms": self.anchor_utc_ms,
            "anchor_factor_flag": self.anchor_factor_flag,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.path)

    def update(self, ring_timestamp: int) -> bool:
        """Advance the saved timestamp. Monotone — never regresses (the
        ring's event log is append-only). Returns True if the stored value
        changed.
        """
        ts = int(ring_timestamp) & 0xffffffff
        if ts > self.last_ring_timestamp:
            self.last_ring_timestamp = ts
            return True
        return False

    def update_anchor(self, ring_time: int, utc_ms: int,
                      factor_flag: int = 0,
                      validate_against_now: bool = True) -> bool:
        """Set the (ring_time, utc_ms, factor_flag) anchor from a fresh
        `API_TIME_SYNC_IND` (0x42) or `API_RING_START_IND` (0x41). Mirrors
        `RingTimeResolver::handle_api_time_sync_ind`. Returns True if the
        stored anchor changed.

        `validate_against_now`: if True, candidate utc_ms must be within
        ±48h of system clock — rejects corrupted ring timestamps. Set False
        for offline replay where system time != capture time.
        """
        if validate_against_now:
            now_ms = int(time.time() * 1000)
            if abs(utc_ms - now_ms) > _ANCHOR_VALIDATION_WINDOW_MS:
                return False
        rt = int(ring_time) & 0xffffffff
        if rt == 0 or utc_ms <= 0:
            return False
        # Monotonic: only accept anchors with newer ring_time (or any anchor
        # if previously invalid). Avoids regressing the anchor when
        # out-of-order events arrive during catchup.
        if self.anchor_ring_time != 0 and rt < self.anchor_ring_time:
            return False
        if (rt == self.anchor_ring_time
                and utc_ms == self.anchor_utc_ms
                and (factor_flag & 0xff) == self.anchor_factor_flag):
            return False
        self.anchor_ring_time = rt
        self.anchor_utc_ms = int(utc_ms)
        self.anchor_factor_flag = int(factor_flag) & 0xff
        return True

    def invalidate_anchor(self) -> None:
        """Zero the anchor (mirrors `RingTimeResolver::invalidate_mapping`)."""
        self.anchor_ring_time = 0
        self.anchor_utc_ms = 0

    def to_utc_ms(self, target_ring_time: int) -> int | None:
        """Interpolate a ring_time to unix-ms via single-anchor linear
        extrapolation. Mirrors `RingTimeResolver::to_utc` from
        libappecore.so:

            factor = 100 if anchor_factor_flag == 0 else 1   (ms per tick)
            utc_ms = anchor_utc_ms + factor * (target - anchor_ring_time)

        Default mode is exactly 10 ticks/sec (100 ms/tick); burst mode is
        1000 ticks/sec (1 ms/tick).

        Returns None when no valid anchor exists (vs. the native code's 0
        sentinel — None is a clearer Python signal). Result accuracy
        degrades as |target - anchor_ring_time| grows: anchor utc_ms is
        granular to 256 s, and rt-rate may drift across disconnects.
        """
        if self.anchor_ring_time == 0 or self.anchor_utc_ms == 0:
            return None
        factor = 100 if self.anchor_factor_flag == 0 else 1
        delta = int(target_ring_time) - self.anchor_ring_time
        if delta >= 0:
            return self.anchor_utc_ms + factor * delta
        result = self.anchor_utc_ms - factor * (-delta)
        return result if result > 0 else None

    def set_ring_serial(self, serial: str | None) -> None:
        """Stamp the ring's serial so the file documents which device it's for.
        If a different serial is loaded later, callers can use this to detect a
        ring swap and discard the old timestamp instead of mis-applying it."""
        self.ring_serial = serial

    def matches_ring(self, serial: str) -> bool:
        """True if this store's serial is unset or matches `serial`. Use this
        before applying the loaded timestamp to a freshly-connected ring."""
        return self.ring_serial is None or self.ring_serial == serial

    def __repr__(self) -> str:
        return (f"SyncState(path={self.path!s}, "
                f"last_ring_timestamp={self.last_ring_timestamp}, "
                f"ring_serial={self.ring_serial!r}, "
                f"last_saved_at_ms={self.last_saved_at_ms})")


# Backward-compat alias so existing imports keep working during transition.
# Will be removed once callers are updated.
CursorStore = SyncState
