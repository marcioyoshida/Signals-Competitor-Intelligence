"""ADR 006 — PESTLE: macro-environmental analysis per entity.

Analyzes the six PESTLE dimensions for each tracked entity:

    political       — government policy, stability, trade restrictions
    economic        — interest rates, exchange, inflation, growth
    social          — demographics, culture, consumer behavior
    technological   — innovation, automation, R&D, digital disruption
    legal           — regulation, compliance, licensing (normativos/DOU)
    environmental   — ESG, sustainability, climate risk

Fed by existing signal: legal ← normativos/DOU; economic ← BCB macro/juros;
political/social/technological ← thematic + news; environmental ← ESG themes.

Each entity is analyzed through one bounded LLM call that reads its recent
narrative evidence + industry context (ADR-006 addendum #32: own-track evidence,
SWOT is not an input; every assessment cites >=1 evidence index). Everything is PROPOSED
ONLY (propose→vet, never auto-asserted) and flows through the Phase-C vetting UI.

The core `analyze_factors()` is pure — it takes a `draft_fn`, so tests use fakes.
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

PESTLE_PROPOSALS_KEY = "pestle/proposals.json"
FRAMEWORK = "pestle"
DIMENSIONS = ("political", "economic", "social", "technological", "legal", "environmental")

DIM_LABELS = {
    "political": "Político",
    "economic": "Econômico",
    "social": "Social",
    "technological": "Tecnológico",
    "legal": "Legal / Regulatório",
    "environmental": "Ambiental / ESG",
}

ENABLED = os.environ.get("ONCA_PESTLE_ENABLED", "1") not in ("0", "false", "False")
MAX_ENTITIES = int(os.environ.get("ONCA_PESTLE_MAX_ENTITIES", "8"))
MAX_PER_DIM = int(os.environ.get("ONCA_PESTLE_MAX_PER_DIM", "2"))
MIN_CONF = float(os.environ.get("ONCA_PESTLE_MIN_CONF", "0.5"))
MIN_EVIDENCE = int(os.environ.get("ONCA_PESTLE_MIN_EVIDENCE", "3"))


def _entity_label(entity: str) -> str:
    return ENTITY_LABELS.get(entity) or str(entity).replace("_", " ").title()


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _bullet_id(entity: str, dim: str, text: str) -> str:
    h = hashlib.sha1(re.sub(r"\s+", " ", text).strip().lower().encode("utf-8")).hexdigest()[:10]
    return f"pestle:{entity}:{dim}:{h}"


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
                    "axis": n.get("axis"), "threat_score": _score(n.get("threat_score"))})
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
    "You are a competitive-intelligence analyst performing a PESTLE macro-environmental "
    "analysis for a Brazilian financial-services competitor, to be reviewed by a human. "
    "The six PESTLE factors are: political (government policy, stability), economic "
    "(interest rates, exchange, inflation, growth), social (demographics, consumer "
    "behavior), technological (innovation, digital disruption), legal (regulation, "
    "compliance, normativos), environmental (ESG, sustainability, climate). Analyze "
    "each factor's impact on this competitor's operating environment. Each assessment "
    "must cite the evidence indices it draws from. Write in pt-BR, one sentence per "
    "assessment. Be specific to this competitor and the Brazilian market. Return ONLY "
    "minified JSON, no prose, no markdown."
)


def _draft_prompt(
    label: str,
    industries: list[str],
    swot_bullets: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> str:
    lines = [
        f"Competitor: {label}",
        f"Industries: {', '.join(industries) or '(unknown)'}",
        "",
        "Recent signals (evidence):",
    ]
    for i, e in enumerate(evidence):
        lines.append(f"[{i}] ({e['date']}, {e.get('axis','')}) {e['claim']}")
    lines += [
        "",
        "Analyze the six PESTLE macro-environmental factors impacting this competitor.",
        "Return JSON:",
        '{"factors":[{"factor":"political|economic|social|technological|legal|environmental",'
        '"text":"<pt-BR, one sentence assessment>",'
        '"confidence":0..1,"evidence":[<indices into the evidence list>]}]}',
        f"Rules: at most {MAX_PER_DIM} assessments per factor; omit a factor with no "
        "supporting evidence. Each assessment MUST include >=1 valid evidence index. "
        "text in pt-BR, specific to the competitor, one sentence.",
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
    for item in data.get("factors") or []:
        if not isinstance(item, dict):
            continue
        factor = str(item.get("factor") or "").lower()
        text = str(item.get("text") or "").strip()[:280]
        if factor not in DIMENSIONS or not text:
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
        if per_dim.get(factor, 0) >= MAX_PER_DIM:
            continue
        per_dim[factor] = per_dim.get(factor, 0) + 1
        out.append({"factor": factor, "text": text,
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


def analyze_factors(
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
    """Pure: beliefs + narratives -> PESTLE proposals. No I/O."""
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

        factors = draft_fn(label, industries, swot_bullets, evidence)
        for f in factors:
            if f["confidence"] < min_conf:
                continue
            ev_ids = [evidence[j]["id"] for j in f["evidence"] if j < len(evidence)]
            if not ev_ids:
                continue
            proposals.append({
                "id": _bullet_id(ent, f["factor"], f["text"]),
                "kind": "pestle",
                "framework": FRAMEWORK,
                "entity": ent,
                "label": label,
                "dimension": f["factor"],
                "target_bullet_id": None,
                "target_text": None,
                "text": f["text"],
                "evidence": ev_ids,
                "narrative_id": ev_ids[0] if ev_ids else None,
                "date": run_date,
                "confidence": round(f["confidence"], 2),
                "stance_conf": round(f["confidence"], 2),
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
    prev = _load_json(bucket, PESTLE_PROPOSALS_KEY, s3).get("proposals", [])
    return frozenset(p.get("entity") for p in prev if p.get("entity"))


def publish(
    proposals: list[dict[str, Any]], bucket: str, *, s3: Any, as_of: str
) -> dict[str, int]:
    prev = _load_json(bucket, PESTLE_PROPOSALS_KEY, s3).get("proposals", [])
    merged = swot_reconcile.merge_proposals(prev, proposals)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    s3.put_object(
        Bucket=bucket, Key=PESTLE_PROPOSALS_KEY,
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
    window = int(os.environ.get("ONCA_PESTLE_WINDOW_DAYS",
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

    proposals = analyze_factors(
        beliefs, narratives, industries_map,
        run_date=run_date, draft_fn=llm_draft,
        already_proposed=already,
    )

    counts = {"proposals": 0, "proposals_new": 0, "proposals_pending": 0}
    try:
        counts = publish(proposals, digests_bucket, s3=s3, as_of=run_date)
    except Exception as exc:
        print(f"Warning: PESTLE publish failed: {exc}")

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
