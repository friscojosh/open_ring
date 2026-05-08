"""5-panel biometric plot from a `Record` stream.

Renders heart rate, blood oxygen, temperature, motion magnitude, and
HRV / breath rate against ring-emit wall-clock time.

The x-axis must be ring-emit wall-clock, not BLE-receive time, because
catchup records arrive in bursts that all share the same `r.t` even
though they cover hours of physiological time. The renderer picks the
best available anchor source automatically:

1. **`t_event_ms` (preferred)** — every Record produced by the driver
   v0.4+ carries this field, populated from the `RingTimeResolver`
   replica (`driver/PROTOCOL.md` §7).
2. **Linear fit on `API_TIME_SYNC_IND` anchors** — for older Records
   without `t_event_ms`. Each `0x42` provides `(rt, unix_s)`; we fit
   `unix_s = slope·rt + intercept` (slope ≈ 0.1 = 10 ticks/sec).
3. **Single-point fallback** — anchor on the latest `(rt, t)` pair at
   the empirical 10 ticks/sec rate.

`matplotlib` is required to render. Imported lazily so consumers using
the rest of `consumers` (which is stdlib-only) aren't forced to
install it.

CLI:
    python -m consumers.plot INPUT [--out PATH]

INPUT may be either a btsnoop `.log` (decoded via `driver.replay`)
or a `.jsonl` file produced by the driver (read directly).
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import open_records


def _matplotlib():
    """Lazy import + actionable error if matplotlib isn't installed."""
    try:
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        return mdates, plt
    except ImportError as e:
        sys.stderr.write(
            "error: oura_summary.plot requires matplotlib\n"
            "  pip install matplotlib\n"
            f"  (underlying error: {e})\n"
        )
        sys.exit(1)


def _pick_time_source(records: list) -> Callable:
    """Return a function `record → datetime | None` that maps a Record
    to its ring-emit wall-clock, picking the best available anchor."""
    n_total = len(records)
    n_with_event_ms = sum(1 for r in records if r.t_event_ms is not None)
    if n_total == 0:
        sys.exit("error: no records to plot")

    # Path 1: per-record t_event_ms from the driver (preferred)
    if n_with_event_ms / n_total > 0.5:
        print(f"time source: per-record t_event_ms "
              f"({n_with_event_ms}/{n_total} = {100*n_with_event_ms/n_total:.1f}%)",
              file=sys.stderr)
        return lambda r: (datetime.fromtimestamp(r.t_event_ms / 1000.0)
                          if r.t_event_ms is not None else None)

    # Path 2: linear fit on 0x42 (API_TIME_SYNC_IND) anchors
    anchors = [(r.rt, r.data["ring_unix_time_approx_s"])
               for r in records
               if r.type == "API_TIME_SYNC_IND"
               and r.rt is not None
               and isinstance(r.data, dict)
               and "ring_unix_time_approx_s" in r.data]

    if len(anchors) >= 2:
        n = len(anchors)
        sx = sum(a[0] for a in anchors); sy = sum(a[1] for a in anchors)
        sxx = sum(a[0]**2 for a in anchors); sxy = sum(a[0]*a[1] for a in anchors)
        slope = (n*sxy - sx*sy) / (n*sxx - sx*sx)
        intercept = (sy - slope*sx) / n
        print(f"time source: rt→unix linear fit ({n} 0x42 anchors)\n"
              f"  unix_s = {slope:.6g}·rt + {intercept:.6g}  "
              f"({1/slope:.3f} rt-ticks per second)", file=sys.stderr)
        return lambda r: (datetime.fromtimestamp(slope*r.rt + intercept)
                          if r.rt is not None else None)

    # Path 3: anchor on latest (rt, t) at the empirical 10 ticks/sec rate
    latest_rt = -1
    latest_t_ms = None
    for r in records:
        if r.rt is not None and r.rt > latest_rt and r.t is not None:
            latest_rt = r.rt
            latest_t_ms = r.t
    if latest_t_ms is None:
        sys.exit("error: no time anchors found "
                 "(no t_event_ms, no 0x42 records, no records with both rt and t)")
    slope = 0.0999  # 100 ms/tick (PROTOCOL.md §7)
    intercept = latest_t_ms / 1000.0 - slope * latest_rt
    print(f"time source: anchored on latest (rt={latest_rt}, t={latest_t_ms} ms) "
          f"at 10 ticks/sec", file=sys.stderr)
    return lambda r: (datetime.fromtimestamp(slope*r.rt + intercept)
                      if r.rt is not None else None)


