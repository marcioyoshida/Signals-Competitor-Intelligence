"""Aggregate per-narrative JSON objects into one static dashboard feed.

Stage B writes `narratives/{date}/{id}.json` to the digests bucket. This module
rolls the most recent window of those into a single `dashboard/feed.json` that
the static warroom UI fetches — feed items (sorted by threat), per-entity
timelines, and KPI rollups. No API/backend: the data changes once a day, so a
pre-built static file keeps cost at the CloudFront request floor (no idle cost).
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any
from urllib.parse import urlparse

import boto3

try:  # reuse the display-name map Stage B already maintains
    from src.synth.synthesize import ENTITY_LABELS
except Exception:  # pragma: no cover - dashboard must build even if synth import shifts
    ENTITY_LABELS = {}

NARRATIVES_PREFIX = "narratives/"
# Published to the static site bucket root; index.html fetches "feed.json".
FEED_KEY = "feed.json"


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def entity_label(entity: str | None) -> str:
    if not entity:
        return "—"
    return ENTITY_LABELS.get(entity, str(entity).replace("_", " ").title())


def display_label(entity: str | None, kind: str | None) -> str:
    """Headline label for a narrative — falls back to kind when entity-less."""
    if entity:
        return entity_label(entity)
    k = str(kind or "")
    if k.startswith("regulatory"):
        return "Regulatório"
    if k.startswith("competitor"):
        return "Sinal de concorrente"
    return "Sinal de mercado"


def _source_of(citation: dict[str, Any]) -> str | None:
    """Best-effort source label for a citation (explicit source or URL host)."""
    src = citation.get("source")
    if src:
        return str(src)
    url = citation.get("url")
    if isinstance(url, str) and url.startswith("http"):
        host = urlparse(url).netloc
        return host or None
    return None


def build_feed(
    narratives: list[dict[str, Any]],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Pure aggregation: narratives -> feed payload. No I/O.

    Feed items are sorted newest-date-first then by threat_score desc. Entity
    timelines carry per-date count + peak score for compact sparklines. KPIs are
    scoped to the latest date, except entities/sources which span the window.
    """
    items: list[dict[str, Any]] = []
    for n in narratives:
        if not isinstance(n, dict):
            continue
        items.append(
            {
                "id": n.get("id"),
                "date": (n.get("as_of") or "")[:10],
                "kind": n.get("kind"),
                "entity": n.get("entity"),
                "entity_label": display_label(n.get("entity"), n.get("kind")),
                "entities": n.get("entities") or [],
                "lenses": n.get("lenses") or [],
                "is_alert": bool(n.get("is_alert")),
                "threat_score": _score(n.get("threat_score")),
                "threat_score_note": n.get("threat_score_note"),
                "narrative": n.get("narrative") or "",
                "citations": [c for c in (n.get("citations") or []) if isinstance(c, dict)],
                "source_ids": n.get("source_ids") or [],
                "mode": n.get("mode"),
            }
        )

    items.sort(key=lambda x: (x["date"], x["threat_score"]), reverse=True)
    dates = sorted({x["date"] for x in items if x["date"]})
    latest = dates[-1] if dates else None

    # Per-entity timelines (peak score + count per date), for sparklines.
    by_entity: dict[str, dict[str, Any]] = {}
    for x in items:
        ent = x["entity"]
        if not ent:
            continue
        rec = by_entity.setdefault(
            ent, {"entity": ent, "label": x["entity_label"], "by_date": {}}
        )
        day = rec["by_date"].setdefault(
            x["date"], {"date": x["date"], "count": 0, "max_score": 0.0}
        )
        day["count"] += 1
        day["max_score"] = max(day["max_score"], x["threat_score"])

    entities: list[dict[str, Any]] = []
    for rec in by_entity.values():
        timeline = [rec["by_date"][d] for d in sorted(rec["by_date"])]
        entities.append(
            {
                "entity": rec["entity"],
                "label": rec["label"],
                "timeline": timeline,
                "peak_score": max((t["max_score"] for t in timeline), default=0.0),
                "total": sum(t["count"] for t in timeline),
            }
        )
    entities.sort(key=lambda r: (r["peak_score"], r["total"]), reverse=True)

    latest_items = [x for x in items if x["date"] == latest]
    sources = {
        s
        for x in items
        for c in x["citations"]
        if (s := _source_of(c))
    }

    return {
        "generated_at": generated_at
        or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "as_of": latest,
        "dates": dates,
        "kpis": {
            "narratives_latest": len(latest_items),
            "alerts_latest": sum(1 for x in latest_items if x["is_alert"]),
            "entities_tracked": len(entities),
            "sources": len(sources),
            "narratives_total": len(items),
        },
        "feed": items,
        "entities": entities,
    }


def _recent_dates(window_days: int) -> set[str]:
    today = dt.date.today()
    return {(today - dt.timedelta(days=i)).isoformat() for i in range(window_days)}


def load_recent_narratives(
    bucket: str,
    window_days: int = 14,
    *,
    s3: Any | None = None,
) -> list[dict[str, Any]]:
    """Read narrative objects for the last window_days from narratives/{date}/."""
    s3 = s3 or boto3.client("s3")
    wanted = _recent_dates(window_days)
    out: list[dict[str, Any]] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=NARRATIVES_PREFIX):
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            # narratives/{date}/{id}.json
            parts = key.split("/")
            if len(parts) < 3 or not key.endswith(".json"):
                continue
            if parts[1] not in wanted:
                continue
            try:
                body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                out.append(json.loads(body.decode("utf-8")))
            except Exception as exc:  # pragma: no cover - skip unreadable objects
                print(f"Warning: skip narrative {key}: {exc}")
    return out


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Build feed.json from recent narratives and publish it to the site bucket."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    site_bucket = os.environ.get("ONCA_SITE_BUCKET")
    window_days = int(os.environ.get("ONCA_FEED_WINDOW_DAYS", "14"))

    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}

    narratives = load_recent_narratives(digests_bucket, window_days)
    feed = build_feed(narratives)
    body = json.dumps(feed, ensure_ascii=False).encode("utf-8")

    published = None
    if site_bucket:
        try:
            boto3.client("s3").put_object(
                Bucket=site_bucket,
                Key=FEED_KEY,
                Body=body,
                ContentType="application/json",
                CacheControl="no-cache",
            )
            published = f"s3://{site_bucket}/{FEED_KEY}"
        except Exception as exc:  # pragma: no cover - publish is best-effort
            print(f"Warning: feed publish failed: {exc}")

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "status": "ok",
                "as_of": feed["as_of"],
                "feed_count": len(feed["feed"]),
                "entities": len(feed["entities"]),
                "published": published,
            }
        ),
    }
