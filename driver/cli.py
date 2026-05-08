"""CLI entry point.

Usage:
    python -m oura_ring.cli replay <btsnoop>
    python -m oura_ring.cli live --mac <MAC> --realm <path/to/assa-store.realm>
    python -m oura_ring.cli live --mac <MAC> --auth-key <hex>
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from .replay import main_replay


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="oura-stream")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Log INFO/DEBUG messages from the driver to stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("replay", help="Decode an offline btsnoop capture as JSONL.")
    rp.add_argument("btsnoop", help="Path to btsnoop_hci.log")
    rp.add_argument("--cmd-handle", type=lambda x: int(x, 0), default=0x0015)
    rp.add_argument("--notify-handle", type=lambda x: int(x, 0), default=0x0012)

    ds = sub.add_parser("discover", help="Scan for an Oura ring by service UUID and print its current MAC.")
    ds.add_argument("--adapter", default=None,
                    help="HCI adapter name (e.g. hci0); default = system default")
    ds.add_argument("--timeout", type=float, default=10.0,
                    help="Scan duration in seconds (default: 10)")

    lv = sub.add_parser("live", help="Stream from a live ring via BLE (requires bleak).")
    lv.add_argument("--mac", default=None,
                    help="Ring BLE MAC, e.g. A0:38:F8:A4:09:C9. If omitted, "
                         "scans by service UUID (robust against RPA rotation).")
    lv.add_argument("--adapter", default=None,
                    help="HCI adapter name (e.g. hci0); default = system default")
    lv.add_argument("--discover-timeout", type=float, default=10.0,
                    help="Seconds to scan for the ring when --mac is unset (default: 10)")
    src = lv.add_mutually_exclusive_group(required=True)
    src.add_argument("--auth-key", help="16-byte AES-128 auth_key as hex (32 chars)")
    src.add_argument("--realm", help="Path to assa-store.realm to extract auth_key from")
    lv.add_argument("--no-reconnect", action="store_true",
                    help="Stop on first disconnect instead of auto-reconnecting")
    lv.add_argument("--sync-state-file", "--cursor-file",
                    dest="sync_state_file",
                    help="Path to the JSON file storing the single delta-sync "
                         "ringTimestamp. Each reconnect resumes from there. "
                         "Default: ~/.local/share/oura_ring/cursors.json. "
                         "Use --no-sync-state-file to disable persistence. "
                         "(--cursor-file is the deprecated alias.)")
    lv.add_argument("--no-sync-state-file", "--no-cursor-file",
                    dest="no_sync_state_file", action="store_true",
                    help="Disable sync-state persistence; every reconnect "
                         "starts from ring_timestamp=0 (full dump if the "
                         "ring still has it).")
    lv.add_argument("--pair", action="store_true",
                    help="Initiate BlueZ-level pairing on connect. The ring may "
                         "drop unpaired centrals after ~2-4s (supervision-timeout "
                         "signature). If you see that, try this flag — or run "
                         "`bluetoothctl pair <MAC>` once before connecting.")
    lv.add_argument("--mtu", type=int, default=247,
                    help="ATT MTU to use for the connection (default 247 — what "
                         "Oura streams). bleak warns 'Using default MTU value' "
                         "when it can't query the negotiated MTU; this flag "
                         "sets bleak's _mtu_size override so it accepts full-"
                         "size notifications.")
    # NOTE: --max-catchup-per-session was removed. The corrected protocol
    # uses a single 32-bit ring_timestamp cursor, so catchup is always one
    # GetEvent request per connect (ring streams every event newer than the
    # saved timestamp). Pass --skip-catchup to suppress it entirely.
    lv.add_argument("--skip-catchup", action="store_true",
                    help="Don't send the catchup GetEvent on connect. Live "
                         "subscribed records still flow; only buffered "
                         "history is suppressed. Useful for one-shot reads.")
    lv.add_argument("--flush-interval", type=float, default=20.0,
                    help="Wall-clock seconds between mid-session "
                         "`data_flush + GetEvent` cycles (default: %(default)s, "
                         "matches the official app's observed cadence). 0 "
                         "disables the loop — initial catchup only. "
                         "Implicitly disabled when --skip-catchup is set.")
    lv.add_argument("--quick-refresh", action="store_true",
                    help="Use Pattern C (5-write fast poll) instead of the "
                         "full 25-write handshake on connect. ONLY safe if "
                         "you just disconnected from a full-setup session "
                         "and the ring still considers you authenticated. "
                         "Will fail silently if not. Useful for back-to-back "
                         "polls in the same session group.")

    args = ap.parse_args(argv)
    if args.verbose:
        import logging
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                            stream=sys.stderr)
    if args.cmd == "replay":
        return main_replay([args.btsnoop, "--cmd-handle", str(args.cmd_handle),
                            "--notify-handle", str(args.notify_handle)])
    if args.cmd == "live":
        return _run_live(args)
    if args.cmd == "discover":
        return _run_discover(args)
    return 1


def _run_discover(args) -> int:
    from .transport import discover_ring

    async def _go():
        device = await discover_ring(timeout=args.timeout, adapter=args.adapter)
        if device:
            print(device.address)
            return 0
        print("no ring found", file=sys.stderr)
        return 1

    try:
        return asyncio.run(_go())
    except KeyboardInterrupt:
        return 130


def _run_live(args) -> int:
    from .persistence import SyncState
    from .transport import OuraRingClient

    # Live mode is interactive / debug-ish by default — surface INFO logs so
    # users see what's happening at each connect step. Quiet via -q if needed.
    import logging
    if not args.verbose:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s.%(msecs)03d %(levelname)s: %(message)s",
                            datefmt="%H:%M:%S",
                            stream=sys.stderr)

    auth_key = bytes.fromhex(args.auth_key) if args.auth_key else None
    realm_path = args.realm

    sync_state: SyncState | None = None
    if not args.no_sync_state_file:
        sync_state = (SyncState(args.sync_state_file)
                      if args.sync_state_file else SyncState())

    async def _go():
        async with OuraRingClient(
            mac=args.mac, auth_key=auth_key, realm_path=realm_path,
            reconnect=not args.no_reconnect,
            sync_state=sync_state,
            adapter=args.adapter,
            discover_timeout=args.discover_timeout,
            pair=args.pair,
            mtu=args.mtu,
            skip_catchup=args.skip_catchup,
            flush_interval_s=args.flush_interval,
            quick_refresh=args.quick_refresh,
        ) as client:
            sys.stderr.write(f"connected to {client.mac}\n")
            async for rec in client.stream():
                sys.stdout.write(rec.to_json())
                sys.stdout.write("\n")
                sys.stdout.flush()

    try:
        asyncio.run(_go())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
