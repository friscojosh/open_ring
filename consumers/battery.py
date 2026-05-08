"""Battery / charging summary statistics.

Inputs:
    - `_BATTERY` synthetic events — `voltage_mv` from 0x0d battery responses
    - `0x61/0x14` DebugDataFuelGaugeStatistics — `battery_percentage`,
      `average_battery_voltage_mv`, `coulomb_counter`, `remaining_capacity`
    - `0x61/0x24` DebugDataBatteryLevelChanged — `battery_percentage`, voltage,
      reason byte (charging/discharging transition)
    - StateChange transitions to `STATE_CHARGING_PHASE` (= 8)

Outputs:
    - voltage range over capture (min / max)
    - battery percentage range
    - drain rate (mV/hour) — linear regression over voltage samples
    - charge cycles (count of charging-state transitions)
    - charging-time / discharging-time accumulator
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from . import event_time


def compute(records: Iterable) -> dict[str, Any]:
    # All voltage/percentage/state samples use event_time (ring-emit time)
    # so drain-rate regression and state-time accumulation reflect what
    # happened on the ring, not the burst pattern of catchup uploads.
    # `_BATTERY` is synthetic (no rt → event_time falls back to r.t).
    voltage_samples: list[tuple[int, int]] = []  # (event_ms, mv)
    pct_samples: list[tuple[int, float]] = []    # (event_ms, percent)
    coulomb_samples: list[tuple[int, int]] = []
    state_intervals: list[tuple[int, int]] = []  # (event_ms, state)
    charge_transitions = 0
    last_state = None

    for r in records:
        et = event_time(r)
        if r.type == "_BATTERY":
            mv = r.data.get("voltage_mv")
            if mv:
                voltage_samples.append((et, mv))
        elif r.tag == "0x61":
            d = r.data
            dd = d.get("_dd")
            if dd == "DebugDataFuelGaugeStatistics":
                voltage_samples.append((et, d["average_battery_voltage_mv"]))
                pct_samples.append((et, d["battery_percentage"]))
                coulomb_samples.append((et, d["coulomb_counter"]))
            elif dd == "DebugDataBatteryLevelChanged":
                voltage_samples.append((et, d["battery_voltage_mv"]))
                pct_samples.append((et, float(d["battery_percentage"])))
        elif r.type in ("API_STATE_CHANGE_IND", "API_WEAR_EVENT"):
            state = r.data.get("state")
            if state is not None and et is not None:
                state_intervals.append((et, state))
                if state == 8 and last_state != 8:  # STATE_CHARGING_PHASE
                    charge_transitions += 1
                last_state = state

    out: dict[str, Any] = {
        "n_voltage_samples": len(voltage_samples),
        "n_pct_samples": len(pct_samples),
        "charge_cycles": charge_transitions,
    }

    if voltage_samples:
        mvs = [mv for _, mv in voltage_samples]
        out["voltage_mv"] = {"min": min(mvs), "max": max(mvs),
                              "mean": round(sum(mvs) / len(mvs), 1)}

    if pct_samples:
        pcts = [p for _, p in pct_samples]
        out["battery_percentage"] = {
            "min": round(min(pcts), 2),
            "max": round(max(pcts), 2),
            "mean": round(sum(pcts) / len(pcts), 2),
        }

    # Drain rate: simple linear regression on voltage_mv vs time, ignoring
    # samples taken while charging (= voltage rising). We segment on charge
    # transitions (or just by sign of dv/dt) and report only the discharge runs.
    if len(voltage_samples) >= 4:
        voltage_samples.sort()
        # Compute per-segment dv/dt while voltage trends down; ignore charging spikes
        discharge_runs: list[tuple[int, int, int, int]] = []  # (t0, t1, mv0, mv1)
        run_start: tuple[int, int] | None = None
        last: tuple[int, int] | None = None
        for t, mv in voltage_samples:
            if last is None:
                run_start = (t, mv)
                last = (t, mv)
                continue
            if mv > last[1] + 50:  # >50 mV jump = likely charging
                if run_start and last[0] - run_start[0] > 600_000:  # ≥10 min run
                    discharge_runs.append((run_start[0], last[0], run_start[1], last[1]))
                run_start = (t, mv)
            last = (t, mv)
        if run_start and last and last[0] - run_start[0] > 600_000:
            discharge_runs.append((run_start[0], last[0], run_start[1], last[1]))

        if discharge_runs:
            # Aggregate drain rate: total mv-drop / total hours discharging
            total_dt_h = sum((t1 - t0) / 3_600_000 for t0, t1, _, _ in discharge_runs)
            total_dmv = sum(mv0 - mv1 for _, _, mv0, mv1 in discharge_runs if mv0 > mv1)
            out["discharge_runs"] = len(discharge_runs)
            out["total_discharge_h"] = round(total_dt_h, 2)
            out["mean_drain_mv_per_h"] = round(total_dmv / total_dt_h, 2) if total_dt_h > 0 else None

    # State-time breakdown (rough: how long was the ring in each state)
    if len(state_intervals) >= 2:
        state_durations: dict[str, float] = {}
        for i in range(len(state_intervals) - 1):
            t, st = state_intervals[i]
            t_next = state_intervals[i + 1][0]
            dur_h = (t_next - t) / 3_600_000
            key = f"state_{st}"
            state_durations[key] = state_durations.get(key, 0) + dur_h
        out["state_hours"] = {k: round(v, 2) for k, v in
                               sorted(state_durations.items(), key=lambda x: -x[1])}

    return out
