"""CSO dimension 3 — deterministic macro-correlation annotation on narratives.

Attaches a date-proximity note to an entity's OWN narrative when it falls near a
known macro event (a Copom/Selic decision). This is deliberately NOT a new
review-gated proposal type like PESTLE/SWOT (``pestle.py``, ``swot_reconcile.py``)
— those exist to gate *LLM-generated, interpretive* judgments that can hallucinate
or misjudge. This is a template-generated, 100% deterministic date computation
with no LLM call, so there's nothing for a human reviewer to catch that the code
itself can't already guarantee (the governance is the mandatory ``inferência:``
label, same tier as the trajectory chart's tick marks in ``v3/index.html``'s
``sparkline()``).

Two hard invariants, both enforced by construction:
- The annotation attaches to an entity's OWN narrative, which already has its own
  real evidence/citations. It never creates a new entity attribution — the macro
  event itself stays entity-less, exactly as O3's hard rule requires.
- The text is always a hedge ("inferência: ... coincide com ...", never "causou"/
  "por causa de") — correlation in time, never asserted causation. See the O3
  ADR discussion and Tarantula's outage-causation precedent for why this matters.

**Deliberately Selic-only, NOT gdelt_macro-dated (fixed 2026-09-18, live-verified
regression found on the first real run).** The GDELT macro-theme digest is a
daily-refreshed SNAPSHOT — its "date" is structurally always "today" (or
"yesterday", per ``gdelt_macro_handler.py``'s ``target_date``), not a rare,
meaningful event like a Copom decision. Using it as a correlation anchor meant
that on ANY day the pipeline ran, virtually every fresh narrative landed "within
window" of it — 406 of ~450 live feed items got the note on the first real run,
which happened to coincide with a genuine Selic decision. That's not a
correlation signal, it's just "today has news," which is always true. Selic
decisions are genuinely rare (~every 45 days), so they stay a meaningful anchor;
gdelt_macro dates don't, so they're excluded here. The header strip
(``build_macro``'s ``gdelt_macro`` headlines) is unaffected — it never claimed a
correlation, just showed today's backdrop, so this fix doesn't touch it.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

DEFAULT_WINDOW_DAYS = 3


def macro_event_dates(macro: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """date (ISO str) -> {"kind", "label"} for every macro event we treat as a
    meaningful, rare correlation anchor — Selic decisions only (see module
    docstring for why gdelt_macro dates are deliberately excluded).

    Mirrors ``macroEventDates()`` in v3/index.html — kept in sync deliberately. If
    one drifts from the other, the chart tick (#1) and this annotation (#3) would
    disagree about what counts as a macro event, which would be confusing on its
    own — keep them identical.
    """
    macro = macro or {}
    out: dict[str, dict[str, Any]] = {}
    selic = macro.get("selic") or {}
    d = (selic.get("last_decision") or {}).get("date")
    if d:
        out[d] = {"kind": "selic", "label": f"decisão do Copom (Selic {selic.get('current')}%)"}
    return out


def _parse_date(s: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def _nearest(item_date: dt.date, events: dict[str, dict[str, Any]], window_days: int):
    best = None
    for ds, info in events.items():
        ed = _parse_date(ds)
        if ed is None:
            continue
        gap = (item_date - ed).days
        if abs(gap) > window_days:
            continue
        if best is None or abs(gap) < abs(best[0]):
            best = (gap, ds, info)
    return best


def annotate_feed_items(
    feed_items: list[dict[str, Any]],
    macro: dict[str, Any] | None,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> list[dict[str, Any]]:
    """Return ``feed_items`` with a ``macro_note`` attached wherever a narrative's
    own date falls within ``window_days`` of a known macro event. Items with no
    match, no date, or a malformed date are returned unchanged (best-effort — a
    missing macro digest must never break the feed build)."""
    events = macro_event_dates(macro)
    if not events:
        return feed_items
    out = []
    for item in feed_items:
        item_date = _parse_date(item.get("date")) if isinstance(item, dict) else None
        hit = _nearest(item_date, events, window_days) if item_date else None
        if hit is None:
            out.append(item)
            continue
        gap, event_date, info = hit
        # gap = item_date - event_date: positive means the narrative landed AFTER
        # the macro event ("depois"), negative means it landed BEFORE it ("antes").
        when = "no mesmo dia" if gap == 0 else (
            f"{abs(gap)} dia(s) {'depois' if gap > 0 else 'antes'}"
        )
        note = {
            "gap_days": gap,
            "event_date": event_date,
            "kind": info["kind"],
            "text": f"inferência: coincide no tempo ({when}) com {info['label']} em {event_date}.",
        }
        out.append({**item, "macro_note": note})
    return out