def _extract_series(records: list, rec_to_dt: Callable) -> dict[str, list]:
    """One pass over the records: extract every series we plot. Each
    entry is a tuple of parallel lists (timestamp + values)."""
    series: dict[str, list[list[Any]]] = {
        "hr": [[], []],                 # sleep IBI → bpm
        "hr_wake": [[], []],            # green-IBI quality → bpm
        "spo2": [[], []],
        "temp": [[], [], [], []],       # ts, t1, t2, t3
        "sleep_temp": [[], []],
        "motion": [[], []],             # ts, |acm|
        "hrv": [[], [], []],            # ts, hr, rmssd
        "breath": [[], []],
        "sleep_hr": [[], []],
    }
    for r in records:
        ts = rec_to_dt(r)
        if ts is None:
            continue
        d = r.data or {}
        typ = r.type

        if typ == "API_IBI_AND_AMPLITUDE_EVENT":
            for ibi in d.get("ibi_ms", []):
                if 300 <= ibi <= 2000:                  # 30–200 BPM sanity
                    series["hr"][0].append(ts)
                    series["hr"][1].append(60000.0 / ibi)
        elif typ == "API_GREEN_IBI_QUALITY_EVENT":
            for s in d.get("samples", []):
                ibi = s.get("value_11bit")
                if ibi and 300 <= ibi <= 2000 and 1 <= s.get("quality_a", 0) <= 2:
                    series["hr_wake"][0].append(ts)
                    series["hr_wake"][1].append(60000.0 / ibi)
        elif typ == "API_SPO2_EVENT":
            for v in d.get("spo2_percent", []):
                if 70 <= v <= 100:
                    series["spo2"][0].append(ts); series["spo2"][1].append(v)
        elif typ == "API_TEMP_EVENT":
            t1, t2, t3 = d.get("temp1_c"), d.get("temp2_c"), d.get("temp3_c")
            if t1 is not None:
                series["temp"][0].append(ts)
                series["temp"][1].append(t1)
                series["temp"][2].append(t2)
                series["temp"][3].append(t3)
        elif typ == "API_SLEEP_TEMP_EVENT":
            for v in d.get("temps_c", []):
                series["sleep_temp"][0].append(ts)
                series["sleep_temp"][1].append(v)
        elif typ == "API_MOTION_EVENT":
            x = d.get("acm_x", 0); y = d.get("acm_y", 0); z = d.get("acm_z", 0)
            series["motion"][0].append(ts)
            series["motion"][1].append((x*x + y*y + z*z) ** 0.5)
        elif typ == "API_HRV_EVENT":
            for s in d.get("samples_5min", []):
                hr_v = s.get("hr_bpm"); rm_v = s.get("rmssd_ms")
                if hr_v:
                    series["hrv"][0].append(ts)
                    series["hrv"][1].append(hr_v)
                    series["hrv"][2].append(rm_v)
        elif typ == "API_SLEEP_PERIOD_INFO_2":
            b = d.get("breath")
            if b is not None and 5 < b < 40:
                series["breath"][0].append(ts); series["breath"][1].append(b)
            ahr = d.get("average_hr")
            if ahr and 30 <= ahr <= 200:
                series["sleep_hr"][0].append(ts); series["sleep_hr"][1].append(ahr)
    return series


