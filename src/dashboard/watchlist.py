"""Per-tenant watchlist, score weights and alert preferences (#302, ADR 001 step 6 §3).

A tenant says "these are my competitors". The list is stored on its ``onca-tenant-config`` row next
to the entitlement and is applied at read time to that tenant's own scoped feed:

* **Ranking:** watched entities come first in ``entities``. Each feed card gets a ``rank_score``
  (``threat_score`` × lens weight × watchlist boost) and the feed is ordered by it. The displayed
  ``threat_score`` is unchanged: the weights are the tenant's priorities, not a re-score of the evidence.
* **Alerts:** an alert-class card about a watched entity is a push event of its own (``watch``),
  and the weekly email gets a "Seus concorrentes" section.

**A watchlist never widens entitlement.** On write, every id must be a registry entity inside the
tenant's licensed modules (directly or through its ADR 017 parent). On read, ids outside today's
scoped feed are dropped, so a lapsed module silently retires its watched entities.
A watchlist is tenant data: it lives on the tenant's row and in its own ``/api/feed`` response,
never in the shared ``feed.json``.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable

WATCHLIST_MAX = 25
WATCH_BOOST_DEFAULT = 1.5
WATCH_BOOST_RANGE = (1.0, 5.0)
LENS_WEIGHT_RANGE = (0.0, 3.0)
ALERT_DEFAULTS = {"push": True, "email": True, "only_watchlist": False}
_ID = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,79}$")
_LENS = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


def clean_watchlist(ids: Any) -> list[str]:
    """Deduplicated, order-kept entity ids. Raises ValueError on a bad id or more than the cap."""
    if ids is None:
        return []
    if not isinstance(ids, (list, tuple)):
        raise ValueError("watchlist must be a list of entity ids")
    out: list[str] = []
    for raw in ids:
        eid = str(raw or "").strip().lower()
        if not _ID.match(eid):
            raise ValueError(f"invalid entity id {raw!r}")
        if eid not in out:
            out.append(eid)
    if len(out) > WATCHLIST_MAX:
        raise ValueError(f"watchlist holds at most {WATCHLIST_MAX} entities")
    return out


def _num(v: Any, lo: float, hi: float, name: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not lo <= f <= hi:
        raise ValueError(f"{name} must be between {lo:g} and {hi:g}")
    return round(f, 2)


def clean_score_weights(w: Any) -> dict[str, float]:
    """``{"watchlist": boost 1–5, "<lens>": weight 0–3}``. Unknown shapes raise ValueError."""
    if w is None:
        return {}
    if not isinstance(w, dict):
        raise ValueError("score_weights must be an object")
    out: dict[str, float] = {}
    for k, v in w.items():
        key = str(k).strip().lower()
        if key == "watchlist":
            out[key] = _num(v, *WATCH_BOOST_RANGE, "score_weights.watchlist")
        elif _LENS.match(key):
            out[key] = _num(v, *LENS_WEIGHT_RANGE, f"score_weights.{key}")
        else:
            raise ValueError(f"unknown score weight {k!r}")
    if len(out) > 40:
        raise ValueError("too many score weights")
    return out


def clean_alert_prefs(p: Any) -> dict[str, bool]:
    if p is None:
        return {}
    if not isinstance(p, dict):
        raise ValueError("alert_prefs must be an object")
    bad = set(p) - set(ALERT_DEFAULTS)
    if bad:
        raise ValueError(f"unknown alert_prefs {sorted(bad)}")
    return {k: bool(v) for k, v in p.items()}


def alert_prefs_of(cfg: dict[str, Any] | None) -> dict[str, bool]:
    return {**ALERT_DEFAULTS, **((cfg or {}).get("alert_prefs") or {})}


def entitled(eid: str, modules: list[str], get_entity: Callable[[str], dict[str, Any] | None]) -> bool:
    """Is the registry entity inside the licensed modules, itself or through its parent chain?"""
    keep = {str(m).strip().lower() for m in modules or []}
    seen: set[str] = set()
    cur: str | None = eid
    while cur and cur not in seen and keep:
        seen.add(cur)
        ent = get_entity(cur)
        if not ent:
            return False
        if keep & {str(i).strip().lower() for i in ent.get("industries") or []}:
            return True
        cur = ent.get("parent")
    return False


def card_entities(c: dict[str, Any]) -> set[str]:
    return {e for e in [c.get("entity"), *(c.get("entities") or [])] if e}


def _ts(c: dict[str, Any]) -> float:
    try:
        return float(c.get("threat_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def rank_score(c: dict[str, Any], watched: set[str], weights: dict[str, float]) -> float:
    """``threat_score`` × the strongest matching lens weight (1 if none is set) × the watchlist
    boost when the card is about a watched entity."""
    lw = [weights[l] for l in (c.get("lenses") or []) if l in weights]
    s = _ts(c) * (max(lw) if lw else 1.0)
    if watched and card_entities(c) & watched:
        s *= weights.get("watchlist", WATCH_BOOST_DEFAULT)
    return round(s, 4)


def apply_to_feed(scoped: dict[str, Any], cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Apply a tenant's preferences to its ALREADY-SCOPED feed (mutates and returns it)."""
    cfg = cfg or {}
    in_scope = {r.get("entity") for r in scoped.get("entities") or []} | set(scoped.get("entity_attrs") or {})
    watch = [e for e in cfg.get("watchlist") or [] if e in in_scope]
    weights = dict(cfg.get("score_weights") or {})
    scoped["watchlist"] = watch
    scoped["score_weights"] = weights
    scoped["alert_prefs"] = alert_prefs_of(cfg)
    if not watch and not weights:
        return scoped
    ws = set(watch)
    order = {e: i for i, e in enumerate(watch)}
    ents = scoped.get("entities") or []
    for r in ents:
        if r.get("entity") in ws:
            r["watched"] = True
    # stable: watched first (in the tenant's own order), the rest as built
    scoped["entities"] = sorted(ents, key=lambda r: order.get(r.get("entity"), len(order)))
    cards = scoped.get("feed") or []
    for c in cards:
        c["rank_score"] = rank_score(c, ws, weights)
        if ws and card_entities(c) & ws:
            c["watched"] = True
    scoped["feed"] = sorted(cards, key=lambda c: -c["rank_score"])
    return scoped


