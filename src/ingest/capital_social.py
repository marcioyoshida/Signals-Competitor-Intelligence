"""#143 — capital social: persist it, diff it, and signal a material move.

**Why this exists.** The junta comercial acts (atos de alteração contratual) that would
tell us a competitor just took an aporte are credential-gated — #26 probed every route
and closed. `capital_social` is the credential-free *partial* substitute: Receita's open
CNPJ data publishes each entity's registered capital, and it only moves when a corporate
act has actually been registered at a junta. We cannot read the act, but we can see its
balance-sheet shadow.

**What it is NOT.** A capital increase is not revenue, valuation, or a funding round —
it is the *registered* capital, which a group can also raise for an internal
reorganization. The signal is "a corporate act happened here", not "they raised money".
Card copy must stay at that claim.

**Materiality.** Two floors, both required, because either alone misfires:
  - a percentage floor alone turns a small entrant's R$100k housekeeping bump into a
    headline (10% of R$1M);
  - an absolute floor alone buries a genuine doubling of a small competitor's capital
    under a big bank's rounding.
So a move must clear BOTH `MATERIAL_PCT` and `MATERIAL_ABS` to surface.

**Data-artifact caution.** Receita's open snapshot is republished periodically, so a
first observation is never a "change" (there is no prior), and a value that moves while
the entity's other fields churn may be a correction rather than an act. We therefore
keep the *previous* value on the record and always show both numbers, so a reader can
judge the move rather than trust a delta in isolation.

Pure module — no I/O, no clock of its own. `watchlist_qsa` supplies the fetched payload
(it already pays for that HTTP call for the QSA) and `entity_registry` does the writing;
`feed_builder` derives the move list from the registry scan it already performs.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

# A move must clear BOTH floors to be called material. See the module docstring.
MATERIAL_PCT = 10.0        # percent of the prior capital
MATERIAL_ABS = 500_000.0   # BRL

# How long a recorded move stays "recent" enough to surface as a signal. Capital moves
# are slow (the refresh itself is TTL-gated at ~30d), so the window is a quarter rather
# than the feed's usual days — a shorter one would blink the signal in and out between
# runs depending on which entities happened to refresh.
RECENT_DAYS = 90


def parse_capital(value: Any) -> float | None:
    """Coerce BrasilAPI's `capital_social` to a float. None when absent/unparseable.

    The field arrives as a JSON number on most records but as a string on some
    ("1000000.00", occasionally with BR thousand separators), so both are handled.
    Zero is a legitimate registered capital, so it is kept, not treated as missing.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"[^\d,.\-]", "", text)
    if not text:
        return None
    # BR decimal comma ("1.234.567,89") vs plain float ("1234567.89"). If both marks are
    # present the LAST one is the decimal separator; if only a comma is present it is.
    if "," in text and "." in text:
        sep = "," if text.rfind(",") > text.rfind(".") else "."
        other = "." if sep == "," else ","
        text = text.replace(other, "").replace(sep, ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def delta_pct(previous: float | None, current: float | None) -> float | None:
    """Percentage move from `previous` to `current`. None when it cannot be expressed.

    A move off zero capital has no meaningful percentage (division by zero), so it
    returns None — such a move is still reported, on its absolute delta alone, by
    `is_material`.
    """
    if previous is None or current is None or previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100.0, 2)


def is_material(
    previous: float | None,
    current: float | None,
    *,
    pct_floor: float = MATERIAL_PCT,
    abs_floor: float = MATERIAL_ABS,
) -> bool:
    """Does this move clear both floors? A first observation is never material."""
    if previous is None or current is None or previous == current:
        return False
    if abs(current - previous) < abs_floor:
        return False
    pct = delta_pct(previous, current)
    if pct is None:
        # Moving off a zero capital: no percentage exists, so the absolute floor
        # (already cleared above) is the whole test.
        return True
    return abs(pct) >= pct_floor


def direction(previous: float | None, current: float | None) -> str | None:
    if previous is None or current is None or previous == current:
        return None
    return "aumento" if current > previous else "reducao"


def _parse_date(value: Any) -> dt.date | None:
    text = str(value or "")[:10]
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def move_record(entity_id: str, attrs: dict[str, Any]) -> dict[str, Any] | None:
    """Shape one entity's persisted capital state into a move record, or None.

    `attrs` is the entity's slice of `list_entity_attributes()`, whose `capital` key
    carries {value, previous, changed_at}. Returns None unless a prior value exists and
    the move is material — an entity whose capital we merely *know* is not a signal.
    """
    cap = (attrs or {}).get("capital") or {}
    current = parse_capital(cap.get("value"))
    previous = parse_capital(cap.get("previous"))
    if not is_material(previous, current):
        return None
    return {
        "entity": entity_id,
        "label": (attrs or {}).get("label") or entity_id,
        "kind": "capital_social_move",
        "source": "Receita Federal (via BrasilAPI)",
        "capital": current,
        "previous_capital": previous,
        "delta": round((current or 0.0) - (previous or 0.0), 2),
        "delta_pct": delta_pct(previous, current),
        "direction": direction(previous, current),
        "date": str(cap.get("changed_at") or "")[:10] or None,
    }


def moves_from_attrs(
    entity_attrs: dict[str, dict[str, Any]] | None,
    *,
    today: dt.date | None = None,
    within_days: int = RECENT_DAYS,
) -> list[dict[str, Any]]:
    """Derive the recent material capital moves from the per-entity attribute map.

    Pure projection over data the feed build already loaded — deliberately NOT a second
    store, so the registry stays the single source of truth for an entity's capital and
    there is no index that can drift out of step with it.

    Sorted by date (newest first), then by the size of the move, so the strongest recent
    act leads regardless of which entity happened to refresh last.
    """
    today = today or dt.date.today()
    out: list[dict[str, Any]] = []
    for entity_id, attrs in (entity_attrs or {}).items():
        rec = move_record(entity_id, attrs)
        if not rec:
            continue
        changed = _parse_date(rec.get("date"))
        # An undated move is kept (it is still a real, persisted change) but sorts last;
        # dropping it would silently lose a move recorded before dating was added.
        if changed and (today - changed).days > within_days:
            continue
        out.append(rec)
    out.sort(key=lambda r: (r.get("date") or "", abs(r.get("delta") or 0.0)), reverse=True)
    return out
