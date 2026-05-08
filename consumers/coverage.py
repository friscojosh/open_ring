"""Per-record-type ingest coverage + missed-record (counter-gap) detection.

Inputs: every inner record (anything with `tag` starting `0x` and a counter).

Outputs:
    - per-type record count + percent of total
    - per-type counter-gap analysis: did any records get dropped between
      the first and last counter we observed for each (type, session)?
    - sessions seen (distinct `sess` values)
    - decode-error count (records that came back with `_decode_error`)
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any


def compute(records: Iterable) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    decode_errors: Counter[str] = Counter()
    # (type, session) → (first_ctr, last_ctr, observed_count)
    counter_state: dict[tuple[str, int], list[int]] = {}
    sessions: set[int] = set()
    n_total = 0

    for r in records:
        if not r.tag.startswith("0x"):
            continue
        n_total += 1
        counts[r.type] += 1
        if r.data.get("_decode_error"):
            decode_errors[r.type] += 1
        if r.sess is not None:
            sessions.add(r.sess)
            if r.ctr is not None:
                key = (r.type, r.sess)
                state = counter_state.get(key)
                if state is None:
                    counter_state[key] = [r.ctr, r.ctr, 1]
                else:
                    state[1] = r.ctr
                    state[2] += 1

    out: dict[str, Any] = {
        "total_inner_records": n_total,
        "distinct_types": len(counts),
        "distinct_sessions": len(sessions),
        "sessions_seen": sorted(sessions),
        "decode_error_count": sum(decode_errors.values()),
    }
    if decode_errors:
        out["decode_errors_by_type"] = dict(decode_errors)

    if n_total:
        # Top types with percent share
        out["top_types"] = [
            {"type": t, "count": c, "pct": round(c / n_total * 100, 2)}
            for t, c in counts.most_common(15)
        ]

    # Counter-gap detection — assumes counter is uint16 with monotonic increase
    # within a session. Reports the implied "missed" count per (type, session).
    # Does NOT account for uint16 wrap; flags it instead.
    gap_summary: list[dict[str, Any]] = []
    for (t, sess), (first, last, observed) in counter_state.items():
        if observed < 2:
            continue
        if last < first:  # wrap suspected
            implied = (last + 0x10000) - first + 1
        else:
            implied = last - first + 1
        missed = max(0, implied - observed)
        if missed > 0 or last < first:
            gap_summary.append({
                "type": t, "session": sess,
                "first_ctr": first, "last_ctr": last,
                "observed": observed, "implied": implied,
                "missed": missed,
                "miss_pct": round(missed / implied * 100, 2) if implied else 0,
                "wrap_suspected": last < first,
            })
    if gap_summary:
        gap_summary.sort(key=lambda g: -g["missed"])
        out["counter_gaps_top_20"] = gap_summary[:20]
        # NOTE: this is the SUM of (last - first - observed) across (type, sess)
        # pairs — it is NOT "records lost on the wire", because a long-running
        # session may have advanced its counter while we were disconnected. Use
        # the per-(type, sess) `miss_pct` for actionable signal instead.
        out["counter_gap_total_implied_missing"] = sum(g["missed"] for g in gap_summary)

    return out
