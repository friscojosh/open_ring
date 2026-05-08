"""Wire framing.

Two layers:
  1. Outer frame on either characteristic: <op:1><len:1><sub:1><payload:len-1>
     Multiple frames may pack into one ATT value; consume `2 + len` and loop.
  2. Inner record stream on the notify char: TLV
     <type:1><len:1><ctr_lo:1><ctr_hi:1><sess_lo:1><sess_hi:1><payload:len-4>
     Records concatenate up to MTU.

A single notification value may carry EITHER outer frames OR inner records.
The first byte disambiguates: if it's a known outer-frame opcode, treat the
value as outer frames; otherwise as inner-record stream.
"""
from __future__ import annotations

from dataclasses import dataclass


# Outer-frame opcode catalog. Bidirectional (phone↔ring); no
# "phone-only" or "ring-only" coloring at this layer.
OPCODES: dict[int, str] = {
    0x06: "identity_req",       0x07: "identity_resp",
    0x08: "time_or_id_req",     0x09: "time_or_id_resp",
    0x0c: "battery_req",        0x0d: "battery_resp",
    0x0e: "soft_reset_req",     0x0f: "soft_reset_ack",
    0x10: "history_fetch",      0x11: "history_fetch_resp",
    0x12: "time_sync_req",      0x13: "time_sync_resp",
    0x16: "subscribe",          0x17: "subscribe_ack",
    0x18: "event_subscribe",    0x19: "event_resp",
    0x1c: "state_cmd",          0x1d: "state_cmd_resp",
    0x1e: "state_query",        0x1f: "state_query_resp",
    0x24: "fw_authorize",
    0x28: "data_flush",         0x29: "data_flush_ack",
    0x2b: "fw_progress",
    0x2c: "fw_bulk",
    0x2f: "secure_session",
}


@dataclass
class OuterFrame:
    opcode: int
    sub_op: int | None       # first byte of payload, by convention
    body: bytes              # everything AFTER length, INCLUDING sub_op
    raw: bytes               # entire frame including opcode + length

    @property
    def name(self) -> str:
        return OPCODES.get(self.opcode, f"unknown_{self.opcode:02x}")


@dataclass
class InnerRecord:
    type_byte: int
    counter: int             # uint16 LE — low 16 bits of ring_time
    session: int             # uint16 LE — high 16 bits of ring_time
    payload: bytes           # bytes after the 4-byte ctr+sess header

    @property
    def ring_time(self) -> int:
        """The 32-bit `ringTimestamp` for this record. The TLV's `(counter,
        session)` pair is actually one u32 stored little-endian: low 16 bits
        in `counter`, high 16 bits in `session`. Verified empirically against
        `monday_morning_wk2.log` — the first record after a `GetEvent(T)`
        request has `ring_time == T`, and the value advances monotonically
        across all records in the stream regardless of type.

        IMPORTANT — this is NOT a wall-clock timestamp despite the name.
        The official app's `GetEvent.java` calls it `eventStartTimestamp`,
        but empirically it's a per-stream event-sequence counter: it
        increments by 1 each time the ring emits a streamable event,
        regardless of how much real time has passed. Concretely:

          - Default mode: exactly 10 ticks/sec = 100 ms/tick. Verified
            against the native `RingTimeResolver::to_utc` which uses a
            constant factor of 100 ms per tick.
          - Burst/extended mode: 1 ms/tick (1 kHz). Ring signals this via a
            `0xfd` token in the next 0x42 (TimeSyncInd) anchor, which sets
            `factor_flag=1` in the native anchor struct.
          - 5839 ring_time units between two records ≈ 583.9 s in default
            mode (10 ticks/sec).

        Use it as a monotonic cursor for `request_events_since(...)` and as
        a sequence comparator. Don't multiply by anything to get seconds.
        """
        return (self.session << 16) | self.counter


def parse_outer_frames(value: bytes) -> list[OuterFrame]:
    """Return zero or more outer frames packed into one ATT value.

    Stops parsing on the first byte that isn't a known opcode (which
    typically signals an inner-record stream instead).
    """
    out: list[OuterFrame] = []
    i = 0
    while i + 2 <= len(value):
        op, ln = value[i], value[i + 1]
        if op not in OPCODES or i + 2 + ln > len(value):
            break
        body = value[i + 2:i + 2 + ln]
        sub = body[0] if ln >= 1 else None
        out.append(OuterFrame(opcode=op, sub_op=sub, body=body, raw=value[i:i + 2 + ln]))
        i += 2 + ln
    return out


def parse_inner_records(value: bytes) -> list[InnerRecord]:
    """Return zero or more inner records concatenated into one notification.

    Stops parsing only when the bytes can't form a complete TLV record
    (truncated body, or `ln < 4` so there's no room for the standard
    ringTimestamp header). Records that ARE structurally complete but
    semantically suspect (e.g. `0x33` with the wrong length, or any
    `UNKNOWN_*` type byte) are still emitted — downstream consumers
    distinguish trustworthy records from misparse fragments via
    `decoders.is_structurally_unknown`. Emitting them keeps the record
    count transparent and lets callers see what the framing layer saw.
    """
    out: list[InnerRecord] = []
    i = 0
    while i + 2 <= len(value):
        t, ln = value[i], value[i + 1]
        body = value[i + 2:i + 2 + ln]
        if len(body) != ln or ln < 4:
            break
        ctr = body[0] | (body[1] << 8)
        sess = body[2] | (body[3] << 8)
        out.append(InnerRecord(type_byte=t, counter=ctr, session=sess, payload=body[4:]))
        i += 2 + ln
    return out


def looks_like_outer_frame(value: bytes) -> bool:
    """First-byte test: is this an outer frame stream or inner records?"""
    return bool(value) and value[0] in OPCODES
