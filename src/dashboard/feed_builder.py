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
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import boto3

from src.dashboard.topics import topic_options, topics_of


def _json_default(o: Any) -> Any:
    """Registry-derived fields (e.g. ESG ise_b3_weight_pct) arrive as DynamoDB
    Decimal; feed.json is JSON, so serialize Decimal as a plain number."""
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")

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
# ADR 016 — the Entry Portal's scoped slice (entry-tier industries only). The Entry
# dashboard fetches THIS, never feed.json, so a higher-tier industry can't leak.
ENTRY_FEED_KEY = "feed.entry.json"

try:  # single source of truth for the entry-tier vertical set (ADR 016)
    from src.dashboard.tenant_config import ENTRY_INDUSTRIES
except Exception:  # pragma: no cover - keep the feed buildable if the module shifts
    ENTRY_INDUSTRIES = ("agri-funds", "betting", "consorcio", "crypto", "real-estate-funds")

# ADR 015 §3 — per-entity expansion MOMENTUM. A weighted COUNT (never a 0–1 index)
# of expansion-lens narratives per entity over the feed window: the fastest-cadence
# "who's moving into the market" signal. The lens identifiers are the ones narratives
# actually carry (`n["lenses"]`), from src.synth.candidates.LENS_WEIGHT — new-entrant
# licensing (`entrants`), public/securities offerings (`ofertas`), new fund/class
# registrations (`funds`), daily fund flows (`inf_diario`), and Pix key activity
# (`pix`). New-entrant licensing + offerings are the strongest expansion tells, so they
# weigh 2; a routine fund/flow/Pix registration weighs 1. A narrative counts once, at
# the max weight among its expansion lenses (so a multi-lens card isn't double-counted).
# NOTE: hiring/LinkedIn velocity is deliberately NOT included — there is no such
# ingester (CLAUDE.md: LinkedIn only via a licensed aggregator, not built).
EXPANSION_LENS_WEIGHTS: dict[str, int] = {
    "entrants": 2,
    "ofertas": 2,
    "funds": 1,
    "inf_diario": 1,
    "pix": 1,
}
EXPANSION_LENSES = frozenset(EXPANSION_LENS_WEIGHTS)


def _momentum_weight(lenses: list[str] | None) -> int:
    """Expansion weight a single narrative contributes: the max weight over its
    expansion lenses (0 if it carries none), so a card is counted once, not per lens."""
    return max(
        (EXPANSION_LENS_WEIGHTS[l] for l in (lenses or []) if l in EXPANSION_LENS_WEIGHTS),
        default=0,
    )


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


def subject_label(n: dict[str, Any]) -> str:
    """Headline for a card — the entity, or the non-entity subject (ADR 003 Wave 1
    theme/instrument/set axes) so entity-less cards still title legibly."""
    if n.get("incident_title"):
        return f"Incidente · {n['incident_title']}"
    if n.get("pair_label"):
        return f"Relacional · {n['pair_label']}"
    if n.get("entity"):
        return display_label(n.get("entity"), n.get("kind"))
    if n.get("theme_display"):
        return f"Tema · {n['theme_display']}"
    if n.get("instrument_label"):
        return f"Regulatório · {n['instrument_label']}"
    if n.get("cohort_label"):
        return f"Setor · {n['cohort_label']}"
    if n.get("hub"):
        return f"Ecossistema · {n['hub']}"
    return display_label(None, n.get("kind"))


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


def _project_item(n: dict[str, Any]) -> dict[str, Any]:
    """Project one narrative (or thread card) into a feed item — one shared schema."""
    # Legacy narratives (persisted before threat_factors) carry a stale, saturated
    # score from a superseded heuristic. Recompute through the current blended model
    # from their recorded lenses so they rank on one scale; new narratives (which
    # already have factors) are untouched. Non-destructive — display only.
    tfactors = n.get("threat_factors") or {}
    if tfactors or score_from_lenses is None or not n.get("lenses"):
        tscore = _score(n.get("threat_score"))
    else:
        tscore, tfactors = score_from_lenses(n.get("lenses"), bool(n.get("is_alert")))
    return {
        "id": n.get("id"),
        # date = run_date (when surfaced) — drives sort/window/timeline; data_date =
        # source age ("dados de"). Legacy objects with no run_date fall back to as_of.
        "date": (n.get("run_date") or n.get("as_of") or "")[:10],
        # run_at = BRT timestamp of the run that last changed this narrative.
        "run_at": n.get("run_at") or "",
        "data_date": (n.get("as_of") or n.get("run_date") or "")[:10],
        "kind": n.get("kind"),
        # ADR 003: narrative-type facets. Cross-sectional cards carry no axis
        # (implicitly "cross_sectional"); derived axes set them so the UI can filter
        # and flag inference. is_inference => label, not fact.
        "axis": n.get("axis"),
        "subject_type": n.get("subject_type"),
        "is_inference": bool(n.get("is_inference")),
        "direction": n.get("direction"),
        "entity": n.get("entity"),
        "entity_label": display_label(n.get("entity"), n.get("kind")),
        # subject_label headlines non-entity axes (theme/instrument/set/incident) so
        # they don't render blank; swot_hint + deadline carry Wave 1 axis payload.
        "subject_label": subject_label(n),
        "swot_hint": n.get("swot_hint"),
        "deadline": n.get("deadline"),
        "days_to_deadline": n.get("days_to_deadline"),
        # thread payload (Wave 2): status + development count + reg-lifecycle stage.
        "status": n.get("status"),
        # ADR 009: regulatory change intelligence — Phase A change list + §3 LLM record.
        "n_changes": n.get("n_changes"),
        "change_record": n.get("change_record"),
        "n_developments": n.get("n_developments"),
        "latest_dev_id": n.get("latest_dev_id"),      # thread: the card its latest dev shows
        "latest_dev_date": n.get("latest_dev_date"),
        "current_stage": n.get("current_stage"),
        "pattern": n.get("pattern"),  # behavioral axis: drumbeat / multi_front
        "relation": n.get("relation"),  # relational axis: co_mention / convergence / dispute
        "horizon_days": n.get("horizon_days"),  # predictive axis: forecast window
        "hub": n.get("hub"),  # ecosystem axis: the infrastructure hub
        "n_dependents": n.get("n_dependents"),  # ecosystem axis: exposed count
        "entities": n.get("entities") or [],
        # A card's self-declared industries (e.g. regulatory instrument cards, #51 nexus).
        # Entity-bearing cards get this overwritten by the ADR-017 denorm below; subject
        # cards with no entities keep their own affected cohort.
        "industries": n.get("industries") or [],
        "lenses": n.get("lenses") or [],
        # ADR #34 Phase 2: coarse topic rollup (from lenses+axis) for the dashboard
        # topic filter + agent grounding boost. Derived here so no synth/backfill.
        "topics": topics_of(n),
        "is_alert": bool(n.get("is_alert")),
        "threat_score": tscore,
        "threat_factors": tfactors,
        "threat_score_note": n.get("threat_score_note"),
        "narrative": n.get("narrative") or "",
        "citations": [c for c in (n.get("citations") or []) if isinstance(c, dict)],
        "source_ids": n.get("source_ids") or [],
        "mode": n.get("mode"),
        # ADR 006: frameworks this narrative updated (evidence-matched) -> on-card strips.
        "fw_updates": n.get("fw_updates") or {},
    }


