# Oura Ring 4 BLE Protocol — Complete Specification

This document is a self-contained reference describing the proprietary BLE
protocol used by the Oura Ring 4 (firmware family `oreo_2.10.x`), as
reconstructed from:

- Decompilation of `com.ouraring.oura` v7.12.1 (Java/Kotlin sources via
  jadx; native shared objects `libappecore.so`, `libecore.so`,
  `libringeventparser.so` via llvm-objdump).
- BTSnoop / btmon captures of the official phone↔ring traffic over many
  sessions and ring states (boot, idle, sleep, exercise).
- A clean-room reimplementation (`driver/`, this repo) that has been
  validated byte-for-byte against the official phone's outbound writes.

Audience: someone competent in BLE GATT, embedded protocol RE, and
Python; it is dense by design.

Empirical figures throughout have been verified against ≥2 independent
captures unless otherwise noted.

---

## 1. BLE / GATT layer

### 1.1 Service & characteristics

```
Service UUID :  98ed0001-a541-11e4-b6a0-0002a5d5c51b
Notify char  :  98ed0003-a541-11e4-b6a0-0002a5d5c51b   (handle 0x0012, ATT op 0x1B)
Cmd char     :  (resolves to handle 0x0015, write-without-response, ATT op 0x12)
```

The service UUID is stable across firmware revisions and is the only
reliable identifier under BLE LE Privacy (the public MAC rotates as a
Resolvable Private Address — see §1.3).

### 1.2 ATT MTU

The ring streams notifications up to **247 bytes**. Without an explicit
MTU exchange, BlueZ defaults to 23 — long records (e.g. `0x60`, `0x80`,
`0x81`) will fragment or be silently dropped. Issue an `ATT_EXCHANGE_MTU`
of 247 immediately after `start_notify`.

A successful negotiation looks like:
```
client → ring : ATT Exchange MTU Request,  Client RX MTU 247
ring   → client: ATT Exchange MTU Response, Server RX MTU 247
```

### 1.3 Authentication & pairing

The ring requires THREE separate things to accept a central:

| Layer | Material | Where it lives on a paired phone |
|---|---|---|
| Link-layer encryption | LTK (16 B) + EDIV / Rand | `bt_config.conf` `[<MAC>]` `LE_KEY_PENC` |
| LE Privacy resolution | Local IRK (16 B) | `bt_config.conf` `[Adapter]` `LE_LOCAL_KEY_IRK` |
| Application handshake | `auth_key` (16 B AES-128) | `assa-store.realm` (binary realm DB) |

Any one missing produces a distinct symptom:

| Symptom | Cause |
|---|---|
| `ATT_CONNECT` times out — no LL Connect Complete | Ring not advertising (already in another connection). |
| LL Connect → Disconnect ~3-4 s, reason `0x15` | IRK mismatch — ring rejects unrecognised central. |
| LL Connect → Encryption Change Failed | LTK byte order wrong. |
| BLE up, but `0x2f 02 2e <01>` after handshake | `auth_key` mismatch. |

The IRK must be installed on the host adapter:
```sh
sudo btmgmt -i hci0 power off
sudo btmgmt -i hci0 privacy on <hex_irk>
sudo btmgmt -i hci0 power on
```

The `auth_key` lives in `assa-store.realm`, immediately after the marker
bytes `41 41 41 41 11 00 00 10`. Extract by linear scan (sole occurrence
in observed realms).

---

## 2. Frame layers

Both directions on both characteristics are framed identically. There
are **two** layers; a single ATT value carries one layer or the other,
not both. The first byte disambiguates: if it is one of the known
opcodes in the outer catalog (§4), the value is **outer frames**;
otherwise it is the **inner record stream**.

### 2.1 Outer frame

```
+--------+--------+----------------------+
| op:1   | len:1  | body : len bytes     |
+--------+--------+----------------------+
```

- `len` counts only the body, not the header.
- Multiple outer frames may be packed into one ATT value; consume `2 + len`
  bytes and loop.
- The first body byte is by convention the `sub_op` (multiplexes meaning
  within an opcode).

### 2.2 Inner record (TLV)

```
+--------+--------+--------+--------+--------+--------+----------------+
| type:1 | len:1  | ctr_lo | ctr_hi | ses_lo | ses_hi | payload : len-4|
+--------+--------+--------+--------+--------+--------+----------------+
```

- `len` ≥ 4. Body comprises the 4-byte ringTimestamp header + payload.
- Records concatenate up to one ATT MTU; each one is `2 + len` bytes long.
- **`ringTimestamp = (session << 16) | counter`**, both u16 LE. This is
  the canonical event-sequence identifier.
- Two record types deviate from this framing:
  - **`0x33`** uses len = 14 but `(ctr, sess)` is sample data, not a
    ringTimestamp. See §5.x.
  - **`0x56`** is a real type with the standard framing but a 1-byte
    payload; rare in captures (4 occurrences across 40 logs).

