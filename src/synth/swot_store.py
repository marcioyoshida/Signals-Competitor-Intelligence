"""ADR 004 step 2 — the per-entity SWOT belief store (v1: deterministic feeders).

The qualitative twin of the Wave-0 feature store: where the feature store holds
*numbers* (mean/σ/cadence), this holds *beliefs* — evidence-linked S/W/O/T claims about
each tracked competitor, rebuilt each run from the ADR-003 Wave-1 axes:

- **comparative** `swot_hint` → **S** (outperforms peers) / **W** (underperforms),
- **cohort**      `swot_hint` → **T** (segment heating) / **O** (segment cooling), per member,
- **thematic**    `swot_hint` → **O** / **T** market currents, per participating entity,
- **longitudinal** direction  → **S** (escalation) / **W** (cooling) [derived here],
- **silence**                → **W** (retreat/absence) [derived here].

v1 is **read-mostly and deterministic** (ADR 004 step 2): no LLM stance, no embeddings
(those land in v2's reconcile-against-belief loop). Bullets are keyed deterministically
by (entity, dimension, source-key); evidence accrues; confidence grows with corroboration
and decays with staleness. A lightweight **deterministic reconcile** flags a bullet
`challenged` when the opposite dimension for the same source-key has fresher evidence
(e.g. a peer *outperform* today supersedes a stale *underperform*). Nothing is ever
auto-retired — precision-first, exactly as ADR 004 requires (contradictions propose,
they don't delete). regulatory (`T`) is domain-level (no member list) and is deferred
from the per-entity store until a domain→entity map exists.

The store is **recomputable derived state**: `swot/{entity}.json` (full, evidence-linked
— the durable belief file) is overwritten each run, and `swot/index.json` (compact) is
published for the dashboard. Not narratives — it never touches the narrative store.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any, Iterator

import boto3

from src.synth import feature_store
from src.synth.synthesize import ENTITY_LABELS, run_at_now, run_date_today

SWOT_PREFIX = "swot/"
INDEX_KEY = "swot/index.json"

DIMENSIONS = ("S", "W", "O", "T")
_OPPOSITE = {"S": "W", "W": "S", "O": "T", "T": "O"}


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except (TypeError, ValueError):
        return float(default)


def _days_between(a: str, b: str) -> int | None:
    try:
        return (feature_store._parse(a) - feature_store._parse(b)).days
    except Exception:
        return None


def _industry_label(slug: str) -> str:
    try:
        from src.synth.entity_registry import INDUSTRIES

        return INDUSTRIES.get(slug, {}).get("display_name") or slug
    except Exception:  # pragma: no cover - registry import best-effort
        return slug


def _entity_label(entity: str, narrative: dict[str, Any]) -> str:
    return (
        narrative.get("entity_label")
        or narrative.get("label")
        or ENTITY_LABELS.get(entity)
        or str(entity).replace("_", " ").title()
    )


# --- Feeders ----------------------------------------------------------------
def feeds(narrative: dict[str, Any]) -> Iterator[tuple[str, str, str, str | None]]:
    """Yield (entity, dimension, base_key, context_label) SWOT feeds from one card.

    Uses each axis's `swot_hint` where present; derives S/W for longitudinal & silence
    (which don't carry a hint). base_key identifies the *claim* so repeated evidence
    lands on one bullet; context_label carries the human label (cohort/theme).
    """
    axis = narrative.get("axis")
    hint = narrative.get("swot_hint") or {}
    if axis == "comparative" and hint.get("dimension"):
        ent = narrative.get("entity")
        coh = hint.get("cohort")
        if ent and coh:
            yield ent, hint["dimension"], f"peer:{coh}", _industry_label(coh)
    elif axis == "cohort" and hint.get("dimension"):
        coh = hint.get("cohort")
        label = narrative.get("cohort_label") or _industry_label(coh or "")
        for ent in hint.get("entities") or []:
            yield ent, hint["dimension"], f"sector:{coh}", label
    elif axis == "thematic" and hint.get("dimension"):
        theme = hint.get("theme")
        label = narrative.get("theme_display") or theme
        for ent in hint.get("entities") or []:
            yield ent, hint["dimension"], f"theme:{theme}", label
    elif axis == "longitudinal":
        ent = narrative.get("entity")
        d = narrative.get("direction")
        dim = "S" if d == "escalation" else "W" if d == "cooling" else None
        if ent and dim:
            yield ent, dim, "trajectory", None
    elif axis == "silence":
        ent = narrative.get("entity")
        if ent:
            yield ent, "W", "silence", None
    # regulatory: domain-level (T), no member list -> deferred from per-entity store (v1)


def _bullet_text(base_key: str, dim: str, ctx: str | None) -> str:
    ctx = ctx or "seu segmento"
    if base_key.startswith("peer:"):
        return (f"Supera os pares em {ctx} — força competitiva relativa sustentada."
                if dim == "S" else
                f"Fica atrás dos pares em {ctx} — fraqueza relativa.")
    if base_key.startswith("sector:"):
        return (f"Opera em um setor em aquecimento ({ctx}) — intensidade competitiva crescente."
                if dim == "T" else
                f"Opera em um setor em esfriamento ({ctx}) — possível janela de oportunidade.")
    if base_key.startswith("theme:"):
        return (f"Posicionado na corrente setorial '{ctx}' — oportunidade de mercado."
                if dim == "O" else
                f"Exposto à pressão da corrente setorial '{ctx}'.")
    if base_key == "trajectory":
        return ("Trajetória própria em escalada — momentum acima da própria média histórica."
                if dim == "S" else
                "Trajetória própria em recuo — perda de momentum vs. a própria média.")
    if base_key == "silence":
        return "Ausência de sinais rastreáveis recentes — possível retração ou perda de relevância."
    return f"{base_key} ({dim})"


def _confidence(n_evidence: int, days_since_latest: int | None, window: int) -> float:
    """Grows with corroboration (capped), decays with staleness."""
    base = min(0.9, 0.35 + 0.13 * min(n_evidence, 4))
    if days_since_latest is None:
        recency = 1.0
    else:
        recency = max(0.5, 1.0 - days_since_latest / max(window, 1))
    return round(base * recency, 2)


def build_beliefs(
    narratives: list[dict[str, Any]], *, as_of: str | None = None, window: int = 90
) -> dict[str, dict[str, Any]]:
    """Pure: narratives -> {entity: belief-file dict}. Deterministic, no I/O."""
    as_of = as_of or run_date_today()

    # (entity, base_key, dim) -> bullet accumulator
    acc: dict[tuple[str, str, str], dict[str, Any]] = {}
    labels: dict[str, str] = {}
    for n in narratives:
        if not isinstance(n, dict):
            continue
        date = feature_store._date_of(n)
        for ent, dim, base_key, ctx in feeds(n):
            if dim not in DIMENSIONS:
                continue
            labels.setdefault(ent, _entity_label(ent, n))
            key = (ent, base_key, dim)
            b = acc.setdefault(key, {
                "dimension": dim, "base_key": base_key, "ctx": ctx,
                "evidence": {}, "latest": "",
            })
            if ctx and not b["ctx"]:
                b["ctx"] = ctx
            nid = n.get("id")
            if nid:
                b["evidence"][nid] = {"id": nid, "date": date, "axis": n.get("axis")}
            if date > b["latest"]:
                b["latest"] = date

    # Assemble per-entity bullets + deterministic reconcile.
    per_entity: dict[str, list[dict[str, Any]]] = {}
    latest_by_base: dict[tuple[str, str], dict[str, str]] = {}  # (ent,base_key)->{dim:latest}
    for (ent, base_key, dim), b in acc.items():
        latest_by_base.setdefault((ent, base_key), {})[dim] = b["latest"]

    for (ent, base_key, dim), b in acc.items():
        ev = sorted(b["evidence"].values(), key=lambda e: e["date"] or "", reverse=True)
        days_since = _days_between(as_of, b["latest"]) if b["latest"] else None
        conf = _confidence(len(ev), days_since, window)

        # Reconcile: challenged if the opposite dimension for this claim is fresher.
        status = "active"
        opp = _OPPOSITE[dim]
        opp_latest = latest_by_base.get((ent, base_key), {}).get(opp)
        if opp_latest and opp_latest > b["latest"]:
            status = "challenged"
            conf = round(conf * 0.6, 2)

        bullet = {
            "id": f"{ent}:{base_key}:{dim}",
            "dimension": dim,
            "text": _bullet_text(base_key, dim, b["ctx"]),
            "confidence": conf,
            "status": status,
            "source_key": base_key,
            "evidence": ev,
            "evidence_count": len(ev),
            "latest": b["latest"],
        }
        per_entity.setdefault(ent, []).append(bullet)

    beliefs: dict[str, dict[str, Any]] = {}
    for ent, bullets in per_entity.items():
        # strongest first: active before challenged, then confidence, then recency
        bullets.sort(key=lambda x: (x["status"] == "active", x["confidence"], x["latest"]),
                     reverse=True)
        counts = {d: sum(1 for x in bullets if x["dimension"] == d and x["status"] == "active")
                  for d in DIMENSIONS}
        beliefs[ent] = {
            "entity": ent,
            "label": labels.get(ent, str(ent).replace("_", " ").title()),
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "as_of": as_of,
            "window_days": window,
            "counts": counts,
            "bullets": bullets,
        }
    return beliefs


def _compact(belief: dict[str, Any]) -> dict[str, Any]:
    """Dashboard view: bullets grouped by dimension, evidence reduced to a count."""
    grouped: dict[str, list[dict[str, Any]]] = {d: [] for d in DIMENSIONS}
    for b in belief["bullets"]:
        grouped[b["dimension"]].append({
            "text": b["text"],
            "confidence": b["confidence"],
            "status": b["status"],
            "evidence_count": b["evidence_count"],
            "latest": b["latest"],
        })
    return {"entity": belief["entity"], "label": belief["label"],
            "counts": belief["counts"], "dimensions": grouped}


def publish(beliefs: dict[str, dict[str, Any]], bucket: str, *, s3: Any | None = None,
            as_of: str | None = None) -> dict[str, Any]:
    """Overwrite swot/{entity}.json (full) + swot/index.json (compact aggregate)."""
    s3 = s3 or boto3.client("s3")
    as_of = as_of or run_date_today()
    for ent, belief in beliefs.items():
        s3.put_object(
            Bucket=bucket, Key=f"{SWOT_PREFIX}{ent}.json",
            Body=json.dumps(belief, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json", CacheControl="no-cache",
        )
    index = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "as_of": as_of,
        "entities": {ent: _compact(b) for ent, b in beliefs.items()},
    }
    s3.put_object(
        Bucket=bucket, Key=INDEX_KEY,
        Body=json.dumps(index, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json", CacheControl="no-cache",
    )
    return index


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Rebuild the per-entity SWOT belief store from the durable narrative history."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}

    window = int(_f("ONCA_SWOT_WINDOW_DAYS", _f("ONCA_FEATURE_WINDOW_DAYS", 90)))
    s3 = boto3.client("s3")
    run_date = run_date_today()

    narratives = feature_store.load_history(digests_bucket, window, s3=s3)
    beliefs = build_beliefs(narratives, as_of=run_date, window=window)

    published = None
    try:
        idx = publish(beliefs, digests_bucket, s3=s3, as_of=run_date)
        published = f"s3://{digests_bucket}/{INDEX_KEY}"
    except Exception as exc:  # pragma: no cover - publish best-effort
        print(f"Warning: SWOT publish failed: {exc}")
        idx = {"entities": {}}

    total_bullets = sum(len(b["bullets"]) for b in beliefs.values())
    return {
        "statusCode": 200,
        "body": json.dumps({
            "status": "ok",
            "as_of": run_date,
            "window_days": window,
            "entities": len(beliefs),
            "bullets": total_bullets,
            "run_at": run_at_now(),
            "published": published,
        }),
    }
