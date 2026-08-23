"""ADR 004 step 4 — cold-start SWOT seeding (LLM-drafted, analyst-vetted).

Steps 2 & 3 grow a competitor's belief store *bottom-up*: the deterministic axes
attach S/W/O/T evidence (step 2) and the reconcile loop folds news corroboration
onto existing bullets (step 3). But a freshly-tracked entity starts **empty**, and
bottom-up growth is slow — the ADR's "cold start" problem. This step closes it the
way the ADR prescribes (hard-parts #3, sequencing step 4): **LLM-draft an initial
SWOT from the entity's own narrative history + its registry dossier, then let a
human vet it.** Recommended hybrid — "drafted, vetted, then evidence-updated. No
un-vetted bullet is ever asserted."

So this producer is **propose-only**, exactly like step 3's contradict/new path: it
writes `swot/seed_proposals.json` (idempotent, human status preserved) and NEVER an
`active` bullet. The vetting UI (Phase C accounts) promotes an accepted draft into
the belief store; until then the drafts sit read-only in the war room's "Propostas
pendentes de revisão" panel alongside the reconcile proposals.

Precision-first guardrails carried from the belief store's lineage:
- **cold-start only** — an entity is drafted once, and only while its belief store
  is thin (≤ `ONCA_SEED_MAX_EXISTING` active bullets); a well-covered entity is left
  to the bottom-up loop. Idempotent: an entity that already has seed proposals is
  skipped (re-runs never re-draft, never duplicate, preserve any human decision).
- **grounded** — a draft needs ≥ `ONCA_SEED_MIN_HISTORY` real activity narratives to
  draw from; each drafted bullet must cite ≥1 of them (evidence-linked or dropped),
  so the SWOT is never hallucinated from thin air. The dossier (sector, industries,
  license, controllers) is context, not a licence to invent.
- **labeled inference, about the competitor's public competitive posture** — no
  claims about named individuals, no CPF; the same defamation surface as the
  relational axis, handled the same way (review-gated).

The core `seed_entities()` is pure — it takes a `draft_fn`, so tests drive it with
deterministic fakes (no Bedrock). This is derived state: it never writes narratives,
and its one durable store is idempotent.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from typing import Any, Callable

import boto3

from src.synth import feature_store, swot_reconcile
from src.synth.bedrock_llm import DEFAULT_SYNTH_MODEL, converse
from src.synth.synthesize import ENTITY_LABELS, run_at_now, run_date_today

SEED_PROPOSALS_KEY = "swot/seed_proposals.json"

DIMENSIONS = ("S", "W", "O", "T")

# Cost/precision gates (env-tunable).
ENABLED = os.environ.get("ONCA_SEED_ENABLED", "1") not in ("0", "false", "False")
MIN_HISTORY = int(os.environ.get("ONCA_SEED_MIN_HISTORY", "3"))   # ground the draft
MAX_EXISTING = int(os.environ.get("ONCA_SEED_MAX_EXISTING", "2"))  # cold-start only
MAX_ENTITIES = int(os.environ.get("ONCA_SEED_MAX_ENTITIES", "8"))  # per-run cost cap
MAX_PER_DIM = int(os.environ.get("ONCA_SEED_MAX_PER_DIM", "2"))    # small, balanced
MAX_CLAIMS = int(os.environ.get("ONCA_SEED_HISTORY_CLAIMS", "12"))  # evidence fed
MIN_CONF = float(os.environ.get("ONCA_SEED_MIN_CONF", "0.5"))      # draft floor


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _entity_label(entity: str) -> str:
    return ENTITY_LABELS.get(entity) or str(entity).replace("_", " ").title()


def _active_bullets(belief: dict[str, Any]) -> int:
    return sum(1 for b in (belief or {}).get("bullets", []) if b.get("status") == "active")


def dossier_of(entity: str, registry: dict[str, Any] | None) -> dict[str, Any]:
    """Compact registry facts that ground a draft (safe on a missing record)."""
    r = registry or {}
    return {
        "label": r.get("display_name") or _entity_label(entity),
        "sector": r.get("sector"),
        "industries": list(r.get("industries") or []),
        "license_class": r.get("license_class"),
        "ticker": r.get("ticker"),
        "controllers": list(r.get("controllers") or []),
    }


def collect_evidence(
    narratives: list[dict[str, Any]], *, max_claims: int = MAX_CLAIMS
) -> list[dict[str, Any]]:
    """Highest-threat activity claims for one entity → the grounding fed to the LLM.

    Reuses step 3's `key_claim` (leading sentences, URLs stripped) and dedups by
    claim text so a repeated daily fusion card doesn't crowd out variety.
    """
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
                    "threat_score": _score(n.get("threat_score"))})
        if len(out) >= max_claims:
            break
    return out


def _bullet_id(entity: str, dim: str, text: str) -> str:
    h = hashlib.sha1(re.sub(r"\s+", " ", text).strip().lower().encode("utf-8")).hexdigest()[:10]
    return f"seed:{entity}:{dim}:{h}"


# --- draft function (LLM) ---------------------------------------------------
DraftFn = Callable[[str, dict[str, Any], list[dict[str, Any]]], list[dict[str, Any]]]

_DRAFT_SYSTEM = (
    "You are a competitive-intelligence analyst drafting an INITIAL SWOT for a "
    "Brazilian financial-services competitor, to be reviewed by a human before use. "
    "SWOT is about the COMPETITOR: S/W are its internal strengths/weaknesses, O/T are "
    "external opportunities/threats acting on it. Draft ONLY from the evidence and "
    "dossier provided — never invent facts, never make claims about named individuals. "
    "Be conservative and specific; prefer fewer, well-grounded bullets. Every bullet "
    "MUST cite the evidence item indices it draws from. Write bullet text in pt-BR, "
    "one sentence each. Return ONLY minified JSON, no prose, no markdown."
)


def _draft_prompt(dossier: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    lines = [
        f"Competitor: {dossier.get('label')}",
        f"Sector: {dossier.get('sector') or '(unknown)'}",
        f"Industries: {', '.join(dossier.get('industries') or []) or '(unknown)'}",
        f"License: {dossier.get('license_class') or '(unknown)'}",
        f"Controllers: {', '.join(dossier.get('controllers') or []) or '(unknown)'}",
        "",
        "Evidence (recent signals about this competitor, pt-BR):",
    ]
    for i, e in enumerate(evidence):
        lines.append(f"[{i}] ({e['date']}) {e['claim']}")
    lines += [
        "",
        "Draft a small, balanced SWOT. Return JSON with this exact shape:",
        '{"bullets":[{"dimension":"S|W|O|T","text":"<pt-BR, one sentence>",'
        '"confidence":0..1,"evidence":[<indices into the list above>]}]}',
        f"Rules: at most {MAX_PER_DIM} bullets per dimension; omit a dimension with "
        "no support. Each bullet MUST include >=1 valid evidence index. text in pt-BR, "
        "about the competitor's competitive posture, one sentence, no URLs, no personal "
        "names.",
    ]
    return "\n".join(lines)


def _parse_draft(raw: str | None, n_evidence: int) -> list[dict[str, Any]]:
    """Defensive JSON extraction. Drops malformed / unevidenced bullets."""
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
    for item in data.get("bullets") or []:
        if not isinstance(item, dict):
            continue
        dim = str(item.get("dimension") or "").upper()
        text = str(item.get("text") or "").strip()[:280]
        if dim not in DIMENSIONS or not text:
            continue
        idx = []
        for j in item.get("evidence") or []:
            try:
                j = int(j)
            except (TypeError, ValueError):
                continue
            if 0 <= j < n_evidence and j not in idx:
                idx.append(j)
        if not idx:  # evidence-linked or dropped (anti-hallucination)
            continue
        if per_dim.get(dim, 0) >= MAX_PER_DIM:
            continue
        per_dim[dim] = per_dim.get(dim, 0) + 1
        out.append({"dimension": dim, "text": text,
                    "confidence": _score(item.get("confidence")), "evidence": idx})
    return out


def llm_draft(
    label: str, dossier: dict[str, Any], evidence: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Real draft_fn: one bounded Converse call to draft the cold-start SWOT."""
    raw = converse(
        _draft_prompt(dossier, evidence),
        model_id=DEFAULT_SYNTH_MODEL,
        system=_DRAFT_SYSTEM,
        max_tokens=900,
    )
    return _parse_draft(raw, len(evidence))


