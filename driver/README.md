# `driver` — pure-Python clean-room driver for the Oura Ring 4 BLE protocol

A clean-room reimplementation of the Oura Ring 4's BLE → biometric data
pipeline. Built from static RE of the official Android app
(`com.ouraring.oura` v7.12.1), the native shared objects
(`libringeventparser.so`, `libecore.so`, `libappecore.so`), and 953,206
inner records of empirical validation across 40 unique BLE captures.

**No vendored binaries. No proprietary blobs. Stdlib + `bleak` + `cryptography`.**

For the complete wire-protocol specification — every opcode, every field
layout, the cryptographic handshake, the time-resolution algorithm —
see [`../PROTOCOL.md`](../PROTOCOL.md). This README is the consumer-facing
introduction; PROTOCOL.md is the full reference.

## What's mapped

The protocol is **fully mapped** as of 2026-05-07:

- All 24 outer-frame opcodes, with sub-op multiplexing for `0x2f`
- All 50 inner-record types observed on the wire
- The cryptographic handshake (`AES-128-ECB(auth_key, nonce ‖ 0x01)[:16]`)
- The `RingTimeResolver` algorithm (single-anchor linear extrapolation,
  100 ms/tick or 1 ms/tick burst mode) — reverse-engineered from
  `libappecore.so::RingTimeResolver` and verified against three
  independent anchor pairs (99.87, 99.87, 100.27 ms/tick observed)
- Connection-time byte sequences for the four observed phone patterns
  (full setup / long catchup / quick refresh / ad-hoc HR)

**Decode coverage: 100.000%** of 953,206 inner records across 40 unique
captures. 0 decode errors. 4 records out of 953K (`UNKNOWN_0x56`,
`(not_in_enum_33)`, `(not_in_enum_56)`) are real wire types whose
official-app enum has no name, but which we still decode structurally.

## Quick start

```sh
# Replay an existing btsnoop capture as JSONL on stdout
python3 -m driver.cli replay capture.log | head

# Find your ring's current MAC (handles BLE Random Resolvable Private
# Address rotation by scanning the GATT service UUID, not MAC)
python3 -m driver.cli discover

# Live stream from a ring (requires `pip install bleak`).
# mac=None auto-discovers by service UUID on every (re)connect:
python3 -m driver.cli live --realm path/to/assa-store.realm

# Or pin to an identity MAC (works after BLE pairing — see PROTOCOL.md §1.3):
python3 -m driver.cli live --mac A0:38:F8:A4:09:C9 --realm path/to/assa-store.realm
```

```python
import asyncio
from driver import RingState, ClientState, SyncState
from driver.transport import OuraRingClient

async def main():
    ring = RingState()
    client_state = ClientState()
    sync = SyncState()                   # persists cursor + time anchor
    async with OuraRingClient(mac=None, realm_path="assa-store.realm",
                               sync_state=sync) as r:
        await r.set_spo2(True)           # on-demand control
        async for rec in r.stream():
            ring.apply(rec); client_state.apply(rec)
            if rec.type == "API_IBI_AND_AMPLITUDE_EVENT":
                # rec.t        = driver-receive time (ms since epoch)
                # rec.t_event_ms = ring-emit time (interpolated from 0x42 anchor)
                print(rec.t_event_ms or rec.t, rec.data["ibi_ms"])

asyncio.run(main())
```

## Two planes — control + data

The driver separates two concerns:

**Data plane (autonomous catch-up)** — every (re)connect runs handshake +
history-fetch (PROTOCOL.md §6.4) to retrieve records buffered while
disconnected. Surfaced as `_HISTORY_FETCH_REQ` / `_HISTORY_FETCH_RESP`
synthetic events.

**Control plane (on-demand)** — high-level methods to actively configure
the ring:

| Method | What it does |
|---|---|
| `await client.set_spo2(on)` | Toggle SpO₂ sampling |
| `await client.set_activity_hr(on)` | Toggle activity heart-rate |
| `await client.set_dhr_mode(mode, sub_mode)` | Set Daytime HR mode + sub-mode (mode=3 → burst) |
| `await client.request_hr_on_demand()` | Trigger the documented burst HR check |
| `await client.read_param(0x04)` | Read 4-byte SpO₂ struct |
| `await client.write_param_byte0(p, v)` | Generic byte-0 write |
| `await client.request_history(cursor)` | Manual delta-sync fetch |

