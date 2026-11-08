"""CPO Product Radar → framework evidence (#371–#375, shares the #369 rules).

The radar (`src/ingest/cpo_radar.py`, #159) clusters YouTube videos (official channels +
BR creators) into product events and keeps App Store review baselines. This module is the
ONE place the strategy axes read it from, so the shared rules live here:

* **Confirmed only.** A YouTube event counts when ``confidence`` is ``oficial`` (an official
  channel) or ``corroborado`` (>= 2 channels) and it resolved to an Onça ``entity``.
  Single-creator events measure attention, not competitive movement, and stay out.
* **Never a narrative.** Radar items are not activity narratives and must never reach the
  feature store's activity / behavioral / silence inputs. Momentum, Ameaça, Manchetes,
  Sinais emergentes and Movimentos stay narrative-driven (owner, 2026-11-08). A derived card
  whose gate only passes because of radar items carries ``radar_backed: True`` and the
  executive panels skip it.
* **YouTube API terms — 30-day deletion.** Durable stores (narratives, threads, proposals,
  outcome logs) keep ONLY the radar event id plus Onça's own labels (entity, product, type,
  confidence, channel count, date) — never the video title/description, the classifier's
  change text, a URL or a channel name. ``label_only`` is that projection; ``claim`` and
  ``citation`` are built from it. Transient processing (theme tagging, an LLM prompt) may read
  the event's ``title`` while the event is still inside the radar's own retention window.
"""
from __future__ import annotations

import os
from typing import Any

CONFIRMED = ("oficial", "corroborado")
PRODUCT_TYPES = ("launch", "feature")
FAILURE_TYPES = ("outage", "complaint")

# The fields a durable store may keep (Onça labels; no YouTube API data).
_LABEL_FIELDS = ("id", "entity", "product", "product_label", "source", "type", "type_label",
                 "confidence", "n_channels", "date")


def load(bucket: str | None, *, s3: Any = None, key: str | None = None) -> dict[str, Any]:
    """The radar document (``product_radar/latest.json`` by default). {} if absent/unreadable.

    Never raises: every consumer treats the radar as optional evidence."""
    if not bucket or os.environ.get("ONCA_RADAR_EVIDENCE", "1") in ("0", "false", "False"):
        return {}
    try:
        from src.ingest import cpo_radar
        if key is None:
            return cpo_radar.load_radar(bucket, s3=s3) or {}
        return cpo_radar.S3Store(bucket, s3=s3).get(key) or {}
    except Exception as exc:  # pragma: no cover - best-effort
        print(f"Warning: radar evidence unavailable ({key or 'latest'}): {exc}")
        return {}


def load_snapshot(bucket: str | None, date: str, *, s3: Any = None) -> dict[str, Any]:
    """That day's dated snapshot (``product_radar/{date}.json``; pruned after 30 days)."""
    return load(bucket, s3=s3, key=f"product_radar/{date}.json")


def is_confirmed(ev: Any) -> bool:
    return (isinstance(ev, dict) and ev.get("source", "youtube") == "youtube"
            and ev.get("confidence") in CONFIRMED and bool(ev.get("entity"))
            and bool(ev.get("id")) and bool(ev.get("date")))


def confirmed_events(radar: dict[str, Any], *, types: tuple[str, ...] | None = None,
                     entities: set[str] | None = None) -> list[dict[str, Any]]:
    """Confirmed YouTube events, optionally filtered by type / entity, newest first."""
    out = [e for e in (radar or {}).get("events") or [] if is_confirmed(e)
           and (types is None or e.get("type") in types)
           and (entities is None or e.get("entity") in entities)]
    out.sort(key=lambda e: (e.get("date") or "", e.get("id") or ""), reverse=True)
    return out


def appstore_alerts(radar: dict[str, Any]) -> list[dict[str, Any]]:
    """App Store review-baseline alerts with a resolved entity (count >= 10 AND z >= 3)."""
    return [a for a in (radar or {}).get("alerts") or []
            if isinstance(a, dict) and a.get("source") == "appstore" and a.get("entity")
            and a.get("id") and a.get("date")]


def label_only(ev: dict[str, Any]) -> dict[str, Any]:
    """The persistable projection: Onça's labels only (see module doc)."""
    return {k: ev.get(k) for k in _LABEL_FIELDS if ev.get(k) is not None}


def source_phrase(ev: dict[str, Any]) -> str:
    """'YouTube oficial' / 'YouTube, 7 canais' / 'App Store (avaliações)'."""
    if ev.get("source") == "appstore":
        return "App Store (avaliações)"
    if ev.get("confidence") == "oficial":
        return "YouTube oficial"
    return f"YouTube, {int(ev.get('n_channels') or 2)} canais"


def _br_date(d: str | None) -> str:
    try:
        y, m, dd = str(d)[:10].split("-")
        return f"{dd}/{m}/{y}"
    except ValueError:
        return str(d or "")


def claim(ev: dict[str, Any]) -> str:
    """A label-only pt-BR claim, safe to persist: 'Itaú — lançamento (YouTube oficial, 04/11/2026)'."""
    who = ev.get("product_label") or ev.get("entity") or "?"
    what = ev.get("type_label") or ev.get("type") or "evento"
    return f"{who} — {what} ({source_phrase(ev)}, {_br_date(ev.get('date'))})"


def citation(ev: dict[str, Any]) -> dict[str, Any]:
    """Feed citation for a radar event: id + source + label claim, no URL (the URL is YouTube
    API data; the live radar card still links it while the event exists)."""
    return {"id": ev["id"], "source": "product_radar", "label": claim(ev)}


def prompt_claim(ev: dict[str, Any]) -> str:
    """Transient (prompt-only, never persisted) claim: the label claim plus the change text."""
    title = str(ev.get("title") or "").strip()
    return f"{claim(ev)}: {title[:120]}" if title else claim(ev)

