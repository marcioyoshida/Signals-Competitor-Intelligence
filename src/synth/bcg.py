"""ADR 006 — BCG Growth-Share Matrix: portfolio-position analysis per entity.

Classifies each tracked entity's competitive position into the four BCG
quadrants based on relative market share and market growth:

    star            — high share, high growth (invest)
    cash_cow        — high share, low growth (harvest)
    question_mark   — low share, high growth (invest or divest)
    dog             — low share, low growth (divest)

Fed by existing signal: IF.data market share (relative share) × longitudinal
time-series (segment growth). The quantitative placement uses sourced data;
the LLM provides contextual analysis per entity.

Each entity is analyzed through one bounded LLM call that reads its recent
narrative evidence + industry context (ADR-006 addendum #32: own-track evidence,
SWOT is not an input; every assessment cites >=1 evidence index). Everything is PROPOSED
ONLY (propose→vet, never auto-asserted) and flows through the Phase-C vetting UI.

The core `classify_portfolio()` is pure — it takes a `draft_fn`, so tests use fakes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from typing import Any, Callable

import boto3

from src.synth import feature_store, swot_reconcile, swot_store
from src.synth.bedrock_llm import DEFAULT_SYNTH_MODEL, converse
from src.synth.synthesize import ENTITY_LABELS, run_at_now, run_date_today

BCG_PROPOSALS_KEY = "bcg/proposals.json"
FRAMEWORK = "bcg"
DIMENSIONS = ("star", "cash_cow", "question_mark", "dog")

from src.synth import framework_common
from src.synth.framework_common import fz

# ADR #34 — per-dimension evidence binding (axis OR lens). See framework_common.
DIM_SIGNALS = {
    "star":          (fz("comparative"),             fz("market")),
    "cash_cow":      (fz(),                           fz("market", "juros")),
    "question_mark": (fz("thematic"),                 fz("entrants")),
    "dog":           (fz("silence", "longitudinal"),  fz()),
}

DIM_LABELS = {
    "star": "Estrela",
    "cash_cow": "Vaca leiteira",
    "question_mark": "Interrogação",
    "dog": "Abacaxi",
}

# Deterministic BCG position from financial statements (issue #7 Phase 2). The LLM
# classifier above narrates from signal evidence; this grounds the two axes in real
# numbers: market growth ≈ the issuer's YoY REVENUE GROWTH, relative share ≈ its REVENUE
# vs the largest same-industry peer among tracked issuers. Thresholds are heuristic
# (documented), so the quadrant is a hard-number anchor the narrative can cite.
BCG_HIGH_SHARE = 0.5   # ≥ half the industry leader's revenue
BCG_HIGH_GROWTH = 0.10  # ≥ 10% YoY revenue growth


def _classify(share: float | None, growth: float | None) -> str | None:
    if share is None or growth is None:
        return None
    hi_s, hi_g = share >= BCG_HIGH_SHARE, growth >= BCG_HIGH_GROWTH
    return ("star" if hi_s and hi_g else "cash_cow" if hi_s
            else "question_mark" if hi_g else "dog")


# Entry-tier/fund industries: an operating company (a DFP filer) competes in its
# OPERATING market (banking/IB/insurance…), not in a fund/consórcio tag it may carry —
# so these are excluded from BCG peer-grouping (also robust to any agri-funds pollution).
_LEAF_INDUSTRIES = frozenset(
    {"agri-funds", "real-estate-funds", "consorcio", "betting", "crypto"}
)


def position_from_financials(
    records: list[dict[str, Any]], industry_map: dict[str, list[str]] | None,
) -> list[dict[str, Any]]:
    """Annotate each financials record with a ``bcg`` position derived from revenue.
    relative_share = revenue / the largest same-industry peer (in the entity's biggest
    OPERATING market); growth = revenue_growth. ``bcg`` is omitted when revenue/growth is
    missing or the entity has no operating industry."""
    industry_map = industry_map or {}
    op: dict[str, list[str]] = {
        r["entity_id"]: [i for i in industry_map.get(r["entity_id"], []) if i not in _LEAF_INDUSTRIES]
        for r in records if r.get("entity_id")
    }
    leader: dict[str, float] = {}  # operating industry -> max tracked revenue
    for r in records:
        rev = float(r.get("revenue") or 0)
        for ind in op.get(r.get("entity_id"), []):
            leader[ind] = max(leader.get(ind, 0.0), rev)
    out: list[dict[str, Any]] = []
    for r in records:
        rec = dict(r)
        rev = float(r.get("revenue") or 0)
        inds = op.get(r.get("entity_id"), [])
        # the entity's biggest market (largest peer leader) — where it actually competes.
        best = max(inds, key=lambda i: leader.get(i, 0.0), default=None)
        peer = leader.get(best, 0.0) if best else 0.0
        share = round(rev / peer, 3) if peer and rev else None
        growth = r.get("revenue_growth")
        q = _classify(share, growth)
        if q and best:
            rec["bcg"] = {"quadrant": q, "label": DIM_LABELS[q],
                          "relative_share": share, "growth": round(float(growth), 4),
                          "industry": best}
        out.append(rec)
    return out

ENABLED = os.environ.get("ONCA_BCG_ENABLED", "1") not in ("0", "false", "False")
MAX_ENTITIES = int(os.environ.get("ONCA_BCG_MAX_ENTITIES", "8"))
MAX_PER_DIM = int(os.environ.get("ONCA_BCG_MAX_PER_DIM", "2"))
MIN_CONF = float(os.environ.get("ONCA_BCG_MIN_CONF", "0.5"))
MIN_EVIDENCE = int(os.environ.get("ONCA_BCG_MIN_EVIDENCE", "3"))


def _entity_label(entity: str) -> str:
    return ENTITY_LABELS.get(entity) or str(entity).replace("_", " ").title()


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _bullet_id(entity: str, dim: str, text: str) -> str:
    h = hashlib.sha1(re.sub(r"\s+", " ", text).strip().lower().encode("utf-8")).hexdigest()[:10]
    return f"bcg:{entity}:{dim}:{h}"


def _active_swot(belief: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in (belief or {}).get("bullets", [])
            if b.get("status") == "active" and b.get("dimension") in swot_store.DIMENSIONS]


def _collect_evidence_ids(narratives: list[dict[str, Any]], *, max_claims: int = 12) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for n in sorted(narratives, key=lambda x: _score(x.get("threat_score")), reverse=True):
        nid = n.get("id")
        claim = swot_reconcile.key_claim(n)
        if not nid or len(claim) < swot_reconcile.MIN_CLAIM_LEN:
            continue
        norm = re.sub(r"\s+", " ", claim).strip().lower()
        if norm in seen:
            continue
        seen.add(norm)
        out.append({"id": nid, "claim": claim, "date": feature_store._date_of(n),
                    "axis": n.get("axis"), "lenses": n.get("lenses") or [],
                    "threat_score": _score(n.get("threat_score"))})
        if len(out) >= max_claims:
            break
    return out


def eligible_entities(
    beliefs: dict[str, dict[str, Any]],
    narratives_by_ent: dict[str, list[dict[str, Any]]],
    *,
    already_proposed: frozenset[str] = frozenset(),
    max_entities: int = MAX_ENTITIES,
    min_evidence: int = MIN_EVIDENCE,
) -> list[str]:
    candidates: list[tuple[str, int]] = []
    for ent in set(beliefs.keys()) | set(narratives_by_ent.keys()):
        if ent in already_proposed:
            continue
        # ADR-006 addendum (#32): own-track evidence — eligibility is gated on the
        # framework's OWN narrative evidence, not on SWOT bullet count.
        n_narr = len(narratives_by_ent.get(ent, []))
        if n_narr >= min_evidence:
            candidates.append((ent, n_narr))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return [ent for ent, _ in candidates[:max_entities]]


DraftFn = Callable[[str, list[str], list[dict[str, Any]], list[dict[str, Any]]], list[dict[str, Any]]]

_DRAFT_SYSTEM = (
    "You are a competitive-intelligence analyst performing a BCG Growth-Share Matrix "
    "analysis for a Brazilian financial-services competitor, to be reviewed by a human. "
    "The four quadrants are: star (high relative market share + high market growth — "
    "invest to maintain leadership), cash_cow (high share + low growth — harvest "
    "profits), question_mark (low share + high growth — invest or divest), dog (low "
    "share + low growth — divest or restructure). Assess this competitor's position "
    "in each of its market segments. Each assessment must cite the evidence indices "
    "it draws from. Write in pt-BR, one sentence per assessment. Be specific to this "
    "competitor. Return ONLY minified JSON, no prose, no markdown."
)


def _draft_prompt(
    label: str,
    industries: list[str],
    swot_bullets: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> str:
    lines = [
        f"Competitor: {label}",
        f"Industries/Segments: {', '.join(industries) or '(unknown)'}",
        "",
        "Recent signals (evidence):",
    ]
    for i, e in enumerate(evidence):
        lines.append(f"[{i}] ({e['date']}, {e.get('axis','')}) {e['claim']}")
    lines += [
        "",
        "Classify this competitor's position using the BCG Growth-Share Matrix.",
        "Return JSON:",
        '{"positions":[{"quadrant":"star|cash_cow|question_mark|dog",'
        '"text":"<pt-BR, one sentence describing the position and segment>",'
        '"confidence":0..1,"evidence":[<indices into the evidence list>]}]}',
        f"Rules: at most {MAX_PER_DIM} positions per quadrant; omit a quadrant with no "
        "evidence. Each position MUST include >=1 valid evidence index. "
        "text in pt-BR, specific to the competitor and segment, one sentence.",
    ]
    return "\n".join(lines)


def _parse_draft(raw: str | None, n_evidence: int) -> list[dict[str, Any]]:
    if not raw:
        return []
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    per_dim: dict[str, int] = {}
    for item in data.get("positions") or []:
        if not isinstance(item, dict):
            continue
        quadrant = str(item.get("quadrant") or "").lower()
        text = str(item.get("text") or "").strip()[:280]
        if quadrant not in DIMENSIONS or not text:
            continue
        indices = []
        for j in item.get("evidence") or []:
            try:
                j = int(j)
            except (TypeError, ValueError):
                continue
            if 0 <= j < n_evidence and j not in indices:
                indices.append(j)
        if not indices:
            continue
        if per_dim.get(quadrant, 0) >= MAX_PER_DIM:
            continue
        per_dim[quadrant] = per_dim.get(quadrant, 0) + 1
        out.append({"quadrant": quadrant, "text": text,
                    "confidence": _score(item.get("confidence")), "evidence": indices})
    return out


def llm_draft(
    label: str, industries: list[str],
    swot_bullets: list[dict[str, Any]], evidence: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    prompt = _draft_prompt(label, industries, swot_bullets, evidence)
    raw = converse(
        prompt,
        model_id=DEFAULT_SYNTH_MODEL,
        system=_DRAFT_SYSTEM,
        max_tokens=900,
    )
    return _parse_draft(raw, len(evidence))


def classify_portfolio(
    beliefs: dict[str, dict[str, Any]],
    narratives: list[dict[str, Any]],
    industries_map: dict[str, list[str]],
    *,
    run_date: str,
    draft_fn: DraftFn,
    already_proposed: frozenset[str] = frozenset(),
    max_entities: int = MAX_ENTITIES,
    min_conf: float = MIN_CONF,
    min_evidence: int = MIN_EVIDENCE,
) -> list[dict[str, Any]]:
    """Pure: beliefs + narratives -> BCG proposals. No I/O."""
    by_ent: dict[str, list[dict[str, Any]]] = {}
    for n in narratives:
        if not isinstance(n, dict):
            continue
        ent = n.get("entity")
        if ent:
            by_ent.setdefault(ent, []).append(n)

    entities = eligible_entities(beliefs, by_ent, already_proposed=already_proposed,
                                  max_entities=max_entities, min_evidence=min_evidence)
    proposals: list[dict[str, Any]] = []

    for ent in entities:
        belief = beliefs.get(ent) or {}
        label = belief.get("label") or _entity_label(ent)
        swot_bullets = _active_swot(belief)
        evidence = _collect_evidence_ids(by_ent.get(ent, []))
        industries = industries_map.get(ent, [])

        positions = draft_fn(label, industries, swot_bullets, evidence)
        for pos in positions:
            if pos["confidence"] < min_conf:
                continue
            ev_ids = framework_common.on_signal_ids(pos["evidence"], evidence, pos["quadrant"], DIM_SIGNALS)
            if not ev_ids:
                continue
            proposals.append({
                "id": _bullet_id(ent, pos["quadrant"], pos["text"]),
                "kind": "bcg",
                "framework": FRAMEWORK,
                "entity": ent,
                "label": label,
                "dimension": pos["quadrant"],
                "target_bullet_id": None,
                "target_text": None,
                "text": pos["text"],
                "evidence": ev_ids,
                "narrative_id": ev_ids[0] if ev_ids else None,
                "date": run_date,
                "confidence": round(pos["confidence"], 2),
                "stance_conf": round(pos["confidence"], 2),
                "status": "pending",
                "created": run_date,
            })
    return proposals


def _load_json(bucket: str, key: str, s3: Any) -> dict[str, Any]:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}


def proposed_entities(bucket: str, *, s3: Any) -> frozenset[str]:
    prev = _load_json(bucket, BCG_PROPOSALS_KEY, s3).get("proposals", [])
    return frozenset(p.get("entity") for p in prev if p.get("entity"))


def publish(
    proposals: list[dict[str, Any]], bucket: str, *, s3: Any, as_of: str
) -> dict[str, int]:
    prev = _load_json(bucket, BCG_PROPOSALS_KEY, s3).get("proposals", [])
    merged = swot_reconcile.merge_proposals(prev, proposals)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    s3.put_object(
        Bucket=bucket, Key=BCG_PROPOSALS_KEY,
        Body=json.dumps({"generated_at": now, "as_of": as_of,
                         "proposals": merged}, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json", CacheControl="no-cache",
    )
    return {
        "proposals": len(merged),
        "proposals_new": len(merged) - len(prev),
        "proposals_pending": sum(1 for p in merged if p.get("status") == "pending"),
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}
    if not ENABLED:
        return {"statusCode": 200, "body": json.dumps({"status": "disabled"})}

    s3 = boto3.client("s3")
    run_date = run_date_today()
    window = int(os.environ.get("ONCA_BCG_WINDOW_DAYS",
                                os.environ.get("ONCA_FEATURE_WINDOW_DAYS", "90")))

    index = _load_json(digests_bucket, swot_store.INDEX_KEY, s3)
    entity_keys = set((index.get("entities") or {}).keys())
    beliefs = swot_reconcile.load_beliefs(digests_bucket, entity_keys, s3=s3)
    narratives = feature_store.load_history(digests_bucket, window, s3=s3)
    already = proposed_entities(digests_bucket, s3=s3)

    try:
        from src.synth.entity_registry import entity_industry_map
        industries_map = entity_industry_map()
    except Exception:
        industries_map = {}

    proposals = classify_portfolio(
        beliefs, narratives, industries_map,
        run_date=run_date, draft_fn=llm_draft,
        already_proposed=already,
    )

    counts = {"proposals": 0, "proposals_new": 0, "proposals_pending": 0}
    try:
        counts = publish(proposals, digests_bucket, s3=s3, as_of=run_date)
    except Exception as exc:
        print(f"Warning: BCG publish failed: {exc}")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "status": "ok",
            "as_of": run_date,
            "entities_eligible": len(entity_keys),
            "already_proposed": len(already),
            "proposals_found": len(proposals),
            "run_at": run_at_now(),
            **counts,
        }),
    }