# ADR 016 — the Entry Portal is scoped on TWO dimensions: industry AND depth. These are
# the DEEP, derived analytical axes (ADR 003 Waves 2–3: inferences, not public-filing
# facts) that must not surface in the shallow Entry feed.
_DEEP_AXES = {"relational", "predictive", "ecosystem", "operatives", "operative", "behavioral"}


def _is_shallow_card(c: dict[str, Any]) -> bool:
    """A shallow public-filing card (fact), not a derived-axis inference. Entry keeps
    only these (issue #44); the deep axes (relational/predictive/ecosystem/behavioral/
    operatives) are dropped from the entry slice."""
    if c.get("is_inference"):
        return False
    if str(c.get("axis") or "").lower() in _DEEP_AXES:
        return False
    if c.get("relation") or c.get("horizon_days") is not None or c.get("hub") or c.get("pattern"):
        return False
    return True


def _regulatory_coverage() -> dict[str, Any]:
    """#2 CVM/BCB regulated-segment coverage scan (best-effort; {} on failure)."""
    try:
        from src.ingest import reg_coverage
        return reg_coverage.coverage_report()
    except Exception as exc:  # pragma: no cover - best-effort, never break the feed
        print(f"Warning: regulatory coverage unavailable: {exc}")
        return {}


def _build_groups(entity_attrs: dict[str, Any] | None) -> dict[str, list[str]]:
    """ADR 017 corporate groups: {parent_id: sorted[child sub-entity ids]} from the
    entities' `parent` links. Only parents that are themselves tracked are kept."""
    attrs = entity_attrs or {}
    groups: dict[str, list[str]] = {}
    for eid, a in attrs.items():
        p = (a or {}).get("parent")
        if p and p in attrs:
            groups.setdefault(str(p), []).append(eid)
    return {p: sorted(kids) for p, kids in groups.items()}