### 2.3 Disambiguating outer vs inner

A leading byte in `OPCODES` (§4.1) → outer-frame stream. Otherwise → inner
record stream. The two never mix in a single value. Empirically the cmd
characteristic carries only outer frames (commands); the notify
characteristic carries both (responses + record bursts).

---

## 3. Cryptography

### 3.1 Handshake proof

Given a 15-byte nonce from the ring, the proof is:

```
proof = AES_128_ECB( auth_key, nonce ‖ 0x01 ‖ PKCS5_PAD_FULL_BLOCK )[:16]
```

- `nonce` is exactly 15 bytes.
- The plaintext is built as `nonce ‖ 0x01` (16 B), then padded with
  `0x10 × 16` to yield 32 B of PKCS5 full-block padded plaintext.
- AES-128-ECB encrypt the 32 B; emit the **first** 16 bytes as the proof.

Verified across 484/484 observed nonce/proof pairs.

### 3.2 No further session keys

There is no AEAD/MAC over subsequent traffic. Once the handshake succeeds
all opcodes are sent in clear (relying on link-layer encryption from the
LTK). There is no rolling counter on the application layer.

---

## 4. Outer-frame catalog

### 4.1 Opcode table

| Op | Direction(s) | Name | Notes |
|---:|---|---|---|
| `0x06` | phone→ring | `identity_req` | rare; used during initial pairing |
| `0x07` | ring→phone | `identity_resp` | |
| `0x08` | phone→ring | `time_or_id_req` | startup probe; `08 03 00 00 00` |
| `0x09` | ring→phone | `time_or_id_resp` | |
| `0x0c` | phone→ring | `battery_req` | `0c 00`; sub-op-less, body empty |
| `0x0d` | ring→phone | `battery_resp` | 8 bytes total; voltage `u16 LE` at body[4..6] |
| `0x0e` | phone→ring | `soft_reset_req` | `0e 01 ff` triggers a ring reboot ~22-35 s later |
| `0x0f` | ring→phone | `soft_reset_ack` | status=0 on accept |
| `0x10` | phone→ring | `history_fetch` (GetEvent) | see §6.4 |
| `0x11` | ring→phone | `history_fetch_resp` | |
| `0x12` | phone→ring | `time_sync_req` | see §6.3 |
| `0x13` | ring→phone | `time_sync_resp` | echoes the counter |
| `0x16` | phone→ring | `subscribe` | `16 01 02` enable, `16 01 00` disable |
| `0x17` | ring→phone | `subscribe_ack` | |
| `0x18` | phone→ring | `event_subscribe` | per-category mask |
| `0x19` | ring→phone | `event_resp` | |
| `0x1c` | phone→ring | `state_cmd` | observed `1c 01 bf` |
| `0x1d` | ring→phone | `state_cmd_resp` | |
| `0x1e` | phone→ring | `state_query` | |
| `0x1f` | ring→phone | `state_query_resp` | |
| `0x24` | both | `fw_authorize` | OTA gate |
| `0x28` | phone→ring | `data_flush` | `28 01 00` — release flash-buffered events to the BLE stream |
| `0x29` | ring→phone | `data_flush_ack` | |
| `0x2b` | both | `fw_progress` | OTA progress |
| `0x2c` | both | `fw_bulk` | OTA payload |
| `0x2f` | both | `secure_session` | param R/W, handshake; sub-op multiplexed |

### 4.2 `0x2f` sub-op map

`0x2f` is the most heavily multiplexed opcode; the second byte (`sub_op`)
selects the operation.

| sub | Direction | Form | Meaning |
|---:|---|---|---|
| `0x01` | phone→ring | `2f 01 2b` | request handshake nonce |
| `0x02` | both | `2f 02 ...` | sec_cfg / status responses (varies by next byte) |
| `0x10` | ring→phone | `2f 10 02 02 0a 06 0b 00 0c 00 0d 01 0e 02 10 00 12 00` | sec_cfg capability response (18 B fixed) |
| `0x11` | phone→ring | `2f 11 2d <proof:16>` | submit handshake proof |
| `0x12` | ring→phone | `2f 12 02 02 00 05 01 03 02 05 03 03 04 04 05 01 08 02 09 00` | sec_cfg pre-auth response |
| `0x20` | phone→ring | `2f 02 20 <param_id>` | param read request |
| `0x21` | ring→phone | `2f 21 ... ` | param read response (≥8 B) |
| `0x22` | phone→ring | `2f 03 22 <param_id> <value>` | param write byte 0 |
| `0x26` | phone→ring | `2f 03 26 <param_id> <value>` | param write byte 2 |
| `0x28` | ring→phone | `2f 28 ... (17 B)` | param push notification (set after a write) |
| `0x29` | phone→ring | `2f 0b 29 04 3c 19 03 1e 18 00 00 00 00` | event-subscribe variant; observed 13 B |
| `0x2b` | phone→ring | `2f 01 2b` | (same as `0x01`; aliased) request handshake nonce |
| `0x2c` | ring→phone | `2f 10 2c <nonce:13>` | handshake nonce response (18 B) |
| `0x2d` | phone→ring | `2f 11 2d <proof:16>` | handshake proof submission (19 B) |
| `0x2e` | ring→phone | `2f 02 2e <status>` | handshake completion (status=0 on success) |