Surfaced as `_PARAM_READ` / `_PARAM_READ_RESP` / `_PARAM_WRITE_B0` /
`_PARAM_WRITE_B2` / `_PARAM_PUSH` synthetic events.

## JSONL envelope

One line per record:

```json
{"t":1777033068525,"rt":76522,"ctr":11609,"sess":2,"tag":"0x60",
 "type":"API_IBI_AND_AMPLITUDE_EVENT",
 "t_event_ms":1777033065123,
 "data":{"ibi_ms":[555,867,843,827,817,764],
         "amp":[0,896,992,1168,1024,928],"amp_shift":4}}
```

| Field | Always present | Meaning |
|---|---|---|
| `t` | yes | Driver-receive time (UTC ms since epoch). When the BLE notification arrived. |
| `rt` | inner records only | `ringTimestamp = (sess << 16) \| ctr` — the canonical event-sequence id |
| `ctr` | inner records only | Per-type counter (u16 LE from TLV header) |
| `sess` | inner records only | Session id (u16 LE from TLV header) |
| `tag` | yes | Wire byte hex (`"0xNN"`) OR underscore-prefixed for synthetic events |
| `type` | yes | Canonical name (`API_*`) or `_*` synthetic |
| `t_event_ms` | when anchor available | **Ring-emit time** (UTC ms) — interpolated from the latest `API_TIME_SYNC_IND` anchor. Diverges from `t` by ≤300 ms for live records, hours/days for catchup-buffered ones. |
| `data` | yes | Type-specific decoded fields |

**`t` vs `t_event_ms`:** `t` is when the driver received the notification.
`t_event_ms` is when the ring actually generated the event. For live
records they differ by BLE latency (~200 ms). For catchup records they
can differ by hours (the ring buffered events while disconnected, then
dumped them in a single burst). Use `t_event_ms` for analytics that
care about physiological time; use `t` for transport-level analysis.

See [`PROTOCOL.md`](../PROTOCOL.md) §7 for the full `RingTimeResolver`
algorithm. The driver maintains the anchor automatically — every `0x42
API_TIME_SYNC_IND` updates it; every `0x41 API_RING_START_IND` with a
regressed `ringTimestamp` invalidates it.

## Synthetic event types

| Type | When emitted | Plane |
|---|---|---|
| `_HANDSHAKE_NONCE` / `_PROOF` / `_OK` / `_FAIL` | Handshake sequence | Lifecycle |
| `_TIME_SYNC_REQ` / `_TIME_SYNC_REPLY` | `0x12` / `0x13` | Lifecycle |
| `_BATTERY` | `0x0d` battery response (voltage_mv) | Lifecycle |
| `_DISCONNECT` | BLE link drop (live only) | Lifecycle |
| `_HISTORY_FETCH_REQ` / `_HISTORY_FETCH_RESP` | `0x10` / `0x11` with cursor | Data |
| `_PARAM_READ` / `_PARAM_READ_RESP` | `0x2F` sub `0x20` / `0x21` | Control |
| `_PARAM_WRITE_B0` / `_PARAM_WRITE_B2` | `0x2F` sub `0x22` / `0x26` | Control |
| `_PARAM_PUSH` | `0x2F` sub `0x28` (unsolicited from ring) | Control |
| `_STATE_PULSE` | `0x1f` autonomous state-machine pulse | Lifecycle |
| `_RING_RESET_ACK` | `0x0f` after a phone-issued `0x0e 01 ff` reset | Lifecycle |

## Persistence — `SyncState` (v4 format)

Across-process state is minimal — four longs that mirror the official
app's `SyncInfoDataStore.SyncInfo` schema:

```json
{
  "_format_version": 4,
  "ring_serial": "40170D2607008061",
  "last_saved_at_ms": 1777258601685,
  "last_ring_timestamp": 8638609,        // catchup cursor
  "anchor_ring_time": 8638042,           // ringTime of last valid 0x42
  "anchor_utc_ms": 1777258345000,        //   utc_ms of  "
  "anchor_factor_flag": 0                // 0=100ms/tick, 1=1ms/tick burst
}
```

