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

import re
import uuid
from collections import Counter
from typing import Any

from src.synth import entity_registry as _er

# #287: the interactions /exec reports, and the actions each one allows. Only ids, keys and enums
# travel (never question/answer text, URLs or free text); anything off this list is refused.
KINDS: dict[str, frozenset[str]] = {
    "session": frozenset({"open"}), "hoje": frozenset({"open"}), "install": frozenset({"open"}),
    "headline": frozenset({"expand", "collapse"}),
    "ask": frozenset({"open", "chip"}),            # chip = an example question, by index
    "share": frozenset({"open"}),
    "push": frozenset({"open", "subscribe"}),
    "drawer": frozenset({"open"}),                 # a board row -> its sources drawer
    "tile": frozenset({"open"}),                   # a KPI tile -> the panel it counts (key = panel)
    "why": frozenset({"open"}),                    # "evidência" on a trajectory / recommendation
    "source": frozenset({"follow"}),               # a source link opened (card id, never the URL)
    "report": frozenset({"open", "sent"}),         # Reportar (key = defeito|sugestao)
    "nav": frozenset({"open"}),                    # officer / view switch (key = cso|…|hoje|painel)
    "upgrade": frozenset({"open"}),                # plan button (key = module or combo id)
    "signin": frozenset({"password", "google", "apple"}),
}
_ACTIONS = frozenset().union(*KINDS.values())
# housekeeping, not attention to content: counted per kind, kept out of the §E interest signal
_NOT_INTEREST = {"signin", "upgrade", "report", "nav", "install"}
_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,47}")


def _key(v: Any) -> str | None:
    """A short id/enum (panel key, officer, module, chip index), or nothing at all."""
    k = str(v or "").strip().lower()
    return k if _KEY_RE.fullmatch(k) else None


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
    key: str | None = None,
    threat_score: Any = None,
    industries: list[str] | None = None,
    topics: list[str] | None = None,
    tenant: str | None = None,
    device: str | None = None,
    standalone: bool | None = None,
    table: Any | None = None,
) -> dict[str, Any]:
    """Append one engagement event. `kind` = the interaction (e.g. 'headline'); `action` =
    expand|collapse|open|follow. Best-effort; returns the stored item.

    #165: ``device`` = phone|tablet|desktop (a CLASS from viewport width + pointer type, never a
    raw user agent) and ``standalone`` = the session ran as an installed PWA. ``tenant`` is the
    caller's verified tenant. No other device data is stored.

    #287: ``kind``/``action`` must be a pair from ``KINDS`` (ValueError otherwise); ``key`` is an
    optional short id (``_KEY_RE``), dropped when it looks like anything else."""
    kind = (kind or "").strip().lower() or "headline"
    action = (action or "").strip().lower() or ("expand" if kind == "headline" else "open")
    if action not in KINDS.get(kind, ()):
        raise ValueError(f"unknown engagement {kind}/{action}")
    eid = uuid.uuid4().hex[:16]
    item = {
        "pk": f"ENGAGEMENT#{eid}", "type": "engagement", "engagement_id": eid,
        "kind": kind, "action": action, "key": _key(key),
        "officer": (officer or "").strip() or None,
        "sector": (sector or "").strip() or None,
        "card_id": (card_id or "").strip() or None,
        "entity": (entity or "").strip() or None,
        "threat_score": threat_score,
        "industries": list(industries or []),
        "topics": list(topics or []),
        "tenant": (tenant or "").strip() or None,
        "device": (device or "").strip() or None,
        "standalone": bool(standalone) if standalone is not None else None,
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
    """Roll up attention: counts per entity / sector / officer / action, per kind (``by_kind``:
    kind -> action -> n), and the top-N interest lists (expand/open/follow on content weight
    attention; collapse and the housekeeping kinds — sign-in, upgrade, Reportar, nav — don't).

    Our own traffic (QA personas, the operator, the owner — `internal_traffic.is_internal`) is
    dropped first and only COUNTED (`internal_excluded`): it fed the ETS Engagement component and
    the "interesse executivo" lists with automation (10/10 on zero client events, 2026-10-18)."""
    from src.synth.internal_traffic import is_internal

    labels = labels or {}
    events = [e for e in (events or []) if isinstance(e, dict)]
    internal = sum(1 for e in events if is_internal(e))
    events = [e for e in events if not is_internal(e)]
    interest = [e for e in events if e.get("action") in ("expand", "open", "follow")
                and e.get("kind") not in _NOT_INTEREST]
    by_kind: dict[str, Counter] = {}
    for e in events:
        by_kind.setdefault(str(e.get("kind") or "headline"), Counter())[str(e.get("action") or "expand")] += 1
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
        "n_events": len(events), "n_interest": len(interest), "internal_excluded": internal,
        "actions": dict(Counter(e.get("action") for e in events)),
        "by_kind": {k: dict(v) for k, v in sorted(by_kind.items())},   # #287 → usage page (#288)
        "top_entities": [{"entity": k, "label": labels.get(k, k), "hits": v}
                         for k, v in by_entity.most_common(top)],
        "top_sectors": [{"sector": k, "hits": v} for k, v in by_sector.most_common(top)],
        "by_officer": dict(by_officer),
    }