### 4.3 Param ID enumeration (sub_op `0x20/0x22/0x26` payloads)

| ID | Name | Notes |
|---:|---|---|
| `0x02` | `PARAM_DHR` | Daytime Heart Rate; byte 0 = mode (0=off, 1=on, 3=burst-3, 4=burst-4), byte 2 = sub-mode (used for mode 3/2 pair) |
| `0x03` | `PARAM_ACTIVITY_HR` | byte 0 toggle |
| `0x04` | `PARAM_SPO2` | byte 0 toggle |
| `0x0B` | `PARAM_ACTIVITY_HR_AUX` | companion to `0x03`; read-only in observed traffic |

The official setup sequence reads `0x02`, `0x04`, then writes `0x03=0x01`,
then reads `0x0b, 0x0d, 0x03, 0x0b, 0x10` (note `0x0b` is read twice).
This handshake-trailing dance is a stateful capability negotiation;
skipping it does not break operation but causes the ring to delay
emitting some event categories.

---

## 5. Inner-record catalog (`RingEventType`)

### 5.1 Type-byte enumeration

The full enum from `RingEventType.java`. Types marked **(unmapped)**
have no enum symbol in the official Android app — they are real wire
tags; the app's parser sees them and discards.

| Tag | Name | Decoder shape (after the 4-byte `(ctr,sess)` header) |
|---:|---|---|
| `0x33` | (unmapped) | **Special framing** — 14-byte body: `<sub_op:u8><seq:u8><6×i16 LE>`. (ctr,sess) bytes ARE sample data, not ringTimestamp. ~50 Hz two-3-axis-samples/record. sub_op ∈ {0x31, 0x32}. |
| `0x41` | `API_RING_START_IND` | Ring boot/start; triggers anchor invalidation if rt regresses. |
| `0x42` | `API_TIME_SYNC_IND` | 9 B: `<token:u8><time_counter:u24 LE><const:5 B>`. `unix_s ≈ time_counter * 256`. **Anchor for time interpolation.** |
| `0x43` | `API_DEBUG_EVENT_IND` | ASCII text payload (state strings like `O2Mode;0`). |
| `0x44` | `API_IBI_EVENT` | (rare) inter-beat interval. |
| `0x45` | `API_STATE_CHANGE_IND` | byte 0 = `STATE_*` enum (§5.3). |
| `0x46` | `API_TEMP_EVENT` | even payload [4..14]; channels are `i16 LE / 100 = °C`. |
| `0x47` | `API_MOTION_EVENT` | 3-axis compact accel (variable). |
| `0x48..0x4f` | sleep summaries / phases | structured proto fields. |
| `0x50` | `API_ACTIVITY_INFO` | activity category + intensity. |
| `0x51..0x52` | `API_ACTIVITY_SUMMARY_1/2` | |
| `0x53` | `API_WEAR_EVENT` | wear/unwear; `STATE_*` enum body. |
| `0x54` | `API_RECOVERY_SUMMARY` | |
| `0x56` | (unmapped) | 1-byte payload, always `0x01`. Always sandwiched between `0x50` (ctr=N) and `0x47` (ctr=N+2) in observed traffic; likely an activity-state-transition marker. |
| `0x5b` | `API_BLE_CONNECTION_IND` | connection event (timing/quality). |
| `0x5c` | `API_USER_INFO` | one-shot per session; user profile. |
| `0x5d` | `API_HRV_EVENT` | rmssd-derived. |
| `0x5e` | `API_SELFTEST_EVENT` | hardware self-test; 2× u16 LE at offsets 0,2. |
| `0x60` | `API_IBI_AND_AMPLITUDE_EVENT` | IBI+amp pairs at variable rate. |
| `0x61` | `API_DEBUG_DATA` | structured debug subtype dispatch (per first body byte). |
| `0x67` | `API_RING_HW_TIME_INFO` | hardware-RTC reference. |
| `0x68` | `API_RAW_PPG_DATA` | declared but not observed in any capture. |
| `0x69` | `API_TEMP_PERIOD` | period summary of `0x46`. |
| `0x6a` | `API_SLEEP_PERIOD_INFO_2` | |
| `0x6b` | `API_MOTION_PERIOD` | uses `MOTION_STATE` enum (§5.3). |
| `0x6c` | `API_FEATURE_SESSION` | 3+ B: byte_0, capability, status, then session-type-specific payload. |
| `0x6e` | `API_SPO2_IBI_AND_AMPLITUDE_EVENT` | as `0x60` but for SpO2 channel. |
| `0x6f` | `API_SPO2_EVENT` | per-sample SpO2. |
| `0x72` | `API_SLEEP_ACM_PERIOD` | |
| `0x73` | `API_EHR_TRACE_EVENT` | exercise-HR raw trace. |
| `0x74` | `API_EHR_ACM_INTENSITY_EVENT` | exercise-HR intensity. |
| `0x75` | `API_SLEEP_TEMP_EVENT` | |
| `0x76` | `API_BEDTIME_PERIOD` | start/end ringTimestamps, bedtime envelope. |
| `0x77` | `API_SPO2_DC_EVENT` | DC channel SpO2. |
| `0x79` | `API_TAG_EVENT` | user-tagged moment. |
| `0x7e` | `API_REAL_STEP_EVENT_FEATURE_ONE` | step counter feature 1. |
| `0x7f` | `API_REAL_STEP_EVENT_FEATURE_TWO` | step counter feature 2. |
| `0x80` | `API_GREEN_IBI_QUALITY_EVENT` | 7 samples × `<value_11bit:u11, qual_a:u3, qual_b:u2>` packed in 14 B. **`value_11bit` = IBI in milliseconds**, NOT raw PPG. Strict quality filter `qual_a ≤ 1, qual_b == 0`. |
| `0x81` | `API_CVA_RAW_PPG_DATA` | session-stateful delta-coded PPG; see §5.4. |
| `0x82` | `API_SCAN_START` | 3+ B: triggering_feature, trigger_reason, classification_metric, candidate_slots[6]. |
| `0x83` | `API_SCAN_END` | mirrors `0x82`. |
| `0x85` | `API_RTC_BEACON_IND` | 10-byte fixed `<unix_s:u32 LE><reserved:4 B (zeros)><trailer:u16 LE>`. trailer ∈ {`0x01f6`, `0x01f8`}. **High-precision wall-clock anchor** (1-second granular vs 0x42's 256-second granular). The official app's enum has no name for this; not used by the native time resolver. |

### 5.2 Channel-bit packing for `0x80`

Each 16-bit sample word is little-endian:
```
bits  0..10 : value_11bit (IBI in ms)
bits 11..13 : qual_a
bits 14..15 : qual_b
```
7 samples per record (14 B). Quality `qual_a ≤ 1, qual_b == 0` selects
samples that match the official app's HR computation (median of filtered
IBIs → instantaneous HR).