The cursor is the only field whose loss matters operationally — without
it, every reconnect issues `GetEvent(0)` and refetches whatever the
ring's circular flash buffer still holds. The anchor triple lets
`t_event_ms` work on the *first* records of a fresh session, before the
new `0x42` lands.

```python
from driver import SyncState
from driver.transport import OuraRingClient

sync = SyncState("~/.local/share/driver/cursors.json")
async with OuraRingClient(mac=None, realm_path="assa-store.realm",
                           sync_state=sync) as r:
    async for rec in r.stream(): ...
# Auto-saved every 64 cursor advances + on disconnect/cancel/shutdown.
# Atomic write (tmp + rename), monotonic-only updates, corrupt-file safe.
```

CLI form: `python -m driver.cli live --sync-state-file PATH` (default
`~/.local/share/driver/cursors.json`) or `--no-sync-state-file` to
disable persistence entirely.

Migration is automatic: v1/v2 cursor maps `{sub_op → cursor}` collapse
to `max(sub_op | (cursor << 8))`; v3 (single `last_ring_timestamp`)
loads with a zero anchor; v4 is current.

## State models

Two small dataclasses that consume the JSONL stream and track ring +
driver state — no I/O, no transport coupling:

| `RingState` | `ClientState` |
|---|---|
| BLE link, identity (firmware/serial) | Connection phase (DISCONNECTED → CONNECTING → HANDSHAKING → SUBSCRIBED → STREAMING) |
| Unified `StateChange` enum (current state + name + text) | Handshake / time-sync counters |
| Sub-state machines: DHR, CVA, A:SA, EHR (parsed from `0x43` debug strings) | Records seen + per-type coverage (count, last counter, last session) |
| Battery (level%, voltage mV), charging, orientation | Autonomous catch-up: history fetches, cursor + anchor |
| `params[pid]` — last-seen 4-byte parameter struct per ID | On-demand control: per-param read/write/push counts |

```python
from driver import replay, RingState, ClientState
ring = RingState(); client = ClientState()
for rec in replay("capture.log"):
    ring.apply(rec); client.apply(rec)
print(client.snapshot())   # {phase: STREAMING, handshake_count: 168, …}
print(ring.snapshot())     # {state: 3 STATE_FINGER_USER_ACTIVE, dhr_state: 1, …}
```

## What's decoded

50 record types, structured fields for every one. Highlights:

| Tag | Type | What |
|---|---|---|
| `0x42` | `API_TIME_SYNC_IND` | Time anchor: `time_counter` + `ring_unix_time_approx_s` (256s granular). Drives `t_event_ms`. |
| `0x85` | `API_RTC_BEACON_IND` | High-precision wall-clock anchor: 1-second granular `unix_time_s`. (Official app ignores this; we use it as a precision bonus.) |
| `0x60` | `API_IBI_AND_AMPLITUDE_EVENT` | 11-bit bit-packed IBI + amplitude with shift; **average matches on-device DB to 1.3 ms** over 77 K samples |
| `0x80` | `API_GREEN_IBI_QUALITY_EVENT` | 7 samples × `<value_11bit, qual_a, qual_b>`; `value_11bit` is IBI in ms (not raw PPG); strict filter `qual_a≤1, qual_b==0` reproduces the app's HR computation |
| `0x81` | `API_CVA_RAW_PPG_DATA` | Stateful delta-coded raw 24-bit ADC. **45,851 driver samples = exact prefix of `TimeseriesDbPpgSample`, sample-for-sample, 100%** |
| `0x46` | `API_TEMP_EVENT` | 7 channels × i16 LE / 100 °C; ranges match DB **byte-for-byte** |
| `0x47` | `API_MOTION_EVENT` | `acm_x/y/z` ranges match DB exactly (`[-968..1016]`, etc.) |
| `0x5d` | `API_HRV_EVENT` | HR avg matches IBI-derived avg within **0.2 BPM** |
| `0x6f` | `API_SPO2_EVENT` | 16,905 per-sample percent values in physiologically correct 80–100% range |
| `0x6a` | `API_SLEEP_PERIOD_INFO_2` | `average_hr`, `breath`, `sleep_state`, mzci/dzci/cv |
| `0x61` | `API_DEBUG_DATA` | Sub-byte dispatched into 28 typed parsers (FuelGaugeStatistics, AfePeriodTick, BatteryLevelChanged, ChargerInformation, …); 99.5%+ coverage with cross-validated values |
| `0x33` | `(not_in_enum_33)` | Real but unmapped: 14-byte body `<sub_op:u8><seq:u8><6×i16 LE>` = two 3-axis sensor samples at ~50 Hz. Two sub_op variants (0x31, 0x32). |
| `0x56` | `(not_in_enum_56)` | Real but unmapped: 1-byte payload always `0x01`, sandwiched between `0x50` and `0x47` events. Likely an activity-state-transition flag. |