def watch_events(scoped: dict[str, Any], watchlist: list[str], *, now: dt.date | None = None,
                 window_days: int = 3) -> list[tuple[str, str]]:
    """("watch", card id) for each recent alert-class card about a watched entity."""
    ws = set(watchlist or [])
    if not ws:
        return []
    floor = ((now or dt.date.today()) - dt.timedelta(days=window_days)).isoformat()
    return [("watch", str(c["id"])) for c in scoped.get("feed") or []
            if c.get("is_alert") and c.get("id") and str(c.get("date") or "") >= floor
            and card_entities(c) & ws]


def digest_part(scoped: dict[str, Any], watchlist: list[str], *, today: str, days: int = 7,
                limit: int = 8) -> dict[str, Any] | None:
    """The weekly email's "Seus concorrentes" section: the week's alert cards about watched
    entities, highest threat first. None when there is nothing to say."""
    ws = set(watchlist or [])
    if not ws:
        return None
    floor = (dt.date.fromisoformat(today) - dt.timedelta(days=days)).isoformat()
    cards = [c for c in scoped.get("feed") or []
             if c.get("is_alert") and str(c.get("date") or "") >= floor and card_entities(c) & ws]
    if not cards:
        return None
    cards.sort(key=lambda c: -_ts(c))
    label = {r.get("entity"): r.get("label") for r in scoped.get("entities") or []}
    lines = []
    for c in cards[:limit]:
        who = c.get("entity_label") or label.get(c.get("entity")) or c.get("subject_label") or ""
        text = re.sub(r"\s+", " ", str(c.get("narrative") or "")).strip()
        lines.append(f"{who}: {text[:180]}" if who else text[:200])
    names = [label.get(e) or e for e in watchlist if e in label][:6]
    return {"headline": f"{len(cards)} alerta(s) na semana sobre {', '.join(names)}.",
            "lines": lines, "lines_heading": "Destaques"}
