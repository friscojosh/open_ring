"""CLI: compute summary statistics from a btsnoop log or driver JSONL.

Usage:
    python -m consumers.cli INPUT [--module hr|ble|battery|sleep|coverage|activity|temperature|all]
                               [--json | --pretty]

INPUT may be either a btsnoop `.log` (decoded via `driver.replay`)
or a `.jsonl` produced by the driver (read directly).

Examples:
    python -m consumers.cli capture.log
    python -m consumers.cli session.jsonl --module hr --json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path

from . import hr, ble, battery, sleep, coverage, activity, temperature, open_records


_MODULES = {
    "hr": hr,
    "ble": ble,
    "battery": battery,
    "sleep": sleep,
    "coverage": coverage,
    "activity": activity,
    "temperature": temperature,
}


def compute_all(records: Iterable) -> dict[str, dict]:
    """Run every module in a single pass over the records (which we materialize
    as a list since each module wants its own iteration)."""
    recs = list(records)
    return {name: mod.compute(recs) for name, mod in _MODULES.items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="oura-summary",
                                 description="Compute summary stats from a btsnoop log or driver JSONL.")
    ap.add_argument("input", help="Path to a btsnoop .log OR a driver-output .jsonl")
    ap.add_argument("--module", choices=list(_MODULES.keys()) + ["all"],
                    default="all",
                    help="Which summary module to run (default: all)")
    ap.add_argument("--json", action="store_true",
                    help="Compact JSON output (default: indented)")
    args = ap.parse_args(argv)

    src = Path(args.input)
    if not src.exists():
        print(f"error: input not found: {src}", file=sys.stderr)
        return 2

    records = list(open_records(src))
    if args.module == "all":
        result = {"_input": str(src), "_n_records": len(records),
                  **compute_all(records)}
    else:
        mod = _MODULES[args.module]
        result = {"_input": str(src), "_n_records": len(records),
                  args.module: mod.compute(records)}

    if args.json:
        print(json.dumps(result, separators=(",", ":"), default=str))
    else:
        print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