For the complete record-type catalog (every byte, every field), see
[PROTOCOL.md §5](../PROTOCOL.md#5-inner-record-catalog-ringeventtype).

## Validation summary

Full regression suite in `tools/verify_claims.py`. Across 40 unique
btsnoop captures totaling **953,206 inner records**:

- **100.000% structural decode** (953,206 of 953,206)
- **0 decode errors**
- **0 unknown record types** — every byte mapped
- 484/484 handshake nonce/proof pairs verify against `auth_key`
- 166/166 time-sync formula checks (`counter = int(time.time()) // 256`)
- ATT MTU = 247 verified
- IBI: 99.7% coverage, average matches DB within 1.3 ms (845.0 vs 846.3)
- TempEvent / MotionEvent: ranges match DB byte-for-byte exact
- 8 ring-debug Realm tables verified within ±5% of ground truth
- Charger serial reconstructed exactly: `'40260D2606050378'`
- 100% overlap of distinct `battery_percentage` values vs DB
- Time-rate constant: 100 ms/tick verified across three independent
  anchor pairs (99.87, 99.87, 100.27)

## Architecture

```
btsnoop / live BLE bytes
   │
   ▼
framing.parse_outer_frames / parse_inner_records   ← TLV / opcode walker
   │
   ▼
decoders.decode(type_byte, payload)                ← per-type wire decoders
   │
   ▼   replay._outer_to_record / transport._outer_to_records
envelope.Record  ┐                                 ← typed dataclass with
                 │                                   t, rt, ctr, sess, tag,
                 │                                   type, data, t_event_ms
                 ├──→  consumer (your code) — JSONL stdout, file, network
                 │
                 ├──→  state.RingState.apply(rec)
                 │     state.ClientState.apply(rec)  ← state-tracking models
                 │
                 ├──→  persistence.SyncState         ← cursor + time anchor
                 │     (cross-session)                  (v4 schema)
                 │
                 └──→  consumers.{hr,ble,battery,
                                     sleep,coverage,
                                     activity,
                                     temperature,plot} ← derivative statistics
                                                       (separate package)
```

## Summary statistics: `consumers`

A separate package that consumes the driver's `Record` stream and
produces derivative health statistics. Pure-Python, JSON-serializable,
one-pass per module. v0.2.0 uses `t_event_ms` for all real-record
analytics so catchup-buffered records bin to the day they were actually
recorded, not the day we received them. See `consumers/README.md` for
details.

```sh
python -m consumers.cli capture.log
python -m consumers.cli capture.log --module hr --json
```

## Provenance

This driver is built directly off the verified findings in
`oura_truth_table.md`, the auto-extracted parser metadata in
`wireformat_extract.json`, and the high-frequency wire-format decoders
disassembled byte-for-byte from `libringeventparser.so` (arm64-v8a, md5
`9941cfb8214faf55150a0b6082127e90`). The time-resolution algorithm is
from `libappecore.so::RingTimeResolver`. The handshake crypto is from
`libsecrets.so` interface inspection plus 484/484 nonce/proof pair
verification.

For the complete protocol specification — every opcode, every field,
every observed sequence — see [`../PROTOCOL.md`](../PROTOCOL.md).