# --- pure seeding core ------------------------------------------------------
def seed_entities(
    narratives: list[dict[str, Any]],
    beliefs: dict[str, dict[str, Any]],
    dossiers: dict[str, dict[str, Any]],
    *,
    run_date: str,
    draft_fn: DraftFn,
    already_seeded: frozenset[str] = frozenset(),
    min_history: int = MIN_HISTORY,
    max_existing: int = MAX_EXISTING,
    max_entities: int = MAX_ENTITIES,
    min_conf: float = MIN_CONF,
) -> list[dict[str, Any]]:
    """Pure: draft cold-start SWOT proposals. No I/O. `draft_fn` is injected.

    Selects entities that are (a) thin in the belief store (≤ max_existing active
    bullets), (b) grounded (≥ min_history activity narratives), (c) not already
    seeded — most-grounded first, capped at max_entities. Each returned proposal is
    `kind="seed"`, `status="pending"`, evidence-linked, never auto-applied.
    """
    by_ent: dict[str, list[dict[str, Any]]] = {}
    for n in narratives:
        if not isinstance(n, dict) or not feature_store.is_activity_narrative(n):
            continue
        ent = n.get("entity")
        if ent:
            by_ent.setdefault(ent, []).append(n)

    candidates = [
        ent for ent, ns in by_ent.items()
        if ent not in already_seeded
        and len(ns) >= min_history
        and _active_bullets(beliefs.get(ent) or {}) <= max_existing
    ]
    # Most-grounded first (best drafts), capped for cost.
    candidates.sort(key=lambda e: len(by_ent[e]), reverse=True)
    candidates = candidates[:max_entities]

    proposals: list[dict[str, Any]] = []
    for ent in candidates:
        evidence = collect_evidence(by_ent[ent])
        if len(evidence) < min_history:
            continue
        dossier = dossiers.get(ent) or dossier_of(ent, None)
        label = dossier.get("label") or _entity_label(ent)
        for b in draft_fn(label, dossier, evidence):
            if b["confidence"] < min_conf:
                continue
            ev_ids = [evidence[j]["id"] for j in b["evidence"] if j < len(evidence)]
            if not ev_ids:
                continue
            proposals.append({
                "id": _bullet_id(ent, b["dimension"], b["text"]),
                "kind": "seed",
                "entity": ent,
                "label": label,
                "dimension": b["dimension"],
                "target_bullet_id": None,
                "target_text": None,
                "text": b["text"],
                "evidence": ev_ids,
                "narrative_id": ev_ids[0],
                "date": run_date,
                "confidence": round(b["confidence"], 2),
                "stance_conf": round(b["confidence"], 2),  # frontend meta parity
                "status": "pending",
                "created": run_date,
            })
    return proposals


