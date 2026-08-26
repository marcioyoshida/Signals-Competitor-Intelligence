"""ADR 006 — Porter's Five Forces: competitive-structure analysis per entity.

Analyzes the five industry forces for each tracked entity's competitive position:

    rivalry          — intensity of competition among existing players
    new_entrants     — threat of new competitors entering
    substitutes      — threat of substitute products/services
    buyer_power      — bargaining power of buyers/customers
    supplier_power   — bargaining power of suppliers/regulators

Fed by existing signal already in the narrative store:
- rivalry:       comparative axis (peer rankings), IF.data market share
- new_entrants:  new_entrants lens from the BCB authorizations pipeline
- substitutes:   thematic axis (disruptive tech / product currents)
- buyer_power:   thematic + news (customer-side signals)
- supplier_power: regulatory axis + news (regulation as constraint)

Each entity is analyzed through one bounded LLM call that reads its recent
narrative evidence + industry context. Per the ADR-006 addendum (#32) the analysis
draws on the framework's OWN narrative evidence, not on the SWOT belief store (only
TOWS is a SWOT transform); every assessment must still cite >=1 evidence index.
Everything is PROPOSED
ONLY (propose→vet, never auto-asserted) and flows through the Phase-C vetting UI.

The core `analyze_forces()` is pure — it takes a `draft_fn`, so tests use fakes.
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

PORTER_PROPOSALS_KEY = "porter/proposals.json"
FRAMEWORK = "porter"
DIMENSIONS = ("rivalry", "new_entrants", "substitutes", "buyer_power", "supplier_power")

from src.synth import framework_common
from src.synth.framework_common import fz

# ADR #34 — per-dimension evidence binding (axis OR lens). See framework_common.
DIM_SIGNALS = {
    "rivalry":        (fz("comparative"), fz("market")),
    "new_entrants":   (fz(),              fz("entrants")),
    "substitutes":    (fz("thematic"),    fz("pix", "news")),
    "buyer_power":    (fz("thematic"),    fz("news", "pix")),
    "supplier_power": (fz("regulatory"),  fz("juros", "regulatory", "dou")),
}

DIM_LABELS = {
    "rivalry": "Rivalidade entre concorrentes",
    "new_entrants": "Ameaça de novos entrantes",
    "substitutes": "Ameaça de substitutos",
    "buyer_power": "Poder de barganha dos compradores",
    "supplier_power": "Poder de barganha dos fornecedores/reguladores",
}

ENABLED = os.environ.get("ONCA_PORTER_ENABLED", "1") not in ("0", "false", "False")
MAX_ENTITIES = int(os.environ.get("ONCA_PORTER_MAX_ENTITIES", "8"))
MAX_PER_FORCE = int(os.environ.get("ONCA_PORTER_MAX_PER_FORCE", "2"))
MIN_CONF = float(os.environ.get("ONCA_PORTER_MIN_CONF", "0.5"))
MIN_EVIDENCE = int(os.environ.get("ONCA_PORTER_MIN_EVIDENCE", "3"))


def _entity_label(entity: str) -> str:
    return ENTITY_LABELS.get(entity) or str(entity).replace("_", " ").title()


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _bullet_id(entity: str, dim: str, text: str) -> str:
    h = hashlib.sha1(re.sub(r"\s+", " ", text).strip().lower().encode("utf-8")).hexdigest()[:10]
    return f"porter:{entity}:{dim}:{h}"


def _active_swot(belief: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in (belief or {}).get("bullets", [])
            if b.get("status") == "active" and b.get("dimension") in swot_store.DIMENSIONS]


def _collect_evidence_ids(narratives: list[dict[str, Any]], *, max_claims: int = 12) -> list[dict[str, Any]]:
    """Highest-threat narratives for one entity, deduped by claim text."""
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
    """Entities with enough signal to ground a Porter analysis."""
    candidates: list[tuple[str, int]] = []
    for ent in set(beliefs.keys()) | set(narratives_by_ent.keys()):
        if ent in already_proposed:
            continue
        # ADR-006 addendum (#32): own-track evidence — eligibility is gated on the
        # framework's OWN narrative evidence, not on SWOT bullet count. An entity
        # with SWOT coverage but no fresh narratives no longer qualifies.
        n_narr = len(narratives_by_ent.get(ent, []))
        if n_narr >= min_evidence:
            candidates.append((ent, n_narr))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return [ent for ent, _ in candidates[:max_entities]]


# --- draft function (LLM) ---------------------------------------------------
DraftFn = Callable[[str, list[str], list[dict[str, Any]], list[dict[str, Any]]], list[dict[str, Any]]]

_DRAFT_SYSTEM = (
    "You are a competitive-intelligence analyst performing a Porter's Five Forces "
    "analysis for a Brazilian financial-services competitor, to be reviewed by a human. "
    "The five forces are: rivalry (competition intensity among existing players), "
    "new_entrants (threat of new competitors), substitutes (threat of substitute "
    "products/services), buyer_power (bargaining power of customers), supplier_power "
    "(bargaining power of suppliers and regulators). Analyze each force from the "
    "perspective of this competitor in its industry. Each assessment must cite the "
    "evidence indices it draws from. Write in pt-BR, one sentence per assessment. "
    "Be specific to this competitor and its industry. Return ONLY minified JSON, "
    "no prose, no markdown."
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
        "Analyze the five competitive forces for this competitor in its industry.",
        "Return JSON:",
        '{"forces":[{"force":"rivalry|new_entrants|substitutes|buyer_power|supplier_power",'
        '"intensity":"high|medium|low","text":"<pt-BR, one sentence assessment>",'
        '"confidence":0..1,"evidence":[<indices into the evidence list>]}]}',
        f"Rules: at most {MAX_PER_FORCE} assessments per force; omit a force with no "
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
    per_force: dict[str, int] = {}
    for item in data.get("forces") or []:
        if not isinstance(item, dict):
            continue
        force = str(item.get("force") or "").lower()
        text = str(item.get("text") or "").strip()[:280]
        if force not in DIMENSIONS or not text:
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
        if per_force.get(force, 0) >= MAX_PER_FORCE:
            continue
        per_force[force] = per_force.get(force, 0) + 1
        intensity = str(item.get("intensity") or "medium").lower()
        if intensity not in ("high", "medium", "low"):
            intensity = "medium"
        out.append({"force": force, "text": text, "intensity": intensity,
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


# --- pure analysis core -------------------------------------------------------
def analyze_forces(
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
    """Pure: beliefs + narratives -> Porter proposals. No I/O."""
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

        forces = draft_fn(label, industries, swot_bullets, evidence)
        for f in forces:
            if f["confidence"] < min_conf:
                continue
            ev_ids = framework_common.on_signal_ids(f["evidence"], evidence, f["force"], DIM_SIGNALS)
            if not ev_ids:
                continue
            proposals.append({
                "id": _bullet_id(ent, f["force"], f["text"]),
                "kind": "porter",
                "framework": FRAMEWORK,
                "entity": ent,
                "label": label,
                "dimension": f["force"],
                "target_bullet_id": None,
                "target_text": None,
                "text": f["text"],
                "intensity": f["intensity"],
                "evidence": ev_ids,
                "narrative_id": ev_ids[0] if ev_ids else None,
                "date": run_date,
                "confidence": round(f["confidence"], 2),
                "stance_conf": round(f["confidence"], 2),
                "status": "pending",
                "created": run_date,
            })
    return proposals


# --- durable, idempotent store -----------------------------------------------
def _load_json(bucket: str, key: str, s3: Any) -> dict[str, Any]:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}


def proposed_entities(bucket: str, *, s3: Any) -> frozenset[str]:
    prev = _load_json(bucket, PORTER_PROPOSALS_KEY, s3).get("proposals", [])
    return frozenset(p.get("entity") for p in prev if p.get("entity"))


def publish(
    proposals: list[dict[str, Any]], bucket: str, *, s3: Any, as_of: str
) -> dict[str, int]:
    prev = _load_json(bucket, PORTER_PROPOSALS_KEY, s3).get("proposals", [])
    merged = swot_reconcile.merge_proposals(prev, proposals)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    s3.put_object(
        Bucket=bucket, Key=PORTER_PROPOSALS_KEY,
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
    """Analyze Porter's Five Forces for tracked entities."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}
    if not ENABLED:
        return {"statusCode": 200, "body": json.dumps({"status": "disabled"})}

    s3 = boto3.client("s3")
    run_date = run_date_today()
    window = int(os.environ.get("ONCA_PORTER_WINDOW_DAYS",
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

    proposals = analyze_forces(
        beliefs, narratives, industries_map,
        run_date=run_date, draft_fn=llm_draft,
        already_proposed=already,
    )

    counts = {"proposals": 0, "proposals_new": 0, "proposals_pending": 0}
    try:
        counts = publish(proposals, digests_bucket, s3=s3, as_of=run_date)
    except Exception as exc:
        print(f"Warning: Porter publish failed: {exc}")

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
