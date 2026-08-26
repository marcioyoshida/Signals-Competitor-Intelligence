"""Shared per-dimension evidence binding for the strategy frameworks (ADR #34).

The narrative corpus classifies evidence across TWO disjoint fields: derived /
detector cards carry a stamped ``axis`` (comparative, longitudinal, regulatory, …)
while base-news narratives carry ``lenses`` (news, pix, entrants, juros, …) and no
axis. So a framework dimension binds to the evidence that actually evidences it via

    DIM_SIGNALS[dim] = (axis_set, lens_set)

A cited evidence item is *on-signal* for a dimension iff its ``axis`` is in
``axis_set`` OR any of its ``lenses`` is in ``lens_set``. Empty sets on BOTH sides
mean the dimension is unconstrained (any cited evidence counts) — the safe fallback
so a dimension is never silently starved by a mapping gap.

This realizes the ADR-006 addendum (#32) "hard, axis/lens-valid evidence link"
without an axis-only whitelist (which would drop the ~83% of narratives that are
axis-less). The gate can be disabled wholesale via ``ONCA_FRAMEWORK_SIGNAL_GATE=0``
(reversibility: falls back to the prior "any cited in-range evidence" behavior).
"""
from __future__ import annotations

import os
from typing import Any

# (axes, lenses) — either side empty is "no constraint from this side"; BOTH empty
# means the dimension accepts any cited evidence.
Signal = tuple[frozenset[str], frozenset[str]]


def fz(*items: str) -> frozenset[str]:
    """Terse frozenset constructor for the DIM_SIGNALS tables."""
    return frozenset(items)


def _gate_enabled() -> bool:
    return os.environ.get("ONCA_FRAMEWORK_SIGNAL_GATE", "1") not in ("0", "false", "False")


def on_signal(ev: dict[str, Any], signal: Signal) -> bool:
    """True iff evidence ``ev`` matches ``signal`` (its axis in axis_set OR one of its
    lenses in lens_set). An unconstrained signal (both sets empty) always matches."""
    axes, lenses = signal
    if not axes and not lenses:
        return True
    ax = ev.get("axis")
    if ax is not None and ax in axes:
        return True
    return any(l in lenses for l in (ev.get("lenses") or []))


def on_signal_ids(
    cited_indices: list[Any],
    evidence: list[dict[str, Any]],
    dim: str,
    dim_signals: dict[str, Signal],
) -> list[str]:
    """The ids of cited evidence that are on-signal for ``dim`` (order-stable, deduped).

    ``cited_indices`` are indices into ``evidence`` (already validated in-range by the
    caller's ``_parse_draft``). A dimension absent from ``dim_signals`` is treated as
    unconstrained. With the gate disabled, every in-range cited id is kept."""
    gate = _gate_enabled()
    signal = dim_signals.get(dim)
    out: list[str] = []
    for j in cited_indices:
        try:
            j = int(j)
        except (TypeError, ValueError):
            continue
        if not (0 <= j < len(evidence)):
            continue
        ev = evidence[j]
        if gate and signal is not None and not on_signal(ev, signal):
            continue
        eid = ev.get("id")
        if eid and eid not in out:
            out.append(eid)
    return out