# --- durable, idempotent store ----------------------------------------------
def _load_json(bucket: str, key: str, s3: Any) -> dict[str, Any]:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body.decode("utf-8"))
    except Exception:  # pragma: no cover - absent/unreadable
        return {}


def seeded_entities(bucket: str, *, s3: Any) -> frozenset[str]:
    """Entities that already carry a seed proposal (any status) — never re-drafted."""
    prev = _load_json(bucket, SEED_PROPOSALS_KEY, s3).get("proposals", [])
    return frozenset(p.get("entity") for p in prev if p.get("entity"))


def publish(
    proposals: list[dict[str, Any]], bucket: str, *, s3: Any, as_of: str
) -> dict[str, int]:
    """Merge into the durable store and overwrite it (idempotent, human status kept)."""
    prev = _load_json(bucket, SEED_PROPOSALS_KEY, s3).get("proposals", [])
    merged = swot_reconcile.merge_proposals(prev, proposals)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    s3.put_object(
        Bucket=bucket, Key=SEED_PROPOSALS_KEY,
        Body=json.dumps({"generated_at": now, "as_of": as_of,
                         "proposals": merged}, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json", CacheControl="no-cache",
    )
    return {
        "proposals": len(merged),
        "proposals_new": len(merged) - len(prev),
        "proposals_pending": sum(1 for p in merged if p.get("status") == "pending"),
    }


def _load_registry(entities: set[str]) -> dict[str, dict[str, Any]]:
    """Per-entity dossier facts from the registry (best-effort; {} if unavailable)."""
    out: dict[str, dict[str, Any]] = {}
    try:
        from src.synth import entity_registry

        for ent in entities:
            rec = entity_registry.get_entity(ent)
            if rec:
                out[ent] = dossier_of(ent, rec)
    except Exception as exc:  # pragma: no cover - registry best-effort
        print(f"Warning: seed dossier load failed: {exc}")
    return out


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Draft cold-start SWOT proposals for thin, well-grounded entities."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}
    if not ENABLED:
        return {"statusCode": 200, "body": json.dumps({"status": "disabled"})}

    s3 = boto3.client("s3")
    run_date = run_date_today()
    window = int(os.environ.get("ONCA_SEED_WINDOW_DAYS",
                                os.environ.get("ONCA_FEATURE_WINDOW_DAYS", "90")))

    narratives = feature_store.load_history(digests_bucket, window, s3=s3)
    entities = {n["entity"] for n in narratives
                if isinstance(n, dict) and feature_store.is_activity_narrative(n) and n.get("entity")}
    beliefs = swot_reconcile.load_beliefs(digests_bucket, entities, s3=s3)
    dossiers = _load_registry(entities)
    already = seeded_entities(digests_bucket, s3=s3)

    proposals = seed_entities(
        narratives, beliefs, dossiers,
        run_date=run_date, draft_fn=llm_draft, already_seeded=already,
    )

    counts = {"proposals": 0, "proposals_new": 0, "proposals_pending": 0}
    try:
        counts = publish(proposals, digests_bucket, s3=s3, as_of=run_date)
    except Exception as exc:  # pragma: no cover - publish best-effort
        print(f"Warning: seed publish failed: {exc}")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "status": "ok",
            "as_of": run_date,
            "entities_considered": len(entities),
            "already_seeded": len(already),
            "proposals_found": len(proposals),
            "run_at": run_at_now(),
            **counts,
        }),
    }
