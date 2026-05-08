"""Activity / motion summary statistics.

Inputs:
    - 0x47 MotionEvent — `acm_x`, `acm_y`, `acm_z`, `flags_low/high`
    - 0x6b MotionPeriod — periodic motion-state snapshot
    - 0x7e/0x7f RealStepsFeatures — step-related FFT features
    - StateChange transitions to STATE_FINGER_USER_ACTIVE (=3 / =5 SpO2 variant)

Outputs:
    - motion event count + acm magnitude distribution
    - active vs sedentary fraction (state-based)
    - orientation distribution (lower 5 bits of MotionEvent flags_low)
    - real-step record count (per-feature event count, NOT step count — that
      requires libappecore aggregation)
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from typing import Any

from . import event_time


_ACTIVE_STATES = {3, 5}      # FINGER_USER_ACTIVE, FINGER_HR_USER_ACTIVE
_SEDENTARY_STATES = {4, 6}   # FINGER_USER_IN_REST, FINGER_HR_USER_IN_REST


def compute(records: Iterable) -> dict[str, Any]:
    motion_count = 0
    motion_period_count = 0
    acm_magnitudes: list[float] = []
    orientation_counter: Counter[int] = Counter()
    real_steps_1_count = 0
    real_steps_2_count = 0
    state_intervals: list[tuple[int, int]] = []

    for r in records:
        if r.type == "API_MOTION_EVENT":
            motion_count += 1
            d = r.data
            x, y, z = d.get("acm_x"), d.get("acm_y"), d.get("acm_z")
            if x is not None and y is not None and z is not None:
                acm_magnitudes.append(math.sqrt(x * x + y * y + z * z))
            flags_low = d.get("flags_low")
            if flags_low is not None:
                orientation_counter[flags_low & 0x1F] += 1
        elif r.type == "API_MOTION_PERIOD":
            motion_period_count += 1
        elif r.type == "API_REAL_STEP_EVENT_FEATURE_ONE":
            real_steps_1_count += 1
        elif r.type == "API_REAL_STEP_EVENT_FEATURE_TWO":
            real_steps_2_count += 1
        elif r.type in ("API_STATE_CHANGE_IND", "API_WEAR_EVENT"):
            state = r.data.get("state")
            if state is not None:
                et = event_time(r)
                if et is not None:
                    state_intervals.append((et, state))

    out: dict[str, Any] = {
        "motion_event_count": motion_count,
        "motion_period_count": motion_period_count,
        "real_steps_features_count": real_steps_1_count + real_steps_2_count,
    }

    if acm_magnitudes:
        out["acm_magnitude"] = {
            "min": round(min(acm_magnitudes), 1),
            "max": round(max(acm_magnitudes), 1),
            "mean": round(sum(acm_magnitudes) / len(acm_magnitudes), 1),
        }

    if orientation_counter:
        total = sum(orientation_counter.values())
        out["orientation_distribution"] = {
            f"orient_{k}": {"count": v, "fraction": round(v / total, 3)}
            for k, v in orientation_counter.most_common(8)
        }

    # Active vs sedentary time accumulator (state-based)
    if len(state_intervals) >= 2:
        active_h = sedentary_h = other_h = 0.0
        for i in range(len(state_intervals) - 1):
            t, st = state_intervals[i]
            t_next = state_intervals[i + 1][0]
            dur_h = (t_next - t) / 3_600_000
            if st in _ACTIVE_STATES:
                active_h += dur_h
            elif st in _SEDENTARY_STATES:
                sedentary_h += dur_h
            else:
                other_h += dur_h
        total_h = active_h + sedentary_h + other_h
        if total_h:
            out["state_time_h"] = {
                "active": round(active_h, 2),
                "sedentary": round(sedentary_h, 2),
                "other": round(other_h, 2),
                "active_fraction": round(active_h / total_h, 3),
            }

    return out