### 5.3 Common enums

```
STATE_CHANGE (0x45 / 0x53):
   0  STATE_UNSPECIFIED
   1  STATE_NOT_IN_FINGER
   2  STATE_FINGER_DETECTION
   3  STATE_FINGER_USER_ACTIVE
   4  STATE_FINGER_USER_IN_REST
   5  STATE_FINGER_HR_USER_ACTIVE
   6  STATE_FINGER_HR_USER_IN_REST
   7  STATE_OUT_OF_POWER
   8  STATE_CHARGING_PHASE
   9  STATE_RING_HIBERNATE_LOW_POWER
  20  STATE_PRODUCTION_DIAGNOSTIC
  21  STATE_PRODUCTION_TESTING
  22  STATE_PRODUCTION_TESTING_CHARGING
  30  STATE_HW_TEST

MOTION_STATE (0x6b):
   0  NO_MOTION
   1  RESTLESS
   2  TOSSING_AND_TURNING
   3  ACTIVE
```

### 5.4 `0x81` (`API_CVA_RAW_PPG_DATA`) decode

Stateful delta + absolute encoding. Each byte is processed sequentially;
the running absolute is reset on `0x80` markers.

```
for b in payload:
    if b == 0x80:                              # next 3 bytes = absolute
        absolute = u24(payload[i+1..i+4]); i += 4; emit absolute
    elif b & 0x80:                              # MSB-set delta = signed
        delta = b - 0x100; absolute += delta; emit
    else:                                       # signed 7-bit delta
        absolute += b; emit
```
The stream survives BLE-link reconnects (it's session-stateful on the
ring side); reset the decoder only on `_RING_RESET_ACK` or after a
>60 s gap in receive timestamps.

---

## 6. Control plane

### 6.1 Connect-time sequence (Pattern A: full setup)

The exact byte-for-byte order observed in every fresh-bond connection by
the official app:

```
1.  phone→ring  08 03 00 00 00                        ; time_or_id_req
2.  ring→phone  09 12 02 00 00 ...                    ; time_or_id_resp (18 B)
3.  phone→ring  2f 02 01 00                           ; sec_cfg pre
4.  ring→phone  2f 12 02 02 00 ...                    ; sec_cfg pre resp
5.  phone→ring  2f 02 01 01                           ; sec_cfg neg
6.  ring→phone  2f 10 02 02 0a 06 ...                 ; sec_cfg neg resp (18 B)
7.  phone→ring  2f 01 2b                              ; request nonce
8.  ring→phone  2f 10 2c <nonce:15>                   ; nonce  (18 B)
9.  phone       compute proof = AES_128_ECB(auth_key, nonce ‖ 0x01 ‖ pad)[:16]
10. phone→ring  2f 11 2d <proof:16>                   ; proof  (19 B)
11. ring→phone  2f 02 2e <status>                     ; 0x00 = OK, anything else = fail
12. phone→ring  16 01 02                              ; subscribe enable
13. phone→ring  1c 01 bf                              ; state_cmd (engage data plane)
14. phone→ring  0c 00                                 ; battery_req (probe)
15. phone→ring  2f 02 20 02 ; 2f 02 20 04 ; 2f 02 03 01
                  2f 02 20 0b ; 2f 02 20 0d ; 2f 02 20 03
                  2f 02 20 0b ; 2f 02 20 10            ; capability dance
16. phone→ring  28 01 00                              ; data_flush
17. phone→ring  10 09 <last_ringTimestamp:4 LE> ff ff ff ff ff   ; GetEvent
18. ring→phone  (records stream)
19. phone→ring  10 09 <updated_rt:4 LE> 00 ff ff ff ff           ; ack-fetch (max_events=0)
20. phone→ring  12 09 <token> <counter:3 LE> 00 00 00 00 f6      ; time_sync
21. ring→phone  13 05 <ack> <echo:3 LE> 00                       ; time_sync_resp
```

### 6.2 Pattern catalog

The phone uses different sub-sequences depending on context:

| Pattern | Trigger | Writes | Notes |
|---|---|---|---|
| **A: Full setup** | Fresh app start, new link | ~25 | The full sequence above. |
| **B: Long catchup** | Ring disconnected >hours | ~20 | Like A but longer GetEvent loop. |
| **C: Quick refresh** | Back-to-back polls in same session group | 5 | `0c 00 ; 16 01 02 ; <data flush> ; <getevent> ; <ack>` only. Skip handshake — assumes ring still considers us authenticated. |
| **D: Ad-hoc HR** | User taps "Heart Rate" in app | A then DHR mode 3/2 burst | Re-trigger every 15 s; ring auto-reverts after ~20 s. |

### 6.3 Time sync request (12/09)

```
12 09  <token:1>  <counter:3 LE>  00 00 00 00  f6
       \_random_/\__counter______/\_const_____/ \_const trailer_/

counter = int(time.time()) // 256
trailer = 0xf6  (the phone uses 0xf6 byte-perfectly; ring also accepts 0xf8)
```

The ring responds with `13 05 <ack> <counter_echo:3 LE> 00` within ~50 ms.
Importantly this also stamps the ring's internal RTC: subsequent `0x42`
records reflect the synced wall-clock.

### 6.4 GetEvent / history fetch (10/09 — 11/08)