def build_feed(
    narratives: list[dict[str, Any]],
    *,
    generated_at: str | None = None,
    reviews: list[dict[str, Any]] | None = None,
    industry_map: dict[str, list[str]] | None = None,
    industry_meta: dict[str, dict[str, Any]] | None = None,
    macro: dict[str, Any] | None = None,
    swot: dict[str, Any] | None = None,
    tows_curated: dict[str, list[dict[str, Any]]] | None = None,
    porter_curated: dict[str, list[dict[str, Any]]] | None = None,
    pestle_curated: dict[str, list[dict[str, Any]]] | None = None,
    ansoff_curated: dict[str, list[dict[str, Any]]] | None = None,
    bcg_curated: dict[str, list[dict[str, Any]]] | None = None,
    four_corners_curated: dict[str, list[dict[str, Any]]] | None = None,
    seven_s_curated: dict[str, list[dict[str, Any]]] | None = None,
    swot_proposals: list[dict[str, Any]] | None = None,
    graph_proposals: list[dict[str, Any]] | None = None,
    thread_cards: list[dict[str, Any]] | None = None,
    distress: list[dict[str, Any]] | None = None,
    entity_attrs: dict[str, dict[str, Any]] | None = None,
    coverage_gaps: list[dict[str, Any]] | None = None,
    reputation: list[dict[str, Any]] | None = None,
    financials: list[dict[str, Any]] | None = None,
    market_share: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Pure aggregation: narratives -> feed payload. No I/O.

    Feed items are sorted newest-date-first then by threat_score desc. Entity
    timelines carry per-date count + peak score for compact sparklines. KPIs are
    scoped to the latest date, except entities/sources which span the window.
    """
    items: list[dict[str, Any]] = [
        _project_item(n) for n in narratives if isinstance(n, dict)
    ]
    # Wave 2 incident threads: grouping cards over signals ALREADY counted above.
    # They join the feed (filterable/sortable) but are kept out of KPI/timeline
    # aggregation below so they never double-count a story's daily developments.
    thread_items = [_project_item(c) for c in (thread_cards or []) if isinstance(c, dict)]
    # Drop a grouping/thread card when its latest development is a card ALREADY
    # standalone in the feed (same id + date) — else the thread re-displays today's
    # daily fusion narrative verbatim and reads as a duplicate. A thread whose latest
    # development is NOT independently in the feed (older than the window, or a card
    # type not surfaced) still adds genuinely new content and is kept.
    present = {(x["id"], x["date"]) for x in items if x.get("id")}
    thread_items = [
        t for t in thread_items
        if not (t.get("latest_dev_id")
                and (t["latest_dev_id"], t.get("latest_dev_date")) in present)
    ]

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
            ent, {"entity": ent, "label": x["entity_label"], "by_date": {}, "momentum": 0}
        )
        day = rec["by_date"].setdefault(
            x["date"], {"date": x["date"], "count": 0, "max_score": 0.0}
        )
        day["count"] += 1
        day["max_score"] = max(day["max_score"], x["threat_score"])
        # ADR 015 §3: weighted count of this entity's expansion-lens narratives.
        rec["momentum"] += _momentum_weight(x["lenses"])

    imap = industry_map or {}
    share_map = market_share or {}
    entities: list[dict[str, Any]] = []
    for rec in by_entity.values():
        timeline = [rec["by_date"][d] for d in sorted(rec["by_date"])]
        entities.append(
            {
                "entity": rec["entity"],
                "label": rec["label"],
                "timeline": timeline,
                "peak_score": max((t["max_score"] for t in timeline), default=0.0),
                # ADR 015 §3: per-entity expansion momentum (weighted count, int, default 0).
                "momentum": int(rec.get("momentum", 0)),
                # ADR 015 §3: IF.data market share (%) if the entity resolved to an
                # IF.data row, else None — never an invented number.
                "market_share_pct": share_map.get(rec["entity"]),
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

    # ADR 017: denormalize each card's industries (union of its entities' registry
    # industries) onto the card, so the Entry fork, the per-tenant read boundary, and
    # the dashboard toggles all scope by the CARD — exact once a conglomerate's lines
    # are sub-entities (a sub-entity card then carries its single lower industry).
    def _card_industries(c: dict[str, Any]) -> list[str]:
        s: set[str] = set()
        for e in [c.get("entity"), *(c.get("entities") or [])]:
            if e:
                s.update(imap.get(e, []))
        if s:
            return sorted(s)
        # No entity to denormalize from — preserve a card's SELF-declared industries
        # (regulatory instrument cards carry their affected cohort, #51 nexus), so the
        # denorm doesn't wipe a subject card off every industry tab.
        return sorted({str(i).strip().lower() for i in (c.get("industries") or []) if i})

    for c in items + thread_items:
        c["industries"] = _card_industries(c)

    # Issue #7 Phase 2: ground each issuer's BCG position in real financials (revenue
    # growth × relative share vs the industry leader) — a hard-number anchor for the
    # BCG framework, computed against the current industry map.
    if financials:
        from src.synth import bcg as _bcg

        financials = _bcg.position_from_financials(financials, imap)

    # Regulatory INSTRUMENT cards use a date-independent id (regulatory-<instrument>),
    # so a re-emission on a new date leaves the older copy in the window — the same rule
    # twice, the stale copy carrying pre-fix citations. Collapse instrument-subject cards
    # to their latest date per id (entity cards keep every date — each is a distinct daily
    # event, i.e. a timeline, not a duplicate).
    def _dedup_instrument_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
        best: dict[str, dict[str, Any]] = {}
        out: list[dict[str, Any]] = []
        for c in cards:
            if c.get("subject_type") != "instrument":
                out.append(c)
                continue
            cid = c.get("id")
            prev = best.get(cid)
            if prev is None or (c.get("date") or "") > (prev.get("date") or ""):
                best[cid] = c
        return out + list(best.values())

    # Merge incident threads into the displayed feed (after KPI/entity aggregation).
    feed_items = sorted(
        _dedup_instrument_cards(items + thread_items),
        key=lambda x: (x["date"], x["threat_score"]), reverse=True,
    )

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
        "feed": feed_items,
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
        # #2 CVM/BCB coverage scan: which regulated segments we ingest as signals vs.
        # maintain an entity roster for (registry sync). The gap/signal-only list is the
        # #14 Official-Registry-Sync roadmap. A durable, measured artifact, not a one-off.
        "regulatory_coverage": _regulatory_coverage(),
        # ADR #34 Phase 2: topic filter options (only topics present in the feed).
        "topic_options": topic_options(feed_items),

        # Pending entity-registry review proposals (ADR step 5), read-only surface.
        "reviews": reviews or [],
        # Standalone Macro panel: Copom/Selic + Focus (market-wide context).
        "macro": build_macro(macro),
        # ADR 004 step 2: per-entity SWOT belief store (compact index) — the war
        # room renders a S/W/O/T panel for the selected entity. {} when absent.
        "swot": (swot or {}).get("entities", {}) if swot else {},
        # ADR 006: curated TOWS postures, grouped by entity.
        "tows": tows_curated or {},
        # ADR 006: curated Porter assessments, grouped by entity.
        "porter": porter_curated or {},
        # ADR 006: curated PESTLE/Ansoff/BCG/Four Corners/7S, grouped by entity.
        "pestle": pestle_curated or {},
        "ansoff": ansoff_curated or {},
        "bcg": bcg_curated or {},
        "four_corners": four_corners_curated or {},
        "seven_s": seven_s_curated or {},
        # ADR 004 step 3: pending reconcile proposals (contradict/new bullets from the
        # LLM stance loop) — surfaced read-only for the review queue (never auto-applied).
        "swot_proposals": [
            p for p in (swot_proposals or []) if p.get("status", "pending") == "pending"
        ],
        # ADR 003 Wave 3: pending relationship-graph proposals (relational convergence/
        # dispute + operative common-control/person). Review-gated, never auto-published
        # — surfaced read-only until the Phase C vetting UI.
        "graph_proposals": [
            p for p in (graph_proposals or []) if p.get("status", "pending") == "pending"
        ],
        # Option A (2026-08-25): entity-tagged corporate distress (RJ/falência) mined
        # from news + persisted. Read-only list, most-recent first. [] when absent.
        "distress": distress or [],
        # Curated per-entity classification attributes (ownership nature, compliance
        # certifications, ticker, industries) — queryable entity facts for the agent
        # + dashboard. Keyed by entity_id. Covers the WHOLE registry, not just
        # narrative-bearing entities.
        "entity_attrs": entity_attrs or {},
        # ADR 017: corporate groups {parent_id: [child sub-entity ids]} — a tier-1
        # entity's lower-industry lines. Drives the dashboard's "show group" opt-in
        # (fold a parent's children's cards into its view on demand).
        "groups": _build_groups(entity_attrs),
        # ADR-014 coverage-gap loop: unanswered in-domain questions awaiting triage/
        # remediation. Read-only list (open + proposed), most-frequent first.
        "coverage_gaps": coverage_gaps or [],
        # Reclame Aqui consumer-reputation snapshots (#31), worst-score first.
        "reputation": reputation or [],
        # Issue #7 / ADR 011 stage 6: per-issuer financial statement metrics (CVM DFP/ITR)
        # — revenue/net income/assets/equity + derived net margin, YoY growth, leverage.
        # Keyed grounding for the agent + a financial lens on the frameworks.
        "financials": financials or [],
    }


def scope_feed_to_modules(feed: dict[str, Any], modules: Any) -> dict[str, Any]:
    """Server-authoritative per-tenant projection (issue #48 / ADR 016 SaaS): the full
    feed scoped to a tenant's licensed ``modules`` (industries). Unlike the Entry fork it
    keeps full depth, industry labels, and corporate groups — and, per the ADR-017 tier-1
    opt-in, folds in the children of any IN-SCOPE parent (a tenant entitled to a
    conglomerate is entitled to its group). Fail closed: empty modules ⇒ empty feed.

    Identical delivery for the two tier-1 planes (your framing): SaaS serves this from the
    shared multi-tenant endpoint; an AWS Marketplace tenant runs the same projection in its
    own account. The read boundary is the same either way.
    """
    keep = {str(i).strip().lower() for i in (modules or [])}
    attrs = feed.get("entity_attrs") or {}
    ent_ind = {
        e: {str(i).strip().lower() for i in ((a or {}).get("industries") or [])}
        for e, a in attrs.items()
    }
    scoped = {e for e, inds in ent_ind.items() if inds & keep}
    if not keep:
        scoped = set()
    # ADR 017 tier-1 opt-in: add descendants of any in-scope parent (recursive).
    groups = feed.get("groups") or {}
    stack = [e for e in scoped if e in groups]
    while stack:
        for child in groups.get(stack.pop(), []):
            if child not in scoped:
                scoped.add(child)
                if child in groups:
                    stack.append(child)

    def card_ok(c: dict[str, Any]) -> bool:
        inds = c.get("industries")
        if inds is not None and {str(i).strip().lower() for i in inds} & keep:
            return True
        for e in [c.get("entity"), *(c.get("entities") or [])]:
            if e and e in scoped:  # covers group children (a licensed parent's lines)
                return True
        return False

    def row_ok(r: dict[str, Any]) -> bool:
        e = r.get("entity") or r.get("entity_id")
        return bool(e and e in scoped)

    def by_key(d: Any) -> dict[str, Any]:
        return {e: v for e, v in (d or {}).items() if e in scoped}

    feed_items = [c for c in (feed.get("feed") or []) if card_ok(c)]
    entities = [r for r in (feed.get("entities") or []) if r.get("entity") in scoped]
    latest = feed.get("run_date")
    latest_items = [c for c in feed_items if c.get("date") == latest]
    sources = {
        s for c in feed_items for cit in (c.get("citations") or []) if (s := _source_of(cit))
    }
    out = dict(feed)
    out.update(
        {
            "scoped_modules": sorted(keep),
            "feed": feed_items,
            "entities": entities,
            "entity_attrs": by_key(attrs),
            "groups": {p: ks for p, ks in groups.items() if p in scoped},
            "kpis": {
                "narratives_latest": len(latest_items),
                "alerts_latest": sum(1 for c in latest_items if c.get("is_alert")),
                "entities_tracked": len(entities),
                "sources": len(sources),
                "narratives_total": len(feed_items),
            },
            "industries": [i for i in (feed.get("industries") or []) if i.get("slug") in keep],
            "industry_options": [
                o for o in (feed.get("industry_options") or []) if o.get("slug") in keep
            ],
            "topic_options": topic_options(feed_items),
            "swot": by_key(feed.get("swot")),
            "tows": by_key(feed.get("tows")),
            "porter": by_key(feed.get("porter")),
            "pestle": by_key(feed.get("pestle")),
            "ansoff": by_key(feed.get("ansoff")),
            "bcg": by_key(feed.get("bcg")),
            "four_corners": by_key(feed.get("four_corners")),
            "seven_s": by_key(feed.get("seven_s")),
            "reviews": [r for r in (feed.get("reviews") or []) if row_ok(r)],
            "swot_proposals": [r for r in (feed.get("swot_proposals") or []) if row_ok(r)],
            "graph_proposals": [r for r in (feed.get("graph_proposals") or []) if row_ok(r)],
            "distress": [r for r in (feed.get("distress") or []) if row_ok(r)],
            "reputation": [r for r in (feed.get("reputation") or []) if row_ok(r)],
            "coverage_gaps": [r for r in (feed.get("coverage_gaps") or []) if row_ok(r)],
            "financials": [r for r in (feed.get("financials") or []) if row_ok(r)],
            "integrity": {"findings": [], "counts": {}, "total": 0},  # operator-only
            "regulatory_coverage": {},                                # operator-only (#2)
        }
    )
    return out


def derive_entry_feed(
    feed: dict[str, Any], *, industries: Any = ENTRY_INDUSTRIES
) -> dict[str, Any]:
    """ADR 016 "fork the feed, not the pipeline": a strict projection of the full feed
    down to the entry-tier industries ONLY. Not a second sensor — every entity-bearing
    section is filtered to entities in the entry industries, so a higher-tier industry
    (or an entity outside the entry set) can never appear in the Entry Portal. KPIs and
    topic options are recomputed for the slice. Fail closed: a row we can't attribute to
    an entry entity is dropped rather than carried over.

    Depth note: the entry industries are themselves the shallow public-filing verticals,
    so scoping by industry already yields the shallow slice; a stricter lens/axis depth
    filter (drop relational/predictive/operative cards) is a documented follow-on.
    """
    keep = {str(i).strip().lower() for i in (industries or [])}
    attrs = feed.get("entity_attrs") or {}
    ent_ind = {
        e: {str(i).strip().lower() for i in ((a or {}).get("industries") or [])}
        for e, a in attrs.items()
    }
    entry_entities = {e for e, inds in ent_ind.items() if inds & keep}

    def card_ok(c: dict[str, Any]) -> bool:
        # Prefer the card's denormalized industries (ADR 017); fall back to entity
        # membership for any card built before the field existed.
        inds = c.get("industries")
        if inds is not None:
            return bool({str(i).strip().lower() for i in inds} & keep)
        for e in [c.get("entity"), *(c.get("entities") or [])]:
            if e and e in entry_entities:
                return True
        return False

    def row_ok(r: dict[str, Any]) -> bool:
        e = r.get("entity") or r.get("entity_id")
        return bool(e and e in entry_entities)

    def by_entity_key(d: Any) -> dict[str, Any]:
        return {e: v for e, v in (d or {}).items() if e in entry_entities}

    def scrub_industries(inds: Any) -> list[str]:
        # a multi-industry entity can sit in an entry AND a higher tier; the Entry
        # dashboard must show ONLY its entry industries, never a higher-tier chip.
        return sorted(i for i in (inds or []) if str(i).strip().lower() in keep)

    # Entry keeps only cards that are BOTH in an entry industry (card_ok) AND shallow
    # public-filing facts (issue #44) — the deep derived axes never reach the Entry feed.
    feed_items = [
        {**c, "industries": scrub_industries(c.get("industries"))}
        for c in (feed.get("feed") or [])
        if card_ok(c) and _is_shallow_card(c)
    ]
    entities = [
        {**r, "industries": scrub_industries(r.get("industries"))}
        for r in (feed.get("entities") or [])
        if r.get("entity") in entry_entities
    ]
    latest = feed.get("run_date")
    latest_items = [c for c in feed_items if c.get("date") == latest]
    sources = {
        s for c in feed_items for cit in (c.get("citations") or []) if (s := _source_of(cit))
    }

    out = dict(feed)  # inherit generated_at/as_of/dates/macro/etc, then override slices
    out.update(
        {
            "tier": "entry",
            "feed": feed_items,
            "entities": entities,
            "entity_attrs": {
                e: {**a, "industries": scrub_industries((a or {}).get("industries"))}
                for e, a in attrs.items()
                if e in entry_entities
            },
            # tier-1 parents aren't in Entry, so the corporate groups collapse to {}.
            "groups": {},
            "kpis": {
                "narratives_latest": len(latest_items),
                "alerts_latest": sum(1 for c in latest_items if c.get("is_alert")),
                "entities_tracked": len(entities),
                "sources": len(sources),
                "narratives_total": len(feed_items),
            },
            "industries": [i for i in (feed.get("industries") or []) if i.get("slug") in keep],
            "industry_options": [
                o for o in (feed.get("industry_options") or []) if o.get("slug") in keep
            ],
            "topic_options": topic_options(feed_items),
            # per-entity framework/belief stores — scope to entry entities
            "swot": by_entity_key(feed.get("swot")),
            "tows": by_entity_key(feed.get("tows")),
            "porter": by_entity_key(feed.get("porter")),
            "pestle": by_entity_key(feed.get("pestle")),
            "ansoff": by_entity_key(feed.get("ansoff")),
            "bcg": by_entity_key(feed.get("bcg")),
            "four_corners": by_entity_key(feed.get("four_corners")),
            "seven_s": by_entity_key(feed.get("seven_s")),
            # entity-attributed lists — keep only rows bound to an entry entity
            "reviews": [r for r in (feed.get("reviews") or []) if row_ok(r)],
            "swot_proposals": [r for r in (feed.get("swot_proposals") or []) if row_ok(r)],
            "graph_proposals": [r for r in (feed.get("graph_proposals") or []) if row_ok(r)],
            "distress": [r for r in (feed.get("distress") or []) if row_ok(r)],
            "reputation": [r for r in (feed.get("reputation") or []) if row_ok(r)],
            "coverage_gaps": [r for r in (feed.get("coverage_gaps") or []) if row_ok(r)],
            "financials": [r for r in (feed.get("financials") or []) if row_ok(r)],
            "integrity": {"findings": [], "counts": {}, "total": 0},  # operator-only
            "regulatory_coverage": {},                                # operator-only (#2)
        }
    )
    return out


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


def load_entity_attributes() -> dict[str, dict[str, Any]]:
    """Per-entity classification attrs (ownership/certifications/ticker) from the
    registry, best-effort. {} when the table is unavailable."""
    if not os.environ.get("ONCA_ENTITIES_TABLE"):
        return {}
    try:
        from src.synth import entity_registry

        return entity_registry.list_entity_attributes()
    except Exception as exc:  # pragma: no cover - best-effort, read-only
        print(f"Warning: load entity attributes failed: {exc}")
        return {}


def _load_macro() -> dict[str, Any] | None:
    """Read the macro slice from the latest structured digest (best-effort)."""
    try:
        from src.synth import digest_io

        digest = digest_io.load_latest_digest_from_s3()
        return (digest or {}).get("macro")
    except Exception as exc:  # pragma: no cover - best-effort, read-only
        print(f"Warning: load macro slice failed: {exc}")
        return None


def _load_threads(digests_bucket: str) -> list[dict[str, Any]]:
    """Read incident-thread feed cards (ADR 003 Wave 2), best-effort. [] if absent."""
    try:
        from src.synth import threads

        body = boto3.client("s3").get_object(
            Bucket=digests_bucket, Key=threads.INDEX_KEY
        )["Body"].read()
        return json.loads(body.decode("utf-8")).get("cards", [])
    except Exception as exc:  # pragma: no cover - best-effort, read-only
        print(f"Warning: load thread index failed: {exc}")
        return []


def _load_reg_lifecycles(digests_bucket: str) -> list[dict[str, Any]]:
    """Read regulatory-lifecycle thread cards (ADR 003 Wave 2), best-effort. [] if absent."""
    try:
        from src.synth import regulatory

        body = boto3.client("s3").get_object(
            Bucket=digests_bucket, Key=regulatory.REG_LIFECYCLE_INDEX_KEY
        )["Body"].read()
        return json.loads(body.decode("utf-8")).get("cards", [])
    except Exception as exc:  # pragma: no cover - best-effort, read-only
        print(f"Warning: load reg-lifecycle index failed: {exc}")
        return []


def _load_financials(digests_bucket: str) -> list[dict[str, Any]]:
    """Read the per-issuer financial-statement store (issue #7), best-effort. [] if absent."""
    try:
        from src.ingest import cvm_financials

        return cvm_financials.load_index(digests_bucket)
    except Exception as exc:  # pragma: no cover - best-effort, read-only
        print(f"Warning: load financials index failed: {exc}")
        return []


def _load_distress(digests_bucket: str) -> list[dict[str, Any]]:
    """Read the entity-tagged distress store (option A), best-effort. [] if absent."""
    try:
        from src.synth import distress

        idx = distress.load_index(digests_bucket)
        return distress.list_records(idx)
    except Exception as exc:  # pragma: no cover - best-effort, read-only
        print(f"Warning: load distress index failed: {exc}")
        return []


def _load_reputation(digests_bucket: str) -> list[dict[str, Any]]:
    """Read the consumer-reputation stores (#31): BCB complaints ranking (official)
    + Reclame Aqui (when an authorized feed is configured). Best-effort."""
    out: list[dict[str, Any]] = []
    try:
        from src.ingest import bcb_reclamacoes

        out += bcb_reclamacoes.list_records(bcb_reclamacoes.load_index(digests_bucket))
    except Exception as exc:  # pragma: no cover - best-effort, read-only
        print(f"Warning: load BCB reclamações store failed: {exc}")
    try:
        from src.ingest import reclame_aqui

        out += reclame_aqui.list_records(reclame_aqui.load_index(digests_bucket))
    except Exception as exc:  # pragma: no cover - best-effort, read-only
        print(f"Warning: load Reclame Aqui store failed: {exc}")
    return out


def _load_market_share(digests_bucket: str) -> dict[str, float]:
    """Read the IF.data market-share store (ADR 015 §3) as {entity_id: share_pct},
    best-effort. {} when absent so entities emit market_share_pct=null."""
    try:
        from src.ingest import bcb_ifdata

        return bcb_ifdata.share_by_entity(bcb_ifdata.load_index(digests_bucket))
    except Exception as exc:  # pragma: no cover - best-effort, read-only
        print(f"Warning: load IF.data market-share store failed: {exc}")
        return {}


def _load_coverage_gaps(digests_bucket: str) -> list[dict[str, Any]]:
    """Read the coverage-gap store (ADR-014), best-effort. [] if absent."""
    try:
        from src.synth import coverage

        idx = coverage.load_index(digests_bucket)
        return coverage.list_open(idx)
    except Exception as exc:  # pragma: no cover - best-effort, read-only
        print(f"Warning: load coverage gaps failed: {exc}")
        return []


def _load_relational(digests_bucket: str) -> list[dict[str, Any]]:
    """Read factual relational (co_mention) cards (ADR 003 Wave 3), best-effort. [] if absent."""
    try:
        from src.synth import relational

        body = boto3.client("s3").get_object(
            Bucket=digests_bucket, Key=relational.GRAPH_INDEX_KEY
        )["Body"].read()
        return json.loads(body.decode("utf-8")).get("cards", [])
    except Exception as exc:  # pragma: no cover - best-effort, read-only
        print(f"Warning: load relational cards failed: {exc}")
        return []


def _load_graph_proposals(digests_bucket: str) -> list[dict[str, Any]]:
    """Read pending graph proposals: relational edges + operative persons (Wave 3)."""
    out: list[dict[str, Any]] = []
    try:
        from src.synth import operatives, relational

        s3 = boto3.client("s3")
        for key in (relational.RELATIONAL_PROPOSALS_KEY,
                    operatives.PERSON_PROPOSALS_KEY):
            try:
                body = s3.get_object(Bucket=digests_bucket, Key=key)["Body"].read()
                out.extend(json.loads(body.decode("utf-8")).get("proposals", []))
            except Exception:
                continue
    except Exception as exc:  # pragma: no cover - best-effort, read-only
        print(f"Warning: load graph proposals failed: {exc}")
    return out


def _load_swot_proposals(digests_bucket: str) -> list[dict[str, Any]]:
    """Read pending SWOT proposals — reconcile (ADR 004 step 3), cold-start seeds
    (step 4), and staleness re-reviews (belief maintenance). All are review-gated and
    render in the same war-room panel."""
    out: list[dict[str, Any]] = []
    s3 = boto3.client("s3")
    from src.synth import (ansoff, bcg, four_corners, pestle, porter,
                           seven_s, swot_maintenance, swot_reconcile, swot_seed, tows)

    for key in (swot_reconcile.PROPOSALS_KEY, swot_seed.SEED_PROPOSALS_KEY,
                swot_maintenance.MAINTENANCE_PROPOSALS_KEY, tows.TOWS_PROPOSALS_KEY,
                porter.PORTER_PROPOSALS_KEY, pestle.PESTLE_PROPOSALS_KEY,
                ansoff.ANSOFF_PROPOSALS_KEY, bcg.BCG_PROPOSALS_KEY,
                four_corners.FOUR_CORNERS_PROPOSALS_KEY, seven_s.SEVEN_S_PROPOSALS_KEY):
        try:
            body = s3.get_object(Bucket=digests_bucket, Key=key)["Body"].read()
            out.extend(json.loads(body.decode("utf-8")).get("proposals", []))
        except Exception:  # pragma: no cover - best-effort, read-only (may be absent)
            continue
    return out


def _load_swot(digests_bucket: str) -> dict[str, Any] | None:
    """Read the compact SWOT belief index (ADR 004 step 2), best-effort. None if absent."""
    try:
        from src.synth import swot_store

        body = boto3.client("s3").get_object(
            Bucket=digests_bucket, Key=swot_store.INDEX_KEY
        )["Body"].read()
        return json.loads(body.decode("utf-8"))
    except Exception as exc:  # pragma: no cover - best-effort, read-only
        print(f"Warning: load SWOT index failed: {exc}")
        return None


def _load_tows_curated(digests_bucket: str) -> dict[str, list[dict[str, Any]]]:
    """ADR 006: load curated TOWS beliefs from swot/curated.json, grouped by entity."""
    try:
        from src.synth import swot_store

        body = boto3.client("s3").get_object(
            Bucket=digests_bucket, Key=swot_store.CURATED_KEY
        )["Body"].read()
        data = json.loads(body.decode("utf-8"))
        retired = {r.get("target_bullet_id") for r in data.get("retirements", [])
                   if r.get("target_bullet_id")}
        by_ent: dict[str, list[dict[str, Any]]] = {}
        for b in data.get("bullets", []):
            if b.get("framework") != "tows":
                continue
            if b.get("id") in retired:
                continue
            ent = b.get("entity")
            if ent:
                by_ent.setdefault(ent, []).append({
                    "dimension": b.get("dimension"),
                    "text": b.get("text"),
                    # the drafter's own confidence (for the dashboard heatmap);
                    # legacy bullets without it fall back to the curated pin.
                    "confidence": b.get("confidence", swot_store.CURATED_CONFIDENCE),
                    "status": "active",
                    "origin": b.get("origin"),
                })
        return by_ent
    except Exception:
        return {}


def _load_porter_curated(digests_bucket: str) -> dict[str, list[dict[str, Any]]]:
    """ADR 006: load curated Porter beliefs from swot/curated.json, grouped by entity."""
    return _load_fw_curated(digests_bucket, "porter")


def _load_fw_curated(digests_bucket: str, framework: str) -> dict[str, list[dict[str, Any]]]:
    """ADR 006: load curated beliefs for a framework from swot/curated.json, grouped by entity."""
    try:
        from src.synth import swot_store

        body = boto3.client("s3").get_object(
            Bucket=digests_bucket, Key=swot_store.CURATED_KEY
        )["Body"].read()
        data = json.loads(body.decode("utf-8"))
        retired = {r.get("target_bullet_id") for r in data.get("retirements", [])
                   if r.get("target_bullet_id")}
        by_ent: dict[str, list[dict[str, Any]]] = {}
        for b in data.get("bullets", []):
            if b.get("framework") != framework:
                continue
            if b.get("id") in retired:
                continue
            ent = b.get("entity")
            if ent:
                by_ent.setdefault(ent, []).append({
                    "dimension": b.get("dimension"),
                    "text": b.get("text"),
                    # the drafter's own confidence (for the dashboard heatmap);
                    # legacy bullets without it fall back to the curated pin.
                    "confidence": b.get("confidence", swot_store.CURATED_CONFIDENCE),
                    "status": "active",
                    "origin": b.get("origin"),
                })
        return by_ent
    except Exception:
        return {}


def _load_fw_updates_by_narrative(digests_bucket: str) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Map narrative id -> {framework: [{dimension, text, confidence, date}]} for the
    framework bullets that cite that narrative in their ``evidence``.

    Powers the on-card framework strips: a card shows a framework strip ONLY when
    that framework CHANGED for this narrative — the caller keeps rows whose bullet
    ``date`` (the belief's change/draft date, stable across mere re-approvals)
    equals the card's run date, so a standing-but-unchanged belief draws no strip.
    SWOT bullets carry no ``framework`` key, so they are keyed as ``swot``."""
    try:
        from src.synth import swot_store

        body = boto3.client("s3").get_object(
            Bucket=digests_bucket, Key=swot_store.CURATED_KEY
        )["Body"].read()
        data = json.loads(body.decode("utf-8"))
        retired = {r.get("target_bullet_id") for r in data.get("retirements", [])
                   if r.get("target_bullet_id")}
        out: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for b in data.get("bullets", []):
            if b.get("id") in retired:
                continue
            fw = b.get("framework") or "swot"
            row = {
                "dimension": b.get("dimension"),
                "text": b.get("text"),
                "confidence": b.get("confidence", swot_store.CURATED_CONFIDENCE),
                "date": str(b.get("date") or "")[:10],
            }
            for nid in dict.fromkeys(b.get("evidence") or []):  # dedup, keep order
                if nid:
                    out.setdefault(nid, {}).setdefault(fw, []).append(row)
        return out
    except Exception:
        return {}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Build feed.json from recent narratives and publish it to the site bucket."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    site_bucket = os.environ.get("ONCA_SITE_BUCKET")
    window_days = int(os.environ.get("ONCA_FEED_WINDOW_DAYS", "14"))

    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}

    narratives = load_recent_narratives(digests_bucket, window_days)
    # Attach the frameworks each narrative CHANGED on its run — a framework strip
    # shows only when this ticket actually moved that framework (a bullet whose
    # change-date matches the card's run date), not merely because the entity has a
    # standing belief citing this narrative. Keeps cards showing only the few
    # frameworks their signal touched (empty box filled / belief updated).
    fw_updates = _load_fw_updates_by_narrative(digests_bucket)
    for n in narratives:
        if not isinstance(n, dict):
            continue
        upd = fw_updates.get(n.get("id"))
        if not upd:
            continue
        run_date = (n.get("run_date") or n.get("as_of") or "")[:10]
        changed: dict[str, list[dict[str, Any]]] = {}
        for fw, rows in upd.items():
            fresh = [{"dimension": r["dimension"], "text": r["text"], "confidence": r["confidence"]}
                     for r in rows if r.get("date") == run_date]
            if fresh:
                changed[fw] = fresh
        if changed:
            n["fw_updates"] = changed
    industry_map, industry_meta = load_industry_data()
    feed = build_feed(
        narratives,
        reviews=load_pending_reviews(),
        industry_map=industry_map,
        industry_meta=industry_meta,
        macro=_load_macro(),
        swot=_load_swot(digests_bucket),
        tows_curated=_load_tows_curated(digests_bucket),
        porter_curated=_load_porter_curated(digests_bucket),
        pestle_curated=_load_fw_curated(digests_bucket, "pestle"),
        ansoff_curated=_load_fw_curated(digests_bucket, "ansoff"),
        bcg_curated=_load_fw_curated(digests_bucket, "bcg"),
        four_corners_curated=_load_fw_curated(digests_bucket, "four_corners"),
        seven_s_curated=_load_fw_curated(digests_bucket, "seven_s"),
        swot_proposals=_load_swot_proposals(digests_bucket),
        graph_proposals=_load_graph_proposals(digests_bucket),
        thread_cards=(_load_threads(digests_bucket) + _load_reg_lifecycles(digests_bucket)
                      + _load_relational(digests_bucket)),
        distress=_load_distress(digests_bucket),
        entity_attrs=load_entity_attributes(),
        coverage_gaps=_load_coverage_gaps(digests_bucket),
        reputation=_load_reputation(digests_bucket),
        financials=_load_financials(digests_bucket),
        market_share=_load_market_share(digests_bucket),
    )
    # ADR 018 Phase 3: continuous integrity audit over the registry + this feed —
    # operator-facing findings (scoped OUT of the entry/tenant slices below).
    try:
        from src.synth import entity_registry, integrity

        feed["integrity"] = integrity.audit(feed, entity_registry.list_entities())
    except Exception as exc:  # pragma: no cover - best-effort, read-only
        print(f"Warning: integrity audit skipped: {exc}")
        feed["integrity"] = {"findings": [], "counts": {}, "total": 0}
    # ADR 019 Phase 3b — vertical feed scoping: a sectorial deployment publishes ONLY its
    # own vertical's industries. financial-services (default) spans the whole taxonomy →
    # vertical_industries is None → no scoping, so Onça's feed is unchanged (non-breaking).
    from src.ingest import registry as _registry

    _vertical = os.environ.get("ONCA_VERTICAL", _registry.VERTICAL_FS)
    _vinds = _registry.vertical_industries(_vertical)
    if _vinds is not None:
        feed = scope_feed_to_modules(feed, _vinds)
        feed["vertical"] = _vertical

    body = json.dumps(feed, ensure_ascii=False, default=_json_default).encode("utf-8")
    # ADR 016: the entry-scoped slice (entry-tier industries only) — written alongside
    # the full feed so the Entry Portal serves ONLY entry data (no higher-tier leak).
    entry_feed = derive_entry_feed(feed)
    entry_body = json.dumps(entry_feed, ensure_ascii=False, default=_json_default).encode("utf-8")

    published = None
    if site_bucket:
        s3 = boto3.client("s3")
        try:
            s3.put_object(
                Bucket=site_bucket,
                Key=FEED_KEY,
                Body=body,
                ContentType="application/json",
                CacheControl="no-cache",
            )
            published = f"s3://{site_bucket}/{FEED_KEY}"
        except Exception as exc:  # pragma: no cover - publish is best-effort
            print(f"Warning: feed publish failed: {exc}")
        try:
            s3.put_object(
                Bucket=site_bucket,
                Key=ENTRY_FEED_KEY,
                Body=entry_body,
                ContentType="application/json",
                CacheControl="no-cache",
            )
        except Exception as exc:  # pragma: no cover - publish is best-effort
            print(f"Warning: entry feed publish failed: {exc}")

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "status": "ok",
                "as_of": feed["as_of"],
                "feed_count": len(feed["feed"]),
                "entry_feed_count": len(entry_feed["feed"]),
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
