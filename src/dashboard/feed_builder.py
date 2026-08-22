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

try:  # recompute legacy (factor-less) scores through the current blended model
    from src.synth.candidates import score_from_lenses
except Exception:  # pragma: no cover - degrade to stored score if synth import shifts
    score_from_lenses = None

NARRATIVES_PREFIX = "narratives/"
# Published to the static site bucket root; index.html fetches "feed.json".
FEED_KEY = "feed.json"

# Below this many narratives over the window, an industry module aggregates too
# little to feel worth an add-on subscription — flagged so we don't sell a thin
# feed. Zero narratives while entities are tracked is a coverage_gap (worse).
LOW_VOLUME_NARRATIVES = 3


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


def build_industry_volume(
    items: list[dict[str, Any]],
    industry_map: dict[str, list[str]] | None,
    industry_meta: dict[str, dict[str, Any]] | None,
    *,
    latest: str | None,
) -> list[dict[str, Any]]:
    """Per-industry narrative VOLUME + coverage over the window.

    Attributes each feed item to the industry module(s) of the entities it names
    (via ``industry_map``), counting each narrative once per touched industry.
    Answers the packaging question: does an industry add-on actually aggregate
    signals, or would a subscriber see an empty feed?

    - ``entities``: distinct tracked entities in this module (concentration).
    - ``narratives``/``alerts``/``peak_score``: signal volume in the window.
    - ``covered``: produced at least one narrative.
    - ``low_volume``: produced some, but below the worth-selling floor.
    - ``coverage_gap``: entities are tracked yet zero narratives surfaced — a
      module we'd bill for that went silent this window (a gap to investigate).
    """
    industry_map = industry_map or {}
    industry_meta = industry_meta or {}

    # Concentration: tracked entities per module (registry side).
    ent_count: dict[str, int] = {}
    for inds in industry_map.values():
        for slug in inds:
            ent_count[slug] = ent_count.get(slug, 0) + 1

    # Volume: narratives per module (narrative side).
    vol: dict[str, dict[str, Any]] = {}

    def bucket(slug: str) -> dict[str, Any]:
        return vol.setdefault(
            slug,
            {"narratives": 0, "narratives_latest": 0, "alerts": 0,
             "peak_score": 0.0, "active": set()},
        )

    for x in items:
        ents = set(x.get("entities") or [])
        if x.get("entity"):
            ents.add(x["entity"])
        touched: dict[str, set[str]] = {}
        for ent in ents:
            for slug in industry_map.get(ent, []):
                touched.setdefault(slug, set()).add(ent)
        for slug, slug_ents in touched.items():
            b = bucket(slug)
            b["narratives"] += 1
            if x["date"] and x["date"] == latest:
                b["narratives_latest"] += 1
            if x.get("is_alert"):
                b["alerts"] += 1
            b["peak_score"] = max(b["peak_score"], x.get("threat_score") or 0.0)
            b["active"].update(slug_ents)

    slugs = set(industry_meta) | set(ent_count) | set(vol)
    out: list[dict[str, Any]] = []
    for slug in slugs:
        meta = industry_meta.get(slug) or {}
        b = vol.get(slug) or {}
        narratives = int(b.get("narratives", 0))
        entities = ent_count.get(slug, 0)
        out.append(
            {
                "slug": slug,
                "display_name": meta.get("display_name") or slug.replace("-", " ").title(),
                "tier": meta.get("tier"),
                "entities": entities,
                "active_entities": len(b.get("active", ())),
                "narratives": narratives,
                "narratives_latest": int(b.get("narratives_latest", 0)),
                "alerts": int(b.get("alerts", 0)),
                "peak_score": round(float(b.get("peak_score", 0.0)), 1),
                "covered": narratives > 0,
                "low_volume": 0 < narratives < LOW_VOLUME_NARRATIVES,
                "coverage_gap": entities > 0 and narratives == 0,
            }
        )
    out.sort(key=lambda r: (r["narratives"], r["entities"], r["slug"]), reverse=True)
    return out


_FOCUS_LABELS = {"IPCA": "IPCA", "Selic": "Selic", "PIB Total": "PIB", "Câmbio": "Câmbio (R$/US$)"}


def _is_recent(iso_date: str | None, *, days: int) -> bool:
    try:
        return (dt.date.today() - dt.date.fromisoformat(str(iso_date)[:10])).days <= days
    except (TypeError, ValueError):
        return False


