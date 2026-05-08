"""Live BLE transport — `OuraRingClient`.

Async client that:
  1. Connects to the ring via `bleak` (cross-platform BLE).
  2. Subscribes to the notify char.
  3. Runs the secure handshake (AES-128-ECB-PKCS5).
  4. Performs initial time-sync.
  5. Subscribes to event categories.
  6. Streams decoded `Record` objects to the caller.
  7. Auto-reconnects when the ring drops (~every few minutes per spec).

Live transport requires the `bleak` package; install with:
    pip install bleak

Crypto requires either `cryptography` or `/usr/bin/openssl`.

The decode pipeline (`framing` + `decoders`) is shared 1:1 with offline
replay, so a btsnoop test harness validates the live decode end-to-end.

Cannot be tested without a real ring; the structure mirrors the verified
control-plane sequences from `sunday_evening.log`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import struct
import time
from collections.abc import AsyncIterator
from typing import Any

from .crypto import compute_handshake_proof, extract_auth_key_from_realm
from .decoders import CvaPpgDecoder, canonical_type, decode, is_structurally_unknown
from .envelope import Record
from .persistence import SyncState
from .framing import (
    OPCODES,
    OuterFrame,
    looks_like_outer_frame,
    parse_inner_records,
    parse_outer_frames,
)


log = logging.getLogger(__name__)

# GATT topology (from `Constants.java`, verified):
SERVICE_UUID = "98ed0001-a541-11e4-b6a0-0002a5d5c51b"
# Default scan timeout when discovering by service UUID. The ring advertises
# every ~150ms while in idle, so 10s is plenty of margin.
_DISCOVER_TIMEOUT_S = 10.0
WRITE_CHAR   = "98ed0002-a541-11e4-b6a0-0002a5d5c51b"
NOTIFY_CHAR  = "98ed0003-a541-11e4-b6a0-0002a5d5c51b"


def _make_time_sync_frame() -> bytes:
    """Build a 12/09 time-sync request frame.

        12 09 <token:1> <counter:3 LE> 00 00 00 00 f6

    `counter = int(time.time()) // 256` — verified across 484 reconnects.
    Trailing byte is `0xf6`, NOT `0xf8` — verified byte-for-byte against
    every TimeSync write in sunday/monday/tuesday phone btsnoops. Earlier
    driver versions used `0xf8`; the ring accepted it (no rejection observed)
    but for byte-perfect protocol parity we use the phone's value.
    """
    token = secrets.token_bytes(1)
    counter = int(time.time()) // 256
    counter_bytes = counter.to_bytes(3, "little")
    return b"\x12\x09" + token + counter_bytes + b"\x00\x00\x00\x00\xf6"


# ----- Discovery ------------------------------------------------------------


async def discover_ring(timeout: float = _DISCOVER_TIMEOUT_S,
                        adapter: str | None = None):
    """Scan for any device advertising the Oura GATT service UUID and return
    the bleak `BLEDevice` (or None if no ring found within `timeout`).

    Use this to handle BLE Random Resolvable Private Address (RPA) rotation:
    the ring's identity address (e.g. `A0:38:F8:...`) is only visible to a
    BLE-paired peer; from an unpaired host you'll see a rotating MAC like
    `67:46:94:...`. Scan-by-service-UUID survives rotation because the
    advertised service stays the same.

    Returns the `BLEDevice` so the caller can hand it directly to
    `BleakClient(...)` — this is more reliable than passing the MAC string,
    because on Linux/BlueZ the string form requires a cache lookup that can
    miss a freshly-discovered RPA.
    """
    try:
        from bleak import BleakScanner
    except ImportError as e:
        raise RuntimeError(
            "bleak is required for live BLE; install with `pip install bleak`"
        ) from e
    log.info("scanning %.1fs for service %s …", timeout, SERVICE_UUID)
    kwargs: dict[str, Any] = {}
    if adapter is not None:
        kwargs["adapter"] = adapter
    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: SERVICE_UUID in (ad.service_uuids or []),
        timeout=timeout,
        **kwargs,
    )
    if device is None:
        log.warning("no ring found in %.1fs", timeout)
        return None
    log.info("found ring at %s (name=%r, RSSI=%s)",
             device.address, getattr(device, "name", None),
             getattr(device, "rssi", "?"))
    return device


# ----- Client ---------------------------------------------------------------


class OuraRingClient:
    """Async live BLE client.

    Usage:
        async with OuraRingClient(mac="A0:38:F8:A4:09:C9", auth_key=key) as client:
            async for rec in client.stream():
                print(rec.to_json())
    """

    def __init__(
        self,
        mac: str | None = None,
        *,
        auth_key: bytes | None = None,
        realm_path: str | os.PathLike | None = None,
        event_categories: tuple[tuple[int, int], ...] = (
            # (category, flags) tuples — flags are u16-LE bitmasks.
            # Verified byte-invariant against the official app's connect
            # sequence in sunday_evening.log.
            (0x14, 0x1000),  # category 0x14 (20)
            (0x18, 0x1000),  # category 0x18 (24)
            (0x28, 0x0900),  # category 0x28 (40)
            (0x34, 0x0400),  # category 0x34 (52)
            (0x04, 0x1000),  # category 0x04 (4)
            (0x08, 0x1000),  # category 0x08 (8)
        ),
        reconnect: bool = True,
        sync_state: "SyncState | None" = None,
        sync_save_every: int = 4,
        adapter: str | None = None,
        discover_timeout: float = _DISCOVER_TIMEOUT_S,
        pair: bool = False,
        mtu: int = 247,
        skip_catchup: bool = False,
        flush_interval_s: float = 20.0,
        quick_refresh: bool = False,
        # Deprecated aliases — accepted for one release so existing callers
        # don't break. Drop after callers are updated.
        cursor_store: "SyncState | None" = None,
        cursor_save_every: int | None = None,
        max_catchup_per_session: int | None = None,
    ):
        """Construct a client. If `mac` is None, scans for the ring by service
        UUID on every connect — robust against BLE Random Resolvable Private
        Address rotation. Pass an explicit MAC if you've already paired (BlueZ
        will then resolve RPAs for you) or if you want to skip the scan.
        """
        if (auth_key is None) == (realm_path is None):
            raise ValueError("Pass exactly one of auth_key= or realm_path=")
        if realm_path is not None:
            auth_key = extract_auth_key_from_realm(realm_path)
        if auth_key is None or len(auth_key) != 16:
            raise ValueError("auth_key must resolve to 16 bytes")
        self.mac = mac
        self._user_supplied_mac = mac is not None
        self.adapter = adapter
        self.discover_timeout = discover_timeout
        self.pair = pair
        self.mtu = max(23, int(mtu))
        self.skip_catchup = skip_catchup
        # Deprecated-flag back-compat: cursor_store / cursor_save_every /
        # max_catchup_per_session were the v2 names for sync_state /
        # sync_save_every / (no analogue — phone always sends 1 fetch). Accept
        # the old kwargs but quietly translate them.
        if sync_state is None and cursor_store is not None:
            sync_state = cursor_store
        if cursor_save_every is not None:
            sync_save_every = cursor_save_every
        # max_catchup_per_session is silently ignored — irrelevant under the
        # corrected timestamp-based catchup. Caller logged a deprecation in
        # the CLI shim.
        _ = max_catchup_per_session

        self.auth_key: bytes = auth_key
        self.event_categories = event_categories
        self.reconnect = reconnect
        # Sync persistence: stores the single ringTimestamp cursor across
        # process restarts. The ONLY field of ClientState whose loss matters
        # for correctness — see oura_ring.persistence for rationale.
        self.sync_state = sync_state
        if sync_state is not None:
            sync_state.load()
        self._sync_save_every = max(1, sync_save_every)
        self._sync_updates_since_save = 0
        # Wall-clock interval (seconds) between automatic
        # `data_flush + GetEvent` pairs after the initial catchup. The
        # official phone app fires this every ~20 s mid-session; without it
        # the ring buffers events in flash and never delivers them on the
        # live stream. 0 = disable the loop. ring_time can't drive this
        # because it pauses when no events fire.
        self.flush_interval_s = max(0.0, float(flush_interval_s))
        # Pattern-C "quick refresh": skip handshake / time-sync / paramRead
        # sweep on connect. Use only if you JUST disconnected from a full-
        # setup session and the ring still considers you authenticated.
        self.quick_refresh = bool(quick_refresh)
        self._flush_task: asyncio.Task | None = None
        # `_consumer_task` owns the raw notify queue: it parses every
        # incoming notification, advances `sync_state.last_ring_timestamp`
        # in real time, and forwards parsed Records to `_record_q`. The
        # user-facing `stream()` then reads from `_record_q`. This split
        # is what lets `_fetch_with_ack` send its ack with the up-to-date
        # ring_timestamp (the consumer has already processed records that
        # arrived during the 100ms inter-fetch gap).
        self._consumer_task: asyncio.Task | None = None
        # Setup helpers (handshake / time-sync ack) need to "expect" specific
        # notification frames mid-flow. Since the consumer task is the sole
        # owner of `_notify_q`, callers register a (predicate, Future) here
        # and the consumer fulfills the Future when a matching frame arrives
        # (instead of forwarding it to `_record_q`). This avoids a race
        # between setup helpers and the consumer for the same queue.
        self._expect_waiters: list[tuple[Any, asyncio.Future]] = []
        self._client = None       # bleak.BleakClient
        self._notify_q: asyncio.Queue[tuple[float, bytes]] = asyncio.Queue()
        self._record_q: asyncio.Queue["Record"] = asyncio.Queue()

    # ----- async context manager -----

    async def __aenter__(self) -> OuraRingClient:
        try:
            from bleak import BleakClient
        except ImportError as e:
            raise RuntimeError(
                "bleak is required for live BLE; install with `pip install bleak`"
            ) from e
        self._BleakClient = BleakClient
        await self._connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Stop the periodic flush loop FIRST so it doesn't try to write
        # over a half-torn-down connection.
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):
                pass
            self._flush_task = None
        # Stop the consumer task (was parsing notifications + advancing
        # sync_state in real time). Cancel after flush_task because the
        # flush loop may still be reading sync_state during its own cleanup.
        if self._consumer_task is not None and not self._consumer_task.done():
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except (asyncio.CancelledError, Exception):
                pass
            self._consumer_task = None
        # Always flush sync state on shutdown — losing the last advance means
        # the next session re-fetches the same window.
        self._save_sync_state_safe()
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    def _save_sync_state_safe(self) -> None:
        """Save sync state to disk, swallowing any error (a failed save
        should never crash the streamer — worst case is we re-sync next time)."""
        if self.sync_state is None:
            return
        try:
            self.sync_state.save()
            self._sync_updates_since_save = 0
        except Exception as e:
            log.warning("sync_state save failed: %s", e)

    # Deprecated alias — keeps any external callers working for one release.
    _save_cursors_safe = _save_sync_state_safe

    # ----- connection lifecycle -----

    async def _connect(self) -> None:
        # If no MAC was supplied (or the previous one is stale due to RPA
        # rotation), scan for the ring by service UUID. The BLEDevice we get
        # back from the scan is what we hand to BleakClient — passing a raw
        # MAC string forces BlueZ to look up the device in its cache, which
        # may not contain the freshly-rotated RPA we just discovered.
        ble_device = None
        if self.mac is None:
            ble_device = await discover_ring(timeout=self.discover_timeout,
                                              adapter=self.adapter)
            if ble_device is None:
                raise RuntimeError(
                    f"no ring found (service {SERVICE_UUID}) within "
                    f"{self.discover_timeout}s — is the ring within range "
                    "and not currently connected to your phone?"
                )
            self.mac = ble_device.address
        target = ble_device if ble_device is not None else self.mac
        log.info("Connecting to %s", self.mac)
        client_kwargs: dict[str, Any] = {}
        if self.adapter is not None:
            client_kwargs["adapter"] = self.adapter

        # Disconnect callback — bleak invokes this when it sees the link drop.
        # Bridge to the queue so `stream()` surfaces a `_DISCONNECT` event with
        # the timing context we need to debug "connects then disconnects".
        connect_t = time.time()
        def on_disconnect(_client) -> None:
            t = time.time()
            uptime = t - connect_t
            step = getattr(self, "_last_setup_step", "<pre-handshake>")
            log.warning("disconnected after %.2fs (last successful step: %s)",
                        uptime, step)
            # 2-5s uptime with no step progress is the classic signature of
            # the BLE supervision timeout (~4s default) firing because the
            # peer side stopped acking. Most common cause: the Oura app on
            # your phone reconnected and stole the link.
            if 1.0 <= uptime <= 6.0 and step in ("<pre-handshake>", "ble_connected",
                                                  "mtu_negotiated", "notify_subscribed"):
                log.warning("⚠ %.1fs ≈ BLE supervision timeout. Most likely "
                            "cause: the Oura app on your phone reconnected "
                            "and stole the link. Try force-quitting the Oura "
                            "app or putting the phone in airplane mode, then "
                            "retry within ~30s before the ring re-pairs.",
                            uptime)
            self._notify_q.put_nowait((t, b"\x00__DISCONNECT__"))

        client_kwargs["disconnected_callback"] = on_disconnect
        self._client = self._BleakClient(target, **client_kwargs)

        try:
            await self._client.connect()
        except Exception as e:
            log.warning("connect to %s failed: %s — most common causes: "
                        "phone has the ring connected, or RPA rotated. "
                        "Will rediscover on next attempt.", self.mac, e)
            if not getattr(self, "_user_supplied_mac", False):
                self.mac = None
            raise
        log.info("BLE connected to %s", self.mac)
        self._last_setup_step = "ble_connected"

        # Optional BlueZ-level pairing. Many wearables require LE Secure
        # Connections key-exchange before they accept GATT operations; without
        # a bond, the peripheral drops the link after a few seconds (the
        # "supervision timeout signature").
        if self.pair:
            try:
                paired = await self._client.pair()
                log.info("BLE pair() returned: %s", paired)
                self._last_setup_step = "ble_paired"
            except Exception as e:
                log.warning("BLE pair() raised: %s — continuing anyway, but "
                            "if the ring drops shortly the lack of a bond is "
                            "the likely cause", e)

        # MTU negotiation — Oura streams ~247-byte notifications. BLE default
        # is 23 bytes; without an MTU exchange the ring won't send long records.
        # bleak's `_acquire_mtu()` issues an ATT Exchange MTU when BlueZ
        # supports it; otherwise we set `_mtu_size` directly to suppress the
        # bleak warning and tell its higher-level code to accept long
        # notifications (the actual wire MTU is whatever BlueZ + kernel
        # negotiated during connect, which we can't change post-hoc).
        if hasattr(self._client, "_acquire_mtu"):
            try:
                await self._client._acquire_mtu()
            except Exception as e:
                log.warning("MTU acquisition failed: %s — falling back to "
                            "explicit _mtu_size override", e)
        mtu = getattr(self._client, "mtu_size", None)
        if mtu is None or mtu < self.mtu:
            # Tell bleak our requested MTU; this suppresses the
            # "Using default MTU value" warning AND lets bleak treat incoming
            # notifications as up to `self.mtu` bytes. If the wire actually
            # negotiated less, the kernel will fragment / the ring will
            # respect its own MTU; this just keeps bleak from doing extra
            # gating on our side.
            try:
                self._client._mtu_size = self.mtu
                log.info("set bleak _mtu_size = %d (acquire reported %s)",
                         self.mtu, mtu)
                mtu = self.mtu
            except Exception as e:
                log.warning("could not set _mtu_size on bleak client: %s", e)
        else:
            log.info("negotiated MTU = %d", mtu)
        if mtu is not None and mtu < 100:
            log.warning("effective MTU=%d is still small; if you only see "
                        "tiny records (TimeSync etc.) and the ring drops "
                        "after ~60s, the wire MTU is the cause. Try BlueZ "
                        "experimental mode or upgrade kernel/BlueZ.", mtu)
        self._last_setup_step = "mtu_negotiated"

        # Subscribe to notify char (CCCD `01 00` is handled by bleak)
        def on_notify(_char, value: bytearray) -> None:
            self._notify_q.put_nowait((time.time(), bytes(value)))
        await self._client.start_notify(NOTIFY_CHAR, on_notify)
        log.info("subscribed to notify char %s", NOTIFY_CHAR)
        self._last_setup_step = "notify_subscribed"

        # Start the consumer task BEFORE setup so notifications that arrive
        # during the handshake / fetch get parsed in real time. This is what
        # lets _fetch_with_ack send a byte-perfect ack: by the time the
        # ack-window timer expires, the consumer has already advanced
        # sync_state.last_ring_timestamp from the in-flight records.
        if self._consumer_task is None or self._consumer_task.done():
            self._consumer_task = asyncio.create_task(
                self._consumer_loop(), name="oura_record_consumer")
            self._last_setup_step = "consumer_started"

        try:
            await self._setup_official_app_sequence()
            log.info("setup complete; streaming")
        except TimeoutError as e:
            # Most actionable diagnostic: the ring accepted our connection but
            # didn't respond to one of our setup writes. This usually means the
            # auth_key is wrong (handshake) or the phone yanked the connection
            # back (control-plane writes silently dropped).
            log.error("setup timeout at step %r: %s — check auth_key matches "
                      "this ring AND the phone isn't holding the connection",
                      self._last_setup_step, e)
            raise

    async def _setup_official_app_sequence(self) -> None:
        """Replay the exact connect sequence the official Android app uses.
        Verified byte-for-byte against `logs/sunday_evening.log` (a btsnoop
        capture from the real app's first reconnect).

        The sequence is more elaborate than just handshake + subscribe + catchup;
        the ring depends on a specific multi-step state-machine progression
        that includes parameter reads (registering our interest in config
        changes), poll/ack opcodes (`1c 01 bf`, `0c 00`, `28 01 00`), and
        per-sub-op history requests using saved cursors.

        Skipping any of these — as our earlier simplified version did — works
        for handshake + identity but the ring won't push biometric records.

        If `self.quick_refresh` is set, runs Pattern C instead (5-write fast
        poll). See `_quick_refresh_sequence` for caveats.
        """
        if self.quick_refresh:
            await self._quick_refresh_sequence()
            return

        # ---- Pre-handshake init ----
        # Opcode 0x08 sub-op 0x03 — purpose unclear (possibly version probe).
        await self._write(b"\x08\x03\x00\x00\x00", response=False)
        # Two early 2F sub-op 0x01 reads — observed but not yet RE'd.
        await self._write(b"\x2f\x02\x01\x00", response=False)
        await self._write(b"\x2f\x02\x01\x01", response=False)
        self._last_setup_step = "pre_handshake_done"

        # ---- Secure handshake ----
        await self._handshake()
        self._last_setup_step = "handshake_ok"

        # ---- Subscribe enable ----
        # `16 01 02` registers us with the event/record stream. Ring acks
        # with `17 01 02`. Without this, per-category subscribes are dropped.
        await self._write(b"\x16\x01\x02", response=False)
        self._last_setup_step = "subscribe_enabled"

        # ---- Time-sync ----
        await self._time_sync_now()
        self._last_setup_step = "time_sync_sent"

        # ---- Mid-setup poll (purpose unknown but app sends it) ----
        await self._write(b"\x1c\x01\xbf", response=False)

        # ---- Per-category subscribes (5-byte frames: cat + u16 LE flags) ----
        for category, flags in self.event_categories:
            frame = bytes([0x18, 0x03, category & 0xff,
                            flags & 0xff, (flags >> 8) & 0xff])
            await self._write(frame, response=False)
        self._last_setup_step = "events_subscribed"

        # ---- Mid-setup poll ----
        await self._write(b"\x0c\x00", response=False)

        # ---- Initial param read sweep (registers interest in config events) ----
        # The ring tracks "who's reading what" and pushes config-change events
        # only to readers. Without these reads, _PARAM_PUSH events never fire.
        # The full sweep mirrors the official app byte-for-byte from
        # latest_btsnoop_hci.log writes #15-#22:
        await self._write(bytes([0x2f, 0x02, 0x20, 0x02]), response=False)  # read DHR
        await self._write(bytes([0x2f, 0x02, 0x20, 0x04]), response=False)  # read SpO2
        await self._write(bytes([0x2f, 0x02, 0x03, 0x01]), response=False)  # write sub_op 03 = 01
        await self._write(bytes([0x2f, 0x02, 0x20, 0x0b]), response=False)  # read 0x0b
        await self._write(bytes([0x2f, 0x02, 0x20, 0x0d]), response=False)  # read 0x0d
        await self._write(bytes([0x2f, 0x02, 0x20, 0x03]), response=False)  # read 0x03
        await self._write(bytes([0x2f, 0x02, 0x20, 0x0b]), response=False)  # read 0x0b again
        await self._write(bytes([0x2f, 0x02, 0x20, 0x10]), response=False)  # read 0x10
        self._last_setup_step = "param_sweep_done"

        # ---- Final ack BEFORE history fetches ----
        # Note: removed two earlier-driver-version writes that aren't in the
        # official app's sequence:
        #   - `2f 0b 29 04 3c 19 03 1e 18 00 00 00 00` (config push) —
        #     it's in sunday_evening.log but NOT in latest_btsnoop_hci.log;
        #     the firmware/app may have stopped requiring it.
        #   - extra `0c 00` poll between param sweep and `28 01 00` —
        #     not in the latest btsnoop either.
        await self._write(b"\x28\x01\x00", response=False)

        # ---- Catch-up: history-fetch with saved cursors ----
        # Ad-hoc callers (e.g., one-shot HR reading) can skip this — they
        # only want fresh records the ring is about to push, not the
        # backlog of buffered records.
        if self.skip_catchup:
            log.info("skipping catchup (skip_catchup=True)")
            self._last_setup_step = "catchup_skipped"
        else:
            await self._catchup()
            self._last_setup_step = "catchup_sent"

        # Kick off the periodic data_flush+fetch loop (matches the phone's
        # mid-session pattern). Disabled if flush_interval_s == 0 OR if
        # skip_catchup is set (one-shot callers like request_hr.py don't
        # want the background loop firing). Cancelled in __aexit__.
        if self.flush_interval_s > 0 and not self.skip_catchup:
            self._flush_task = asyncio.create_task(
                self._periodic_flush_loop(),
                name="oura_periodic_flush",
            )
            self._last_setup_step = "flush_loop_started"

    async def _disconnected_or_idle(self) -> None:
        # Detect disconnect via bleak's is_connected attribute
        while self._client and self._client.is_connected:
            await asyncio.sleep(0.5)

    # ----- write helper -----

    async def _write(self, data: bytes, *, response: bool = True) -> None:
        if self._client is None or not self._client.is_connected:
            raise RuntimeError("not connected")
        await self._client.write_gatt_char(WRITE_CHAR, data, response=response)

    async def _expect(self, predicate, timeout: float = 5.0) -> tuple[float, bytes]:
        """Wait for a notification matching `predicate(value)`. Returns the
        matching (timestamp, value). Raises TimeoutError on no match.

        Registers (predicate, future) with the consumer task so we don't
        race the consumer for `_notify_q`. The consumer fulfills the future
        the next time a matching notification arrives, instead of forwarding
        that notification to `_record_q`. Non-matching notifications continue
        to flow through the consumer normally.
        """
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        entry = (predicate, future)
        self._expect_waiters.append(entry)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            # Remove from waiters list if still there (e.g., on timeout)
            try:
                self._expect_waiters.remove(entry)
            except ValueError:
                pass

    # ----- secure handshake -----

    async def _handshake(self) -> None:
        # Phone → Ring: 2F 01 2B   (start)
        await self._write(b"\x2f\x01\x2b", response=False)

        # Ring → Phone: 2F 10 2C <nonce:15>
        def is_nonce(v: bytes) -> bool:
            for f in parse_outer_frames(v):
                if f.opcode == 0x2f and f.sub_op == 0x2c and len(f.body) == 16:
                    return True
            return False

        _, value = await self._expect(is_nonce)
        nonce = next(
            f.body[1:16] for f in parse_outer_frames(value)
            if f.opcode == 0x2f and f.sub_op == 0x2c
        )

        # Phone → Ring: 2F 11 2D <proof:16>
        proof = compute_handshake_proof(self.auth_key, nonce)
        frame = b"\x2f\x11\x2d" + proof
        await self._write(frame, response=False)

        # Ring → Phone: 2F 02 2E <status:1>  (00 = success)
        def is_status(v: bytes) -> bool:
            for f in parse_outer_frames(v):
                if f.opcode == 0x2f and f.sub_op == 0x2e and len(f.body) == 2:
                    return True
            return False

        _, value = await self._expect(is_status)
        status = next(
            f.body[1] for f in parse_outer_frames(value)
            if f.opcode == 0x2f and f.sub_op == 0x2e
        )
        if status != 0:
            raise RuntimeError(f"handshake failed; status=0x{status:02x}")
        log.info("handshake OK")

    # ----- time-sync -----

    async def _time_sync_now(self) -> None:
        await self._write(_make_time_sync_frame(), response=False)
        # The reply is `13 05 <ack> <echo:3 LE> 00`. We don't strictly need to
        # validate it — the formula is one-way (phone tells the ring the time;
        # the ring acknowledges).

    # ----- control plane: parameter RPC + history fetch -----
    #
    # These let the driver actively change ring sensor configuration (toggle SpO2,
    # set DHR mode, etc.) and request missed records from a delta-sync cursor.

    # Documented parameter IDs (verified empirically; see truth-table § 8.2)
    PARAM_DHR             = 0x02   # Daytime Heart Rate; bytes 0/2 are mode/sub-mode
    PARAM_ACTIVITY_HR     = 0x03   # Activity HR enable; byte 0 toggle
    PARAM_SPO2            = 0x04   # SpO2 enable; byte 0 toggle
    PARAM_ACTIVITY_HR_AUX = 0x0B   # companion to 0x03 (read-only in observed traffic)
    PARAM_UNMAPPED_0D     = 0x0D
    PARAM_UNMAPPED_10     = 0x10

    async def read_param(self, param_id: int) -> None:
        """Fire `2F 02 20 <param>` to request the 4-byte param value. The ring
        replies with `2F 06 21 <param> <value:4>` which `stream()` will surface
        as a `_PARAM_READ_RESP` Record.
        """
        await self._write(bytes([0x2f, 0x02, 0x20, param_id]), response=False)

    async def write_param_byte0(self, param_id: int, value: int) -> None:
        """Fire `2F 03 22 <param> <value>` — sets BYTE 0 of the param."""
        await self._write(bytes([0x2f, 0x03, 0x22, param_id, value & 0xff]), response=False)

    async def write_param_byte2(self, param_id: int, value: int) -> None:
        """Fire `2F 03 26 <param> <value>` — sets BYTE 2 of the param."""
        await self._write(bytes([0x2f, 0x03, 0x26, param_id, value & 0xff]), response=False)

    # High-level convenience wrappers

    async def set_spo2(self, on: bool) -> None:
        """Enable or disable SpO2 sampling. Verified by toggle-RE (spec § 8.3)."""
        await self.write_param_byte0(self.PARAM_SPO2, 0x01 if on else 0x00)

    async def set_activity_hr(self, on: bool) -> None:
        """Toggle activity-heart-rate detection."""
        await self.write_param_byte0(self.PARAM_ACTIVITY_HR, 0x01 if on else 0x00)

    async def set_dhr_mode(self, mode: int, sub_mode: int = 0) -> None:
        """Set Daytime Heart Rate mode (byte 0) and sub-mode (byte 2).
        Observed: (mode=3, sub_mode=2) for an on-demand HR check; (mode=1, sub_mode=0) idle.

        Phone-verified pattern (sunday_morning_wk2.log +52.7s and +66.9s):
        a `paramRead(0x02)` precedes the writes — likely to read the current
        state and verify the change took. We mirror that for byte-perfect
        parity with the official app.
        """
        await self.read_param(self.PARAM_DHR)
        await self.write_param_byte0(self.PARAM_DHR, mode)
        if sub_mode is not None:
            await self.write_param_byte2(self.PARAM_DHR, sub_mode)

    async def request_hr_on_demand(self) -> None:
        """Fire the on-demand HR burst pattern: DHR mode=3 / sub-mode=2.
        Per spec § 8.2: this triggers a ~20 s HR sampling window, after which
        the ring returns to mode=1 / sub-mode=0 on its own.
        """
        await self.set_dhr_mode(mode=3, sub_mode=2)

    async def soft_reset(self) -> None:
        """Issue a soft reset to the ring: phone sends `0e 01 ff`, ring acks
        `0f 01 00`, and the ring reboots ~25-35 seconds later (emits
        `API_RING_START_IND` on reconnect).

        Verified across thursday.log: 3 reset commands → 3 ring boots, each
        with ack latency 19-181 ms and reboot delay 22-35 s.
        """
        await self._write(b"\x0e\x01\xff", response=False)

    async def request_events_since(
        self,
        ring_timestamp: int,
        max_events: int = 255,
        flags: int = 0xFFFFFFFF,
    ) -> None:
        """Phone → Ring: `10 09 <ring_timestamp:4 LE> <max_events:1> <flags:4 LE>`
        — 11 bytes total. Verified against `GetEvent.java` in the official app:

            REQUEST_TAG  = 0x10
            length       = 0x09
            bytes 2-5    = eventStartTimestamp (LE u32) — the ring's own
                           monotonic event-counter; ring streams every event
                           with `ringTimestamp > eventStartTimestamp`.
            byte 6       = maxEventsToGet, capped at 255. 0 acts as an
                           "advance my cursor here" ack with no expected data.
            bytes 7-10   = flags (LE u32). Phone always sends 0xFFFFFFFF
                           (= include all event types).

        After this write the ring streams matching events as raw inner-record
        notifications, then sends a `0x11` summary frame whose bytes 4-7 carry
        the new highest delivered timestamp. Use that to advance state for the
        next call.
        """
        ts = int(ring_timestamp) & 0xFFFFFFFF
        max_events = max(0, min(255, int(max_events)))
        flags = int(flags) & 0xFFFFFFFF
        await self._write(
            bytes([0x10, 0x09]) + ts.to_bytes(4, "little")
                + bytes([max_events]) + flags.to_bytes(4, "little"),
            response=False,
        )

    # Deprecated alias kept for one release so out-of-tree callers don't break.
    # Old call: request_history(sub_op, cursor, alt_pattern) — the (sub_op,
    # cursor) pair was a mis-decoded split of the same 32-bit ring_timestamp.
    async def request_history(self, sub_op: int = 0x00, cursor: int = 0,
                              alt_pattern: bool = False) -> None:
        ring_ts = (sub_op & 0xff) | ((cursor & 0xffffff) << 8)
        max_events = 0 if alt_pattern else 255
        await self.request_events_since(ring_ts, max_events=max_events)

    # ----- event subscribe -----

    async def _subscribe_events(self) -> None:
        # `16 01 02` = enable the event/record stream subscription. The ring
        # responds with `17 01 02`. Without this, per-category subscribes
        # are silently dropped.
        await self._write(b"\x16\x01\x02", response=False)
        # `18 03 <category:u8> <flags:u16-LE>` per category. The frame is 5
        # bytes total (the sub_op byte 0x03 = "3 bytes of payload follow").
        # An earlier driver version sent only 4 bytes (u8 flags) — the ring
        # treats those as malformed and silently ignores them, then drops
        # the connection ~60s later when no expected catch-up traffic flows.
        for category, flags in self.event_categories:
            frame = bytes([0x18, 0x03, category & 0xff,
                            flags & 0xff, (flags >> 8) & 0xff])
            await self._write(frame, response=False)
        # Send the post-handshake config push (verified byte-invariant against
        # sunday_evening.log capture). Ring acks with `2f 03 2a 04 00`.
        await self._write(bytes.fromhex("2f0b29043c19031e1800000000"), response=False)

    async def _fetch_with_ack(self, ts: int) -> None:
        """Send the phone's two-part fetch pattern:

            +0ms    GetEvent ts=T  max=255   ← drain (A trailing: ff*5)
            +100ms  GetEvent ts=T' max=0     ← ack   (B trailing: 00 ff*4)

        The ack tells the ring "I consumed up through T'" so its internal
        cursor advances; without it, repeated fetches re-deliver the same
        window. Phone always uses a slightly higher T' (= last delivered
        ring_time) for the ack.

        Architecture: `_consumer_loop` (started in `_connect`) is parsing
        notifications in real time and updating `sync_state` as records
        arrive. So during our 100ms wait here, sync_state's value advances
        to match what the ring is currently delivering — and the ack uses
        that fresh value, byte-perfect with the phone.
        """
        await self.request_events_since(ts, max_events=255)
        # Phone's observed inter-fetch gap is ~100ms. The consumer task
        # advances sync_state.last_ring_timestamp during this window from
        # the in-flight records; we then ack with the new value.
        await asyncio.sleep(0.10)
        ack_ts = (self.sync_state.last_ring_timestamp
                  if self.sync_state is not None else ts)
        if ack_ts < ts:
            ack_ts = ts  # never regress
        await self.request_events_since(ack_ts, max_events=0)

    async def _quick_refresh_sequence(self) -> None:
        """Pattern C: 5-write fast poll. Skips handshake + time-sync +
        param sweep on the assumption that the ring still considers us
        authenticated from a recent prior session.

        Verified against `sunday_morning_wk2.log` session #5 (5 writes,
        14 seconds wall-clock):

            +0.0s  0c 00              BatteryReq
            +0.4s  16 01 02           SubscribeEnable
            +13s   10 09 ... ff*5     GetEvent max=255
            +13s   10 09 ... 00 ff*4  ack max=0
            +14s   16 01 00           SubscribeDisable

        Caveats:
          - If the ring has rotated state / forgotten us, this will fail
            quietly (the ring just won't respond). Caller should fall back
            to a full Pattern A connect.
          - We don't know the empirical "still considered authenticated"
            window. The phone uses Pattern C across same-btsnoop sessions
            (i.e. within minutes); after longer gaps it falls back to A.
        """
        await self._write(b"\x0c\x00", response=False)        # BatteryReq
        self._last_setup_step = "qr_battery"
        await self._write(b"\x16\x01\x02", response=False)    # SubscribeEnable
        self._last_setup_step = "qr_subscribe_enabled"
        if not self.skip_catchup:
            await self._catchup()
            self._last_setup_step = "qr_catchup_sent"
        else:
            self._last_setup_step = "qr_catchup_skipped"
        log.info("quick-refresh sequence complete (5-write fast poll)")

    async def _catchup(self) -> None:
        """Autonomous catch-up: on (re)connect, ask the ring for every event
        with `ringTimestamp > self.sync_state.last_ring_timestamp`.

        Sends the phone's verified two-part `data_fetch + ack` pattern (the
        `max=0` ack is what makes the ring advance its cursor; without it
        repeated fetches re-deliver the same window).

        Bootstrap path for a fresh laptop:
          1. Capture a btsnoop while the official app reconnects to the ring.
          2. Run `tools/seed_cursors_from_btsnoop.py <btsnoop>` to extract the
             phone's most-recent eventStartTimestamp into the sync-state file.
          3. Future `live` connects fetch from there.

        With no saved state (`last_ring_timestamp == 0`), we still send the
        request — the ring will dump whatever's still in its circular buffer.
        """
        if self.sync_state is None:
            log.info("no sync_state attached — skipping history fetch; "
                     "live-push records still arrive via subscribe")
            return
        ts = self.sync_state.last_ring_timestamp
        await self._fetch_with_ack(ts)
        log.info("history-fetch: requested events since ring_timestamp=%d "
                 "(0x%08x), then ack-fetched", ts, ts)

    async def _periodic_flush_loop(self) -> None:
        """Wall-clock-driven `data_flush + GetEvent` cycle. Mimics the
        official app's mid-session pattern (verified against
        `hci_monday_evening_wk2.log`):

            t=2.7s   initial fetch (max=255)        → drains current backlog
            t=2.8s   ack fetch (max=0)              → advances cursor
            t=23.3s  data_flush (28 01 00)          ← we now do this too
            t=23.4s  fetch (max=255)                ← and this
            ...

        Without this loop the ring buffers events in flash and never
        delivers them on the live stream. With it, a long-running session
        reaches data parity with the phone (including `EHR_TRACE_EVENT`
        records that don't appear in the initial catchup).

        Wall-clock-driven, NOT ring_time-driven: ring_time pauses when
        nothing is happening, so it's a useless trigger. The phone's
        observed cadence is ~20 s.
        """
        if self.flush_interval_s <= 0:
            log.info("periodic flush loop disabled (flush_interval_s=0)")
            return
        log.info("periodic flush loop: every %.1fs", self.flush_interval_s)
        # Phone toggles subscribe disable→enable mid-session (~16-20s gap)
        # to reset the ring's stream-state machine. Empirically observed to
        # coax additional buffered events out. We fire this every Nth flush.
        SUB_TOGGLE_EVERY_N_FLUSHES = 3   # at default 20s flush interval = ~60s
        flush_count = 0
        try:
            while True:
                await asyncio.sleep(self.flush_interval_s)
                if self.sync_state is None:
                    continue
                try:
                    flush_count += 1
                    # Subscribe toggle every Nth flush (matches phone's
                    # mid-session refresh pattern from sunday_morning_wk2.log).
                    if flush_count % SUB_TOGGLE_EVERY_N_FLUSHES == 0:
                        log.info("periodic subscribe-toggle: 16 01 00 → wait → 16 01 02")
                        await self._write(b"\x16\x01\x00", response=False)
                        # Phone's observed Disable→Enable gap: ~16s. We use
                        # 2.5s to keep the loop responsive without breaking
                        # too far from observed behavior.
                        await asyncio.sleep(2.5)
                        await self._write(b"\x16\x01\x02", response=False)
                    # Per phone capture: data_flush precedes the fetch by ~100ms
                    await self._write(b"\x28\x01\x00", response=False)
                    ts = self.sync_state.last_ring_timestamp
                    await self._fetch_with_ack(ts)
                    log.info("periodic flush #%d: 28 01 00 + fetch+ack since "
                             "ring_timestamp=%d", flush_count, ts)
                except Exception as e:
                    log.warning("periodic flush failed: %s", e)
        except asyncio.CancelledError:
            log.info("periodic flush loop stopped")
            raise

    # ----- background consumer + user-facing stream -----

    async def _consumer_loop(self) -> None:
        """Single owner of `self._notify_q`. Parses every incoming
        notification into Records (or synthetic envelope events), updates
        `sync_state` in real time, and forwards Records to `self._record_q`
        for the user-facing `stream()` to consume.

        Real-time `sync_state` advance is the whole point — without this,
        `_fetch_with_ack` would always send the ack with a stale ts and
        we'd never byte-match the phone.
        """
        cva_ppg_dec = CvaPpgDecoder()
        cva_ppg_last_t: int | None = None
        try:
            while True:
                ts, value = await self._notify_q.get()
                utc_ms = int(ts * 1000)

                # Disconnect sentinel from on_disconnect callback — emit a
                # synthetic _DISCONNECT and exit. The user's stream() will
                # see it and (if reconnect enabled) trigger _connect again.
                if value == b"\x00__DISCONNECT__":
                    self._save_sync_state_safe()
                    # Fail any pending _expect waiters so setup doesn't hang
                    for _, fut in list(self._expect_waiters):
                        if not fut.done():
                            fut.set_exception(
                                ConnectionError("link dropped during expect"))
                    self._expect_waiters.clear()
                    self._record_q.put_nowait(Record(
                        t=int(time.time() * 1000), rt=None, ctr=None, sess=None,
                        tag="_DISCONNECT", type="_DISCONNECT",
                        data={"reason": "ConnectionError",
                              "detail": "link dropped (disconnect callback)"},
                    ))
                    return

                # Check _expect waiters BEFORE forwarding. If any predicate
                # matches, fulfill that future and SKIP forwarding (mirrors
                # the old "drop on floor for matched _expect" behavior).
                # Use a copy of the list because we mutate it.
                matched = False
                for entry in list(self._expect_waiters):
                    pred, fut = entry
                    if fut.done():
                        try: self._expect_waiters.remove(entry)
                        except ValueError: pass
                        continue
                    try:
                        if pred(value):
                            fut.set_result((ts, value))
                            try: self._expect_waiters.remove(entry)
                            except ValueError: pass
                            matched = True
                            break
                    except Exception as pe:
                        log.warning("_expect predicate raised: %s", pe)
                if matched:
                    continue   # don't forward this notification to _record_q

                if looks_like_outer_frame(value):
                    for f in parse_outer_frames(value):
                        for rec in _outer_to_records(f, utc_ms):
                            if rec.type == "_RING_RESET_ACK":
                                cva_ppg_dec.reset()
                            # NOTE: 0x11 `_HISTORY_FETCH_RESP` exposes
                            # `last_ring_timestamp` for observability but
                            # we do NOT advance sync_state from it (it's a
                            # per-batch value, not a cursor). Real cursor
                            # advance comes from inner-record ring_times.
                            self._record_q.put_nowait(rec)
                else:
                    for r in parse_inner_records(value):
                        # 0x33 doesn't use the standard ringTimestamp
                        # framing — its (ctr,sess) bytes are data, not a
                        # timestamp. Reconstruct the full body and decode
                        # directly. See `decode_unknown_33_body` docstring.
                        if r.type_byte == 0x33:
                            from .decoders import decode_unknown_33_body
                            import struct as _struct
                            try:
                                _body = (_struct.pack('<HH', r.counter, r.session)
                                         + r.payload)
                                data = decode_unknown_33_body(_body)
                            except (ValueError, _struct.error) as _e:
                                data = {"_decode_error": str(_e),
                                        "hex": r.payload.hex(),
                                        "len": len(r.payload)}
                        else:
                            data = decode(r.type_byte, r.payload)
                        # Update the time anchor from API_TIME_SYNC_IND (0x42).
                        # Mirrors RingTimeResolver::handle_api_time_sync_ind:
                        # ring_time = TLV ringTime, utc_ms = unix_s * 1000,
                        # factor_flag = 1 if token == 0xfd (burst mode) else 0.
                        if (r.type_byte == 0x42
                                and self.sync_state is not None
                                and 0 < r.ring_time < 0x80000000):
                            unix_s = data.get("ring_unix_time_approx_s")
                            if isinstance(unix_s, int) and unix_s > 0:
                                token = data.get("token")
                                self.sync_state.update_anchor(
                                    ring_time=r.ring_time,
                                    utc_ms=unix_s * 1000,
                                    factor_flag=1 if token == 0xfd else 0,
                                )
                        # Mirror RingTimeResolver::handle_api_ring_start_ind:
                        # on 0x41, if new rt is BELOW the current anchor's rt
                        # the ring restarted (session reset) → invalidate the
                        # stale anchor; next 0x42 will rebuild it.
                        elif (r.type_byte == 0x41
                                and self.sync_state is not None
                                and self.sync_state.anchor_ring_time != 0
                                and r.ring_time < self.sync_state.anchor_ring_time):
                            self.sync_state.invalidate_anchor()
                        if r.type_byte == 0x81:
                            if cva_ppg_last_t is not None and (utc_ms - cva_ppg_last_t) > 60_000:
                                cva_ppg_dec.reset()
                            samples = cva_ppg_dec.feed(r.payload)
                            data = {
                                "samples": samples,
                                "samples_in_record": len(samples),
                                "session_samples_total": cva_ppg_dec.samples_total,
                                "session_absolutes": cva_ppg_dec.absolutes_total,
                                "session_deltas": cva_ppg_dec.deltas_total,
                            }
                            cva_ppg_last_t = utc_ms
                        # Advance the persistent sync cursor from each
                        # record's ring_time. Skip TLV-misparse artifacts
                        # (unknown type-byte OR ring_time ≥ 2**31).
                        rt = r.ring_time
                        if (self.sync_state is not None
                                and 0 < rt < 0x80000000
                                and not is_structurally_unknown(r.type_byte)
                                and self.sync_state.update(rt)):
                            self._sync_updates_since_save += 1
                            if (self._sync_updates_since_save
                                    >= self._sync_save_every):
                                self._save_sync_state_safe()
                        t_event_ms = (self.sync_state.to_utc_ms(rt)
                                      if (self.sync_state is not None
                                          and 0 < rt < 0x80000000
                                          and not is_structurally_unknown(r.type_byte))
                                      else None)
                        self._record_q.put_nowait(Record(
                            t=utc_ms,
                            rt=rt,
                            ctr=r.counter,
                            sess=r.session,
                            tag=f"0x{r.type_byte:02x}",
                            type=canonical_type(r.type_byte),
                            data=data,
                            t_event_ms=t_event_ms,
                        ))
        except asyncio.CancelledError:
            self._save_sync_state_safe()
            raise
        except Exception as e:
            log.exception("consumer task crashed: %s", e)
            self._save_sync_state_safe()
            # Surface as a synthetic _DISCONNECT so user's stream() unblocks
            self._record_q.put_nowait(Record(
                t=int(time.time() * 1000), rt=None, ctr=None, sess=None,
                tag="_DISCONNECT", type="_DISCONNECT",
                data={"reason": type(e).__name__, "detail": str(e)},
            ))

    async def stream(self) -> AsyncIterator[Record]:
        """Yield decoded Records as the ring sends notifications.

        Reads from `self._record_q` which is fed by `_consumer_loop`.
        Auto-reconnects on `_DISCONNECT` if `self.reconnect=True`.
        """
        while True:
            try:
                rec = await self._record_q.get()
                yield rec
                if rec.type == "_DISCONNECT":
                    if not self.reconnect:
                        return
                    log.warning("disconnect; reconnecting…")
                    await asyncio.sleep(1.0)
                    try:
                        await self._connect()
                    except Exception as e2:
                        log.error("reconnect failed: %s", e2)
                        await asyncio.sleep(5.0)
            except (asyncio.CancelledError, KeyboardInterrupt):
                self._save_sync_state_safe()
                raise


# ----- outer-frame → synthetic Record (live mode) ---------------------------


def _outer_to_records(f: OuterFrame, utc_ms: int) -> list[Record]:
    """Translate a live outer frame into zero-or-one synthetic Records.
    Mirrors `replay._outer_to_record` but ring-direction-only since the live
    client only listens to notify-char traffic.
    """
    op = f.opcode

    if op == 0x0d and len(f.raw) == 8:
        return [Record(
            t=utc_ms, rt=None, ctr=None, sess=None,
            tag="_BATTERY", type="_BATTERY",
            data={"voltage_mv": f.raw[6] | (f.raw[7] << 8),
                  "state_bytes": list(f.raw[2:6])},
        )]
    if op == 0x13 and len(f.raw) == 7:
        return [Record(
            t=utc_ms, rt=None, ctr=None, sess=None,
            tag="_TIME_SYNC_REPLY", type="_TIME_SYNC_REPLY",
            data={"ack_code": f.raw[2],
                  "time_echo": f.raw[3] | (f.raw[4] << 8) | (f.raw[5] << 16)},
        )]
    if op == 0x1f and len(f.raw) == 6:
        return [Record(
            t=utc_ms, rt=None, ctr=None, sess=None,
            tag="_STATE_PULSE", type="_STATE_PULSE",
            data={"sub_op": f.raw[2], "data": list(f.raw[3:6])},
        )]
    # Ring → Phone history-fetch summary. Verified against `GetEvent.java`'s
    # `parseResponse` in the official app:
    #   byte 0      : 0x11 (RESPONSE_TAG)
    #   byte 1      : length (typically 8 — body bytes that follow)
    #   byte 2      : status (0x00 = empty, 0xFF = data delivered)
    #   byte 3      : sub-status
    #   bytes 4-7   : last_ring_timestamp (LE u32) — the highest
    #                 ringTimestamp the ring delivered in this batch
    #   bytes 8+    : padding/extras (length-dependent)
    # `_catchup()` and `stream()` use the timestamp to advance sync_state.
    if op == 0x11 and len(f.body) >= 6:
        status = f.body[0]
        sub_status = f.body[1]
        ts = (f.body[2] | (f.body[3] << 8)
              | (f.body[4] << 16) | (f.body[5] << 24))
        return [Record(
            t=utc_ms, rt=None, ctr=None, sess=None,
            tag="_HISTORY_FETCH_RESP", type="_HISTORY_FETCH_RESP",
            data={"status": status, "sub_status": sub_status,
                  "last_ring_timestamp": ts},
        )]
    return []


# ----- Sync wrapper (for non-asyncio consumers) -----------------------------


def stream_sync(
    mac: str,
    *,
    auth_key: bytes | None = None,
    realm_path: str | os.PathLike | None = None,
    **kwargs: Any,
):
    """Synchronous generator wrapper around the async `OuraRingClient.stream`.

    Each `next()` call drives the asyncio loop one record forward.
    """
    async def _gen():
        async with OuraRingClient(
            mac=mac, auth_key=auth_key, realm_path=realm_path, **kwargs
        ) as client:
            async for rec in client.stream():
                yield rec

    loop = asyncio.new_event_loop()
    agen = _gen().__aiter__()
    try:
        while True:
            yield loop.run_until_complete(agen.__anext__())
    except StopAsyncIteration:
        return
    finally:
        loop.close()
