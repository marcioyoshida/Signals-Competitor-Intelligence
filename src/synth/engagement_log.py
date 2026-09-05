"""ADR 021 §E — engagement telemetry (the Executive Engagement signal).

An executive expanding a headline is an **attention signal** — the raw material for the ETS
Engagement component (§E) and a Product/Strategy intelligence read ("which entities/sectors draw
executive attention"). Captured fire-and-forget via the §H beacon → `record_engagement` → an
append-only `OncaEngagementLog` (`ENGAGEMENT#<id>` items in the entities table, same pattern as
`OncaDecisionLog`). High-frequency telemetry — NOT journaled to OncaCurationLog.

Every event is tagged (officer / sector / entity / card / action + the card's threat/industries)
so the rollup can attribute attention. No PII: the card/entity ids are first-party feed refs.
"""
from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

from src.synth import entity_registry as _er

_ACTIONS = {"expand", "collapse", "open", "follow"}


def _table(table: Any | None = None):
    return _er._table(table)


def record_engagement(
    *,
    kind: str,
    actor: str,
    officer: str | None = None,
    sector: str | None = None,
    card_id: str | None = None,
    entity: str | None = None,
    action: str | None = None,
    threat_score: Any = None,
    industries: list[str] | None = None,
    topics: list[str] | None = None,
    table: Any | None = None,
) -> dict[str, Any]:
    """Append one engagement event. `kind` = the interaction (e.g. 'headline'); `action` =
    expand|collapse|open|follow. Best-effort; returns the stored item."""
    eid = uuid.uuid4().hex[:16]
    item = {
        "pk": f"ENGAGEMENT#{eid}", "type": "engagement", "engagement_id": eid,
        "kind": (kind or "").strip() or "headline",
        "action": (action or "").strip().lower() or "expand",
        "officer": (officer or "").strip() or None,
        "sector": (sector or "").strip() or None,
        "card_id": (card_id or "").strip() or None,
        "entity": (entity or "").strip() or None,
        "threat_score": threat_score,
        "industries": list(industries or []),
        "topics": list(topics or []),
        "actor": actor, "created_at": _er._now_iso(),
    }
    _table(table).put_item(Item={k: v for k, v in item.items() if v is not None})
    return item


def list_engagement(*, since: str | None = None, table: Any | None = None) -> list[dict[str, Any]]:
    """Scan engagement events (newest first), optionally since a created-at floor."""
    items = _er._scan_type(_table(table), "engagement")
    out = [e for e in items if not since or str(e.get("created_at") or "") >= since]
    out.sort(key=lambda e: str(e.get("created_at") or ""), reverse=True)
    return out


def aggregate(events: list[dict[str, Any]], *, labels: dict[str, str] | None = None,
              top: int = 10) -> dict[str, Any]:
    """Roll up attention: counts per entity / sector / officer / action, and the top-N interest
    lists (expand actions weight attention; collapse is neutral)."""
    labels = labels or {}
    events = [e for e in (events or []) if isinstance(e, dict)]
    interest = [e for e in events if e.get("action") in ("expand", "open", "follow")]
    by_entity: Counter = Counter()
    by_sector: Counter = Counter()
    by_officer: Counter = Counter()
    for e in interest:
        if e.get("entity"):
            by_entity[e["entity"]] += 1
        for s in (e.get("industries") or ([e["sector"]] if e.get("sector") else [])):
            if s and s != "__all__":
                by_sector[s] += 1
        if e.get("officer"):
            by_officer[e["officer"]] += 1
    return {
        "n_events": len(events), "n_interest": len(interest),
        "actions": dict(Counter(e.get("action") for e in events)),
        "top_entities": [{"entity": k, "label": labels.get(k, k), "hits": v}
                         for k, v in by_entity.most_common(top)],
        "top_sectors": [{"sector": k, "hits": v} for k, v in by_sector.most_common(top)],
        "by_officer": dict(by_officer),
    }