def build_macro(macro: dict[str, Any] | None) -> dict[str, Any]:
    """Turn the digest's macro slice into the standalone Macro-panel payload:
    current Selic + last Copom decision, and the Focus medians with WoW shifts.
    A recent Copom decision or a notable Focus shift becomes an alert card."""
    macro = macro or {}
    selic = macro.get("selic") or None
    focus = [f for f in (macro.get("focus") or []) if f.get("median") is not None]
    cards: list[dict[str, Any]] = []

    if selic:
        d = selic.get("last_decision") or {}
        recent = _is_recent(d.get("date"), days=12)
        arrow = {"alta": "↑", "baixa": "↓"}.get(d.get("direction"), "→")
        if d.get("direction") in ("alta", "baixa"):
            detail = (
                f"Copom {'elevou' if d['direction'] == 'alta' else 'reduziu'} a Selic "
                f"em {abs(int(d.get('bps') or 0))} bps ({d.get('previous')}% → {d.get('value')}%) "
                f"em {d.get('date')}."
            )
        else:
            detail = f"Copom manteve a Selic em {selic.get('current')}% (última mudança em {d.get('date')})."
        cards.append({
            "kind": "selic",
            "title": f"Selic {selic.get('current')}% a.a. {arrow}",
            "detail": detail,
            "is_alert": recent,
            "as_of": selic.get("as_of"),
        })

    # Focus: one card per indicator/year with a notable WoW shift (threshold by
    # indicator scale). PIB/Câmbio move on a larger scale than IPCA/Selic.
    thresh = {"IPCA": 0.05, "Selic": 0.05, "PIB Total": 0.1, "Câmbio": 0.03}
    for f in focus:
        delta = f.get("delta")
        if delta is None or abs(delta) < thresh.get(f["indicator"], 0.05):
            continue
        up = delta > 0
        cards.append({
            "kind": "focus",
            "title": f"Focus: {_FOCUS_LABELS.get(f['indicator'], f['indicator'])} {f['ref_year']} → {f['median']}%",
            "detail": (
                f"Mediana das expectativas {'subiu' if up else 'caiu'} "
                f"{abs(delta):+.2f} p.p. na semana (para {f['ref_year']})."
            ),
            "is_alert": False,
            "as_of": f.get("date"),
        })

    return {"selic": selic, "focus": focus, "cards": cards}