def render(records: Iterable, *, title: str | None = None,
           out: str | Path | None = None):
    """Render a 5-panel biometric figure from a Record iterable.

    Returns the matplotlib `Figure`. If `out` is given, also saves to
    PNG. `matplotlib` is imported lazily.
    """
    mdates, plt = _matplotlib()

    recs = list(records)
    rec_to_dt = _pick_time_source(recs)
    series = _extract_series(recs, rec_to_dt)

    print(f"hr (sleep ibi):     {len(series['hr'][1])}", file=sys.stderr)
    print(f"hr (wake green ibi):{len(series['hr_wake'][1])}", file=sys.stderr)
    print(f"hr (sleep avg):     {len(series['sleep_hr'][1])}", file=sys.stderr)
    print(f"hrv 5-min samples:  {len(series['hrv'][2])}", file=sys.stderr)
    print(f"spo2 samples:       {len(series['spo2'][1])}", file=sys.stderr)
    print(f"temp records:       {len(series['temp'][0])}", file=sys.stderr)
    print(f"sleep-temp samples: {len(series['sleep_temp'][1])}", file=sys.stderr)
    print(f"motion samples:     {len(series['motion'][1])}", file=sys.stderr)
    print(f"breath samples:     {len(series['breath'][1])}", file=sys.stderr)

    fig, axes = plt.subplots(5, 1, figsize=(13, 15), sharex=True)

    ax = axes[0]
    ax.scatter(*series["hr"], s=3, alpha=0.25, color="crimson", label="sleep IBI")
    ax.scatter(*series["hr_wake"], s=3, alpha=0.25, color="darkorange",
               label="wake green-IBI")
    if series["sleep_hr"][0]:
        ax.plot(*series["sleep_hr"], ".", ms=5, color="black", label="sleep avg HR")
    if series["hrv"][0]:
        ax.plot(series["hrv"][0], series["hrv"][1], ".", ms=5, color="darkblue",
                label="HRV-event HR")
    ax.set_ylabel("HR (bpm)")
    ax.set_title("Heart rate")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)

    ax = axes[1]
    ax.scatter(*series["spo2"], s=4, alpha=0.4, color="steelblue")
    ax.set_ylabel("SpO2 (%)")
    ax.set_title("Blood oxygen")
    ax.grid(True, alpha=0.3)

    ax = axes[2]
    if series["temp"][0]:
        ts_temp, t1s, t2s, t3s = series["temp"]
        ax.plot(ts_temp, t1s, ".", ms=2, label="temp1", alpha=0.5)
        ax.plot(ts_temp, t2s, ".", ms=2, label="temp2", alpha=0.5)
        ax.plot(ts_temp, t3s, ".", ms=2, label="temp3", alpha=0.5)
    if series["sleep_temp"][0]:
        ax.plot(*series["sleep_temp"], ".", ms=2, label="sleep skin",
                alpha=0.6, color="orange")
    ax.set_ylabel("°C")
    ax.set_title("Temperature")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)

    ax = axes[3]
    ax.scatter(*series["motion"], s=2, alpha=0.3, color="darkgreen")
    ax.set_ylabel("|acm|")
    ax.set_title("Motion magnitude")
    ax.grid(True, alpha=0.3)

    ax = axes[4]
    ax.scatter(series["hrv"][0], series["hrv"][2], s=10, alpha=0.7, color="purple",
               label="RMSSD (ms)")
    ax.set_ylabel("RMSSD (ms)", color="purple")
    ax.set_title("HRV / breath rate")
    ax.grid(True, alpha=0.3)
    if series["breath"][0]:
        ax2 = ax.twinx()
        ax2.scatter(*series["breath"], s=4, alpha=0.5, color="teal", label="breath rpm")
        ax2.set_ylabel("breath (rpm)", color="teal")

    all_ts = (series["hr"][0] + series["hr_wake"][0] + series["spo2"][0]
              + series["temp"][0] + series["sleep_temp"][0] + series["motion"][0]
              + series["hrv"][0] + series["breath"][0] + series["sleep_hr"][0])
    if not all_ts:
        plt.close(fig)
        sys.exit("error: no plottable records found "
                 "(no IBI/SpO2/temp/motion/HRV/sleep events)")
    t_min, t_max = min(all_ts), max(all_ts)
    span_h = (t_max - t_min).total_seconds() / 3600
    fmt = "%H:%M" if t_min.date() == t_max.date() else "%m-%d %H:%M"
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter(fmt))
    axes[-1].set_xlim(t_min, t_max)
    axes[-1].set_xlabel(
        f"time  ({t_min:%Y-%m-%d %H:%M} → {t_max:%Y-%m-%d %H:%M},  {span_h:.1f} h)"
    )
    if title:
        fig.suptitle(f"Ring stats — {title}", y=1.0)
    fig.autofmt_xdate()
    fig.tight_layout()

    if out is not None:
        fig.savefig(out, dpi=120)
        print(f"wrote {out}", file=sys.stderr)
    return fig


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="oura-summary-plot",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("input",
                    help="Path to a btsnoop .log OR a driver-output .jsonl file")
    ap.add_argument("--out", default=None,
                    help="Output PNG path (default: <input>.png)")
    args = ap.parse_args(argv)

    src = Path(args.input)
    if not src.exists():
        print(f"error: {src} not found", file=sys.stderr)
        return 1
    out = Path(args.out) if args.out else src.with_suffix(".png")

    render(open_records(src), title=src.stem, out=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
