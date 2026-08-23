"""ADR 006 — TOWS Matrix: strategic posture pairing over vetted SWOT beliefs.

TOWS is a pure transform over the SWOT belief store — no new ingestion. It reads
an entity's **active** SWOT bullets and asks an LLM to synthesize paired strategic
postures across the four TOWS quadrants:

    SO — maximize: use strengths to exploit opportunities
    ST — counter:  use strengths to mitigate threats
    WO — overcome: address weaknesses by pursuing opportunities
    WT — avoid:    minimize weaknesses and avoid threats

Each TOWS bullet inherits the evidence (citations) of the paired SWOT bullets it
draws from. Everything is PROPOSED ONLY (propose→vet, never auto-asserted) and
flows through the same Phase-C vetting UI as SWOT proposals. An approved TOWS
bullet becomes a curated belief with `framework="tows"` in `swot/curated.json`.

Only entities with at least one active S-or-W bullet AND one active O-or-T bullet
are candidates. The pairing is deterministic; the synthesis is one bounded LLM
call per entity. Cost is capped by `ONCA_TOWS_MAX_ENTITIES`.

The core `pair_postures()` is pure — it takes a `draft_fn`, so tests drive it with
deterministic fakes (no Bedrock).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from typing import Any, Callable

import boto3

from src.synth import swot_reconcile, swot_store
from src.synth.bedrock_llm import DEFAULT_SYNTH_MODEL, converse
from src.synth.synthesize import ENTITY_LABELS, run_at_now, run_date_today

TOWS_PROPOSALS_KEY = "tows/proposals.json"
FRAMEWORK = "tows"
DIMENSIONS = ("SO", "ST", "WO", "WT")

PAIRS = {
    "SO": ("S", "O"),
    "ST": ("S", "T"),
    "WO": ("W", "O"),
    "WT": ("W", "T"),
}
PAIR_LABELS = {
    "SO": "Maximizar: usar forças para explorar oportunidades",
    "ST": "Contra-atacar: usar forças para mitigar ameaças",
    "WO": "Superar: abordar fraquezas ao perseguir oportunidades",
    "WT": "Evitar: minimizar fraquezas e evitar ameaças",
}

ENABLED = os.environ.get("ONCA_TOWS_ENABLED", "1") not in ("0", "false", "False")
MAX_ENTITIES = int(os.environ.get("ONCA_TOWS_MAX_ENTITIES", "8"))
MAX_PER_QUAD = int(os.environ.get("ONCA_TOWS_MAX_PER_QUAD", "2"))
MIN_CONF = float(os.environ.get("ONCA_TOWS_MIN_CONF", "0.5"))


def _entity_label(entity: str) -> str:
    return ENTITY_LABELS.get(entity) or str(entity).replace("_", " ").title()


def _bullet_id(entity: str, dim: str, text: str) -> str:
    h = hashlib.sha1(re.sub(r"\s+", " ", text).strip().lower().encode("utf-8")).hexdigest()[:10]
    return f"tows:{entity}:{dim}:{h}"


def _active_bullets(belief: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in (belief or {}).get("bullets", [])
            if b.get("status") == "active" and b.get("dimension") in swot_store.DIMENSIONS]


def _collect_evidence(bullets: list[dict[str, Any]]) -> list[str]:
    """Merge evidence ids from multiple SWOT bullets (deduped, preserving order)."""
    seen: set[str] = set()
    out: list[str] = []
    for b in bullets:
        for e in b.get("evidence") or []:
            eid = e.get("id") if isinstance(e, dict) else str(e)
            if eid and eid not in seen:
                seen.add(eid)
                out.append(eid)
    return out


def eligible_entities(
    beliefs: dict[str, dict[str, Any]],
    *,
    already_proposed: frozenset[str] = frozenset(),
    max_entities: int = MAX_ENTITIES,
) -> list[str]:
    """Entities that have at least one active S-or-W AND one active O-or-T."""
    candidates: list[tuple[str, int]] = []
    for ent, belief in beliefs.items():
        if ent in already_proposed:
            continue
        active = _active_bullets(belief)
        has_internal = any(b["dimension"] in ("S", "W") for b in active)
        has_external = any(b["dimension"] in ("O", "T") for b in active)
        if has_internal and has_external:
            candidates.append((ent, len(active)))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return [ent for ent, _ in candidates[:max_entities]]


# --- draft function (LLM) ---------------------------------------------------
DraftFn = Callable[[str, dict[str, list[dict[str, Any]]]], list[dict[str, Any]]]

_DRAFT_SYSTEM = (
    "You are a competitive-intelligence analyst synthesizing a TOWS strategic-posture "
    "matrix for a Brazilian financial-services competitor, to be reviewed by a human. "
    "TOWS pairs existing SWOT beliefs into strategic postures: SO (use strengths to "
    "exploit opportunities), ST (use strengths to counter threats), WO (overcome "
    "weaknesses via opportunities), WT (minimize weaknesses and avoid threats). "
    "Each posture must reference the specific S/W/O/T beliefs it pairs — use their "
    "indices. Write posture text in pt-BR, one actionable sentence each. Be specific "
    "to this competitor. Return ONLY minified JSON, no prose, no markdown."
)


def _draft_prompt(label: str, by_dim: dict[str, list[dict[str, Any]]]) -> str:
    lines = [f"Competitor: {label}", "", "Active SWOT beliefs:"]
    idx = 0
    index_map: dict[str, int] = {}
    for dim in ("S", "W", "O", "T"):
        for b in by_dim.get(dim, []):
            index_map[b.get("id", str(idx))] = idx
            lines.append(f"[{idx}] ({dim}) {b.get('text', '')}")
            idx += 1
    lines += [
        "",
        "Synthesize TOWS postures. Return JSON:",
        '{"postures":[{"quadrant":"SO|ST|WO|WT","text":"<pt-BR, one actionable sentence>",'
        '"confidence":0..1,"pairs":[<indices of the S/W and O/T beliefs this posture draws from>]}]}',
        f"Rules: at most {MAX_PER_QUAD} postures per quadrant; omit a quadrant with no "
        "meaningful pairing. Each posture MUST reference >=2 belief indices (one internal "
        "S/W + one external O/T). text in pt-BR, specific to the competitor, one sentence.",
    ]
    return "\n".join(lines), index_map


def _parse_draft(raw: str | None, n_beliefs: int) -> list[dict[str, Any]]:
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
    per_quad: dict[str, int] = {}
    for item in data.get("postures") or []:
        if not isinstance(item, dict):
            continue
        quad = str(item.get("quadrant") or "").upper()
        text = str(item.get("text") or "").strip()[:280]
        if quad not in DIMENSIONS or not text:
            continue
        pairs = []
        for j in item.get("pairs") or []:
            try:
                j = int(j)
            except (TypeError, ValueError):
                continue
            if 0 <= j < n_beliefs and j not in pairs:
                pairs.append(j)
        if len(pairs) < 2:
            continue
        if per_quad.get(quad, 0) >= MAX_PER_QUAD:
            continue
        per_quad[quad] = per_quad.get(quad, 0) + 1
        out.append({"quadrant": quad, "text": text,
                    "confidence": _score(item.get("confidence")), "pairs": pairs})
    return out


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def llm_draft(label: str, by_dim: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    prompt, _ = _draft_prompt(label, by_dim)
    n = sum(len(bs) for bs in by_dim.values())
    raw = converse(
        prompt,
        model_id=DEFAULT_SYNTH_MODEL,
        system=_DRAFT_SYSTEM,
        max_tokens=800,
    )
    return _parse_draft(raw, n)


# --- pure pairing core -------------------------------------------------------
def pair_postures(
    beliefs: dict[str, dict[str, Any]],
    *,
    run_date: str,
    draft_fn: DraftFn,
    already_proposed: frozenset[str] = frozenset(),
    max_entities: int = MAX_ENTITIES,
    min_conf: float = MIN_CONF,
) -> list[dict[str, Any]]:
    """Pure: beliefs -> TOWS proposals. No I/O. `draft_fn` is injected."""
    entities = eligible_entities(beliefs, already_proposed=already_proposed,
                                 max_entities=max_entities)
    proposals: list[dict[str, Any]] = []

    for ent in entities:
        belief = beliefs[ent]
        active = _active_bullets(belief)
        label = belief.get("label") or _entity_label(ent)

        by_dim: dict[str, list[dict[str, Any]]] = {}
        for b in active:
            by_dim.setdefault(b["dimension"], []).append(b)

        all_bullets = []
        for dim in ("S", "W", "O", "T"):
            all_bullets.extend(by_dim.get(dim, []))

        postures = draft_fn(label, by_dim)
        for p in postures:
            if p["confidence"] < min_conf:
                continue
            paired_bullets = [all_bullets[j] for j in p["pairs"] if j < len(all_bullets)]
            if len(paired_bullets) < 2:
                continue
            ev_ids = _collect_evidence(paired_bullets)

            proposals.append({
                "id": _bullet_id(ent, p["quadrant"], p["text"]),
                "kind": "tows",
                "framework": FRAMEWORK,
                "entity": ent,
                "label": label,
                "dimension": p["quadrant"],
                "target_bullet_id": None,
                "target_text": None,
                "text": p["text"],
                "paired_beliefs": [b.get("id") for b in paired_bullets],
                "evidence": ev_ids,
                "narrative_id": ev_ids[0] if ev_ids else None,
                "date": run_date,
                "confidence": round(p["confidence"], 2),
                "stance_conf": round(p["confidence"], 2),
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
    """Entities that already carry a TOWS proposal (any status) — not re-drafted."""
    prev = _load_json(bucket, TOWS_PROPOSALS_KEY, s3).get("proposals", [])
    return frozenset(p.get("entity") for p in prev if p.get("entity"))


def publish(
    proposals: list[dict[str, Any]], bucket: str, *, s3: Any, as_of: str
) -> dict[str, int]:
    prev = _load_json(bucket, TOWS_PROPOSALS_KEY, s3).get("proposals", [])
    merged = swot_reconcile.merge_proposals(prev, proposals)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    s3.put_object(
        Bucket=bucket, Key=TOWS_PROPOSALS_KEY,
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
    """Pair TOWS strategic postures from active SWOT beliefs."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}
    if not ENABLED:
        return {"statusCode": 200, "body": json.dumps({"status": "disabled"})}

    s3 = boto3.client("s3")
    run_date = run_date_today()

    index = _load_json(digests_bucket, swot_store.INDEX_KEY, s3)
    entity_keys = set((index.get("entities") or {}).keys())
    beliefs = swot_reconcile.load_beliefs(digests_bucket, entity_keys, s3=s3)
    already = proposed_entities(digests_bucket, s3=s3)

    proposals = pair_postures(
        beliefs, run_date=run_date, draft_fn=llm_draft,
        already_proposed=already,
    )

    counts = {"proposals": 0, "proposals_new": 0, "proposals_pending": 0}
    try:
        counts = publish(proposals, digests_bucket, s3=s3, as_of=run_date)
    except Exception as exc:
        print(f"Warning: TOWS publish failed: {exc}")

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