def build_feed(
    narratives: list[dict[str, Any]],
    *,
    generated_at: str | None = None,
    reviews: list[dict[str, Any]] | None = None,
    industry_map: dict[str, list[str]] | None = None,
    industry_meta: dict[str, dict[str, Any]] | None = None,
    macro: dict[str, Any] | None = None,
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
        # Legacy narratives (persisted before threat_factors) carry a stale,
        # saturated score from a superseded heuristic. Recompute through the current
        # blended model from their recorded lenses so they rank on one scale; new
        # narratives (which already have factors) are untouched. Non-destructive —
        # display only; auto-noops once legacy objects age out of the window.
        tfactors = n.get("threat_factors") or {}
        if tfactors or score_from_lenses is None or not n.get("lenses"):
            tscore = _score(n.get("threat_score"))
        else:
            tscore, tfactors = score_from_lenses(n.get("lenses"), bool(n.get("is_alert")))
        items.append(
            {
                "id": n.get("id"),
                # date = run_date (when surfaced) — drives sort/window/timeline;
                # data_date = source age, shown as "dados de". Legacy objects with
                # no run_date fall back to as_of for both.
                "date": (n.get("run_date") or n.get("as_of") or "")[:10],
                # run_at = BRT timestamp of the run that last changed this
                # narrative; card shows it as a time suffix (multiple runs/day).
                "run_at": n.get("run_at") or "",
                "data_date": (n.get("as_of") or n.get("run_date") or "")[:10],
                "kind": n.get("kind"),
                # ADR 003: narrative-type facets. Cross-sectional cards carry no
                # axis (implicitly "cross_sectional"); silence &c set them so the
                # UI can filter and flag inference. is_inference => label, not fact.
                "axis": n.get("axis"),
                "subject_type": n.get("subject_type"),
                "is_inference": bool(n.get("is_inference")),
                # direction distinguishes escalation/cooling on trajectory breaks;
                # None for axes without a polarity.
                "direction": n.get("direction"),
                "entity": n.get("entity"),
                "entity_label": display_label(n.get("entity"), n.get("kind")),
                "entities": n.get("entities") or [],
                "lenses": n.get("lenses") or [],
                "is_alert": bool(n.get("is_alert")),
                "threat_score": tscore,
                "threat_factors": tfactors,
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
    # "dados de" = newest underlying source date (data_date), independent of when
    # the run happened; can lag behind `latest` when a source (e.g. CVM) is stale.
    data_dates = sorted({x["data_date"] for x in items if x["data_date"]})
    data_as_of = data_dates[-1] if data_dates else latest

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

    imap = industry_map or {}
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
                # industry slugs this entity belongs to — lets the dashboard group
                # the entity monitor under each industry (fused coverage panel).
                "industries": sorted(imap.get(rec["entity"], [])),
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
        "as_of": data_as_of,
        "run_date": latest,
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
        # Per-industry volume + coverage (ADR 002 Phase B): drives module
        # packaging and flags add-ons that would aggregate too little.
        "industries": build_industry_volume(
            items, industry_map, industry_meta, latest=latest
        ),
        # Industry taxonomy for the review-queue pick control (slug -> label).
        "industry_options": [
            {"slug": s, "display_name": (m or {}).get("display_name") or s}
            for s, m in sorted((industry_meta or {}).items())
        ],
        # Pending entity-registry review proposals (ADR step 5), read-only surface.
        "reviews": reviews or [],
        # Standalone Macro panel: Copom/Selic + Focus (market-wide context).
        "macro": build_macro(macro),
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


def load_pending_reviews(*, limit: int = 50) -> list[dict[str, Any]]:
    """Read pending entity-registry review proposals and attach display labels.

    Best-effort and read-only: any failure (no table, no access) yields an empty
    list so the feed still publishes. Labels resolve entity_id/target_id slugs to
    display names so the dashboard can render a human-readable proposal.
    """
    if not os.environ.get("ONCA_ENTITIES_TABLE"):
        return []
    try:
        from src.synth import entity_registry

        names: dict[str, str] = {}

        def label(eid: str | None) -> str | None:
            if not eid:
                return None
            if eid not in names:
                ent = entity_registry.get_entity(eid)
                names[eid] = (ent or {}).get("display_name") or eid
            return names[eid]

        out: list[dict[str, Any]] = []
        for r in entity_registry.list_reviews(status="pending")[:limit]:
            out.append(
                {
                    "review_id": r.get("review_id"),
                    "kind": r.get("kind"),
                    "member": r.get("entity_id"),
                    "member_label": label(r.get("entity_id")),
                    "leader": r.get("target_id"),
                    "leader_label": label(r.get("target_id")),
                    "proposed": r.get("proposed"),
                    "reason": r.get("reason"),
                    "hint": r.get("hint"),
                    "confidence": r.get("confidence"),
                    "created_at": r.get("created_at"),
                }
            )
        return out
    except Exception as exc:  # pragma: no cover - best-effort, read-only
        print(f"Warning: load pending reviews failed: {exc}")
        return []


def load_industry_data() -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
    """Read the entity→industry map and the industry taxonomy from the registry.

    Best-effort and read-only: any failure (no table, no access) yields empty
    maps so the feed still publishes (industries section simply empty)."""
    if not os.environ.get("ONCA_ENTITIES_TABLE"):
        return {}, {}
    try:
        from src.synth import entity_registry

        return entity_registry.entity_industry_map(), dict(entity_registry.INDUSTRIES)
    except Exception as exc:  # pragma: no cover - best-effort, read-only
        print(f"Warning: load industry data failed: {exc}")
        return {}, {}


def _load_macro() -> dict[str, Any] | None:
    """Read the macro slice from the latest structured digest (best-effort)."""
    try:
        from src.synth import digest_io

        digest = digest_io.load_latest_digest_from_s3()
        return (digest or {}).get("macro")
    except Exception as exc:  # pragma: no cover - best-effort, read-only
        print(f"Warning: load macro slice failed: {exc}")
        return None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Build feed.json from recent narratives and publish it to the site bucket."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    site_bucket = os.environ.get("ONCA_SITE_BUCKET")
    window_days = int(os.environ.get("ONCA_FEED_WINDOW_DAYS", "14"))

    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}

    narratives = load_recent_narratives(digests_bucket, window_days)
    industry_map, industry_meta = load_industry_data()
    feed = build_feed(
        narratives,
        reviews=load_pending_reviews(),
        industry_map=industry_map,
        industry_meta=industry_meta,
        macro=_load_macro(),
    )
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
                "industries_covered": sum(1 for i in feed["industries"] if i["covered"]),
                "industry_coverage_gaps": [
                    i["slug"] for i in feed["industries"] if i["coverage_gap"]
                ],
                "reviews_pending": len(feed["reviews"]),
                "published": published,
            }
        ),
    }