Request (phone→ring, 11 bytes):
```
10 09  <ringTimestamp:4 LE>  <max_events:1>  <flags:4 LE>
```
- `ringTimestamp`: cursor — ring streams every event with rt > this value.
  0 = full dump (subject to the ring's circular flash buffer).
- `max_events`: ≤255 to fetch data, 0 to ack-only (advance cursor without
  expecting more data).
- `flags`: phone always sends `0xFFFFFFFF`.

Response (ring→phone, 10 bytes):
```
11 08  <status:1>  <sub_status:1>  <last_ring_timestamp:4 LE>  <padding:2 B>
```
- `status` = `0x00` empty / `0xFF` data follows.
- `last_ring_timestamp` is metadata (likely a batch ID); the *real*
  cursor for the next request is the max `ring_time` observed across the
  inner records that arrived in this batch.

**Pair pattern (canonical phone behavior):** every GetEvent burst is
followed ~100 ms later by a second GetEvent with `max_events = 0` and the
updated cursor. The first fetches data, the second tells the ring "I got
through here" — without it, the same events stream again next session.

### 6.5 Data flush (28/01/00)

Releases events that the ring has buffered to flash but not yet pushed to
the BLE stream. Should precede every GetEvent in active polling. Ring
responds with `29 ...` ack; many records may follow before the next
notification cycle.

### 6.6 Subscribe toggle (16/01/0X)

`16 01 02` (enable) and `16 01 00` (disable). Toggling once every ~3
flush cycles refreshes the ring's notify state machine — without it,
some event categories drift into a quiescent state and stop emitting.
This is a phone-observed periodic dance; functionally optional but
required for byte-perfect parity.

### 6.7 DHR (daytime heart-rate) burst

To force live high-rate HR sampling:
```
phone→ring  2f 02 20 02              ; read DHR (capability check)
phone→ring  2f 03 22 02 03           ; write DHR.byte_0 = 3   (burst mode)
phone→ring  2f 03 26 02 02           ; write DHR.byte_2 = 2   (sub-mode)
```
Ring responds with `2f 28 ...` param push notifications and starts
emitting `0x80`/`0x60` samples densely. The ring auto-reverts to mode 0
after ~20 s; the phone re-triggers every ~15 s to keep it engaged.

### 6.8 Soft reset (0e/01/ff)

Rare. `0e 01 ff` triggers a ring reboot 22-35 s later. Ring acks with
`0f ... <status>` within ~150 ms (status=0 on accept). After the reboot
the next reconnect sees `API_RING_START_IND` (0x41) — see §7.3 for the
side-effect on the time anchor.

---

## 7. Time-resolution algorithm

The official app maintains a single anchor `(ring_time, utc_ms,
factor_flag)` and uses linear extrapolation from it. The implementation
lives in the native `libappecore.so` as the C++ class
`RingTimeResolver`. Reproduced exactly:

### 7.1 Anchor structure

```c
struct event_time_mapping_v2_t {
    uint64_t ring_time;     // offset 0x00
    uint64_t utc_ms;        // offset 0x08
    uint8_t  factor_flag;   // offset 0x10  (0 = 100 ms/tick, 1 = 1 ms/tick)
    /* 7 bytes pad */
};
```

### 7.2 `to_utc(target_rt, anchor)`

```c
uint64_t to_utc(uint64_t target_rt, const event_time_mapping_v2_t* a) {
    if (a->utc_ms == 0 || a->ring_time == 0) return 0;       // invalid
    uint64_t factor = (a->factor_flag == 0) ? 100 : 1;
    if (target_rt >= a->ring_time)
        return a->utc_ms + factor * (target_rt - a->ring_time);
    /* target before anchor: saturating sub */
    uint64_t sub = factor * (a->ring_time - target_rt);
    return (a->utc_ms < sub) ? 0 : (a->utc_ms - sub);
}
```

Empirical confirmation: 99.87, 99.87, 100.27 ms/tick across three
independent capture pairs of `0x42` anchors — exactly the 100 ms/tick
constant baked into the native code.

### 7.3 Anchor-update rules

| Event | Action |
|---|---|
| `0x42` `API_TIME_SYNC_IND` | Set `ring_time = record.ring_time`, `utc_ms = (data.unix_s) * 1000`, `factor_flag = (token == 0xfd ? 1 : 0)`. The native code validates `utc_ms` against system clock first; reject if implausibly far. |
| `0x41` `API_RING_START_IND` and `record.ring_time < anchor.ring_time` | Invalidate anchor (zero `ring_time` and `utc_ms`). Models session reset: the ring's `ring_time` counter restarted, our old anchor is stale. |
| Anything else | No effect. |

### 7.4 Validation envelope

The native validator checks the candidate `utc_ms` is within a sane
range of `now`. The minimum safe replica: reject if
`|candidate_utc_ms − system_clock_ms| > 48 h`. Tighter is fine for
production; offline replay should disable validation entirely (system
clock is unrelated to the capture).

### 7.5 What the official app does NOT use

- `0x85` (`API_RTC_BEACON_IND`) is ignored by the native resolver.
  Its `unix_s` field is per-second precise (vs `0x42`'s 256-second
  granularity), and it carries an unmodified ring-RTC reading. A custom
  driver gains useful precision by tracking it as a secondary
  high-resolution anchor.
- Multi-anchor interpolation (e.g. between two `0x42`s). The official
  algorithm is single-anchor + linear; rate variation across the
  interval is unmodeled.

---

## 8. State model

### 8.1 Persistent state (across process restarts)

The phone's `SyncInfoDataStore.SyncInfo` schema (4 longs):

| Field | Meaning |
|---|---|
| `nextEventToSync` | The catchup cursor — the ringTimestamp to send in the next GetEvent. Advance from the max `ring_time` observed across emitted records. |
| `lastSyncCompleteTime` | Wall-clock when the previous catchup completed (informational). |
| `lastTimeSyncRingTime` | `ring_time` of the most recent valid `0x42`. Persisted seed for `to_utc`. |
| `lastTimeSyncUtcTime` | `utc_ms` of the most recent valid `0x42`. |

A clean-room driver mirrors this as `SyncState v4`:
```json
{
  "_format_version": 4,
  "ring_serial": "<16 hex>",
  "last_saved_at_ms": 1777258601685,
  "last_ring_timestamp": 8638609,
  "anchor_ring_time": 8638042,
  "anchor_utc_ms": 1777258345000,
  "anchor_factor_flag": 0
}
```
Atomic write via tmp-file + rename. Migration rules (v1/v2 → v3 → v4)
must preserve cursor; anchor restarts at zero on first migration.

### 8.2 Per-session ephemeral state

| State | Lives in | Notes |
|---|---|---|
| BLE session (`session` field of TLV) | Ring firmware, increments on each ring-side stream restart | Coupled with ring's internal sampler state. |
| `factor_flag` of the running anchor | Driver in-memory + persisted | Set from `0x42` token. |
| CVA-PPG running absolute (§5.4) | Driver in-memory; reset on `_RING_RESET_ACK` or 60 s gap | Survives BLE reconnects. |
| DHR mode (3 = burst) | Ring; ~20 s timeout | Re-engage with `set_dhr_mode(3)` periodically. |

### 8.3 Re-derivable state (need not be persisted)

- Ring identity, capabilities — re-issued every connect by `0x09` /
  `0x12` / `0x10` responses.
- BLE link details (negotiated MTU, RSSI, channel map, RPA) — link-layer.
- `ring_serial` is informative only; useful to detect ring swaps and
  refuse to apply a stale cursor to a different device.

---

## 9. Stream-shape semantics

### 9.1 Inner record envelope (consumer view)

```json
{
  "t":          1777007497060,        // wall-clock when the driver received the notification
  "rt":         103884,               // ring_time = (sess << 16) | ctr
  "ctr":        38348,                // u16 LE from TLV
  "sess":       1,                    // u16 LE from TLV
  "tag":        "0x42",               // wire tag (hex)
  "type":       "API_TIME_SYNC_IND",  // canonical name
  "t_event_ms": 1777007360000,        // interpolated event-time (when ring generated it); null if no anchor
  "data":       { ... type-specific decoded fields ... }
}
```

`t` and `t_event_ms` differ by the buffering window: live records have
Δ ≈ BLE-pipeline latency (≤ 300 ms); catchup records can have Δ in
hours-to-days (the ring buffered them while disconnected).

### 9.2 Empirical rates

| Phenomenon | Rate / period |
|---|---|
| `ring_time` ticks (default mode) | exactly 10/s = 100 ms/tick |
| `ring_time` ticks (burst / `factor_flag=1`) | 1000/s = 1 ms/tick |
| `0x42` `API_TIME_SYNC_IND` cadence | 5–9 per session, irregular |
| `0x42.unix_s` granularity | 256-second multiples |
| `0x85` `API_RTC_BEACON_IND` cadence | 1–4 per session |
| `0x85.unix_s` granularity | 1 second |
| `0x33` (when active) | ~24.7 records/sec → 49.4 samples/sec |
| `0x80` GREEN_IBI quality | bursts during HR mode |
| `0x81` CVA raw PPG | bursts during HR mode, MTU-bound |
| Steady-state record stream during wear | ~10–40 records/sec depending on activity |

### 9.3 Filters every consumer must apply

1. **Skip records with `rt ≥ 2^31`** (TLV-misparse signature).
2. **Don't advance the cursor or interpolate `t_event_ms`** from records
   whose `canonical_type` matches `(not_in_enum_*)` or `UNKNOWN_*`. Use a
   `is_structurally_unknown(type_byte)` helper. Currently catches
   `0x33` (real data, but its (ctr,sess) bytes carry sample data) and
   `0x56` (real but rare; conservative skip).
3. **`0x33`-specific framing exception**: bytes 2..14 of the body carry
   sensor data, not a ringTimestamp. Reconstruct full body as
   `pack('<HH', ctr, sess) + payload` and decode separately.

### 9.4 Synthetic envelope events

The driver emits non-TLV synthetic records using the same envelope, with
`tag` and `type` prefixed by underscore:

| Synthetic | Source | Use |
|---|---|---|
| `_BATTERY` | `0x0d` response | voltage_mv + state bytes |
| `_TIME_SYNC_REQ` / `_TIME_SYNC_REPLY` | `0x12` / `0x13` | Driver wall-clock alignment trace. |
| `_HANDSHAKE_NONCE` / `_HANDSHAKE_PROOF` / `_HANDSHAKE_OK` / `_HANDSHAKE_FAIL` | `0x2f` 2c/2d/2e | Handshake observability. |
| `_RING_RESET_ACK` | `0x0f` after a `0x0e 01 ff` | Drives `0x81` decoder reset. |
| `_HISTORY_FETCH_RESP` | `0x11` | last_ring_timestamp surfaced for observability; NOT the cursor. |
| `_STATE_PULSE` | periodic flush + state probe | Liveness. |

These never bear a `rt` and never participate in cursor advance.

---

## 10. Reconnect / resume logic

A correct reconnect:

1. Scan by service UUID (RPA-resilient) → device.
2. Connect; negotiate MTU 247.
3. Subscribe to notify char.
4. Run handshake (§6.1 steps 1–11).
5. Issue subscribe-enable + state-cmd + battery-probe (steps 12–14).
6. Run capability dance (step 15).
7. Send `data_flush` (§6.5).
8. Send `GetEvent(last_ring_timestamp = saved_cursor, max_events=255, flags=0xFFFFFFFF)`.
9. Drain incoming records; track max observed `ring_time`.
10. ~100 ms later, send the ack-fetch with `max_events = 0` and the
    updated cursor.
11. Send a `0x12` time-sync (refreshes ring RTC; elicits `0x42` shortly).
12. Enter steady state: every 20 s issue `data_flush + GetEvent` (catchup
    of any newly buffered events). Every 3rd cycle, additionally toggle
    the subscribe state (disable, sleep 2.5 s, re-enable).

`Pattern C` (quick refresh) skips steps 1, 4, 5–6, 8, 9, 10, 11 if the
caller knows the ring still considers us authenticated (e.g. a poll
< 30 s after disconnect). Behavior is undefined if the ring has
de-authorized; expect timeouts rather than auth-failure responses.

---

## 11. Failure modes & recovery

| Failure | Detection | Recovery |
|---|---|---|
| Handshake nonce timeout | `_expect(is_nonce)` raises after 5 s | Disconnect; reconnect (often a stuck BLE link). |
| Handshake status non-zero | `_HANDSHAKE_FAIL` synthetic | Almost always wrong `auth_key`. Validate against realm. |
| GetEvent response `status=0xFF` but no records arrive | wall-clock idle on notify char > 5 s | Send `data_flush` + retry GetEvent. |
| Ring stops emitting subscribed events | record gap > 30 s with link still up | Subscribe-toggle dance (§6.6). |
| Ring counter regressed | `inner.ring_time < anchor.ring_time` AND `tag == 0x41` | Invalidate anchor (§7.3). Cursor stays — old cursor remains a valid `eventStartTimestamp` because the ring's flash buffer is keyed by emission order, not rt. |
| Disconnect with reason `0x15` after 3-4 s | btmon | IRK mismatch on the host. |
| Disconnect with reason `0x13` (remote terminate) | btmon | Ring reboot or user-initiated unpair. |

---

## 12. Empirical constants

| Symbol | Value | Source |
|---|---|---|
| `MS_PER_TICK_DEFAULT` | 100 | `ring_time_to_utc` immediate; verified across 3 anchor pairs (99.87, 99.87, 100.27) |
| `MS_PER_TICK_BURST` | 1 | `ring_time_to_utc` immediate (factor_flag = 1 path) |
| `ANCHOR_VALIDATION_WINDOW_MS` | 48 × 3600 × 1000 | Conservative replica of the native validator |
| `MAX_TIMEOUT_HANDSHAKE_S` | 5 | Empirical; ring nonce arrives within ~80 ms in healthy sessions |
| `FLUSH_INTERVAL_S_DEFAULT` | 20 | Phone-observed cadence |
| `SUBSCRIBE_TOGGLE_EVERY_N_FLUSHES` | 3 | Phone-observed |
| `DHR_REFRESH_INTERVAL_S` | 15 | Phone-observed (ring auto-reverts at ~20 s) |
| `BLE_MTU` | 247 | Ring's preferred ATT MTU |
| `AUTH_KEY_LEN` | 16 | AES-128 ECB |
| `NONCE_LEN` | 15 | Pads to 16 with `0x01`; full PKCS5 yields 32 B plaintext |
| `PROOF_LEN` | 16 | First 16 bytes of AES-128-ECB output |

---

## 13. Reconstruction recipe (TL;DR)

To go from zero to a working clean-room driver:

1. Implement BLE GATT client on the service UUID (§1.1), with MTU 247
   and notify-handler delivering a queue of `(timestamp_ms, value)`
   tuples.
2. Implement a parser that, for each notification value, decides outer
   vs inner by first-byte (§2.3) and walks frames per §2.1 / §2.2.
3. Implement the handshake (§3.1, §6.1 steps 1–11).
4. Implement `SyncState` per §8.1.
5. Implement `to_utc_ms` per §7.2 + the update rules in §7.3.
6. Implement GetEvent + ack-fetch per §6.4.
7. Implement the per-record decoder dispatch from §5.1, with the special
   case for `0x33` (full-body decode, §5.x) and `0x81` (running CVA-PPG
   state, §5.4).
8. Apply the filter rules in §9.3 to avoid corruption from
   misparse-shaped bytes.
9. For every emitted record envelope, populate `t_event_ms = to_utc_ms
   (rt)` if and only if the type is structurally trustworthy.

A driver that passes a byte-for-byte comparison against the official
app's outbound writes — and whose output matches the app's `RawRingEvent`
stream after time resolution — is correct. The pre-recorded btsnoop
captures in this repo are a regression-quality test set: a clean-room
implementation is verified against ~953,000 records covering every
record type observed in the wild.
