"""ADR 004 step 3 — reconcile-against-belief (LLM stance) for free-text narratives.

Step 2 built the belief store from the *deterministic* axes (comparative/cohort/
thematic/longitudinal/silence) — no LLM. But the richest signal, the news & fatos
narratives, carries no `swot_hint`: mapping "Nubank processado por vazamento de
dados" onto the right S/W/O/T bullet is a stance/NLI judgement, not a rule. That is
what this step adds — the loop ADR 004 calls **reconcile-against-belief**:

    1. embed each surfaced free-text narrative's key claim (Titan),
    2. cosine-retrieve the top-k nearest existing bullets for that entity,
    3. LLM stance-classify each (claim, bullet) pair → reinforces | contradicts |
       unrelated, and — if nothing relates — propose ONE new bullet,
    4. apply:
       - **reinforces** → auto-apply: a durable evidence record (`swot/reinforcements.json`)
         that `swot_store` folds onto the matching bullet next rebuild (confidence up),
       - **contradicts** / **new** → **propose only** (`swot/proposals.json`), never
         auto-applied — precision-first, exactly as ADR 004 requires (a wrong NLI must
         not retire a true belief). The vetting UI is Phase C; the queue is built now.

Cost is bounded the way ADR 004 prescribes: only narratives that *surfaced this run*
are considered (highest-threat first, env-capped), retrieval is top-k not all-pairs,
and every embedding is content-hash cached (`swot/embcache.json`) so stable texts are
never re-embedded. The core `reconcile()` is pure — it takes an `embed_fn` and a
`stance_fn`, so tests drive it with deterministic fakes (no Bedrock).

This is derived state, not narratives: it never writes to `narratives/`, and the two
durable stores it maintains (reinforcements, proposals) are idempotent — re-running a
day adds no duplicates and preserves any human status already on a proposal.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from collections import defaultdict
from typing import Any, Callable

import boto3

from src.synth import embeddings, feature_store
from src.synth.bedrock_llm import DEFAULT_SYNTH_MODEL, converse
from src.synth.synthesize import ENTITY_LABELS, run_at_now, run_date_today

REINFORCEMENTS_KEY = "swot/reinforcements.json"
PROPOSALS_KEY = "swot/proposals.json"
EMBCACHE_KEY = "swot/embcache.json"

DIMENSIONS = ("S", "W", "O", "T")

# Cost/precision gates (env-tunable). Reconcile only free-text narratives that
# surfaced today; embed+judge the top-k nearest bullets; accept a stance only
# above the confidence floor.
MAX_CANDIDATES = int(os.environ.get("ONCA_RECONCILE_MAX", "40"))
TOP_K = int(os.environ.get("ONCA_RECONCILE_TOP_K", "4"))
MIN_SIM = float(os.environ.get("ONCA_RECONCILE_MIN_SIM", "0.30"))
REINFORCE_MIN = float(os.environ.get("ONCA_RECONCILE_REINFORCE_MIN", "0.55"))
CONTRADICT_MIN = float(os.environ.get("ONCA_RECONCILE_CONTRADICT_MIN", "0.60"))
NEW_MIN = float(os.environ.get("ONCA_RECONCILE_NEW_MIN", "0.60"))
MIN_CLAIM_LEN = 24


def _date_of(n: dict[str, Any]) -> str:
    return feature_store._date_of(n)


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _entity_label(entity: str) -> str:
    return ENTITY_LABELS.get(entity) or str(entity).replace("_", " ").title()


def key_claim(narrative: dict[str, Any]) -> str:
    """The narrative's leading claim (first ~2 sentences) — what we embed & judge."""
    text = re.sub(r"\s+", " ", str(narrative.get("narrative") or "")).strip()
    text = re.sub(r"https?://\S+", "", text).strip()  # drop cited URLs from the claim
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(sentences[:2]).strip()[:600]


def select_candidates(
    narratives: list[dict[str, Any]], run_date: str, *, max_candidates: int = MAX_CANDIDATES
) -> list[dict[str, Any]]:
    """Free-text, entity-scoped narratives that surfaced this run (highest threat first).

    Excludes the derived/inference axes (they already fed the belief store
    deterministically in step 2) via feature_store.is_activity_narrative.
    """
    out: list[dict[str, Any]] = []
    for n in narratives:
        if not isinstance(n, dict):
            continue
        if not feature_store.is_activity_narrative(n):
            continue
        if not n.get("entity"):
            continue
        if _date_of(n) != run_date:
            continue
        if len(key_claim(n)) < MIN_CLAIM_LEN:
            continue
        out.append(n)
    out.sort(key=lambda n: _score(n.get("threat_score")), reverse=True)
    return out[:max_candidates]


def _bullets_of(belief: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic step-2 bullets we can reconcile against (need source_key + text)."""
    return [
        b for b in (belief or {}).get("bullets", [])
        if b.get("source_key") and b.get("text") and b.get("dimension") in DIMENSIONS
    ]


# --- stance function (LLM) --------------------------------------------------
StanceFn = Callable[[str, str, list[dict[str, Any]]], dict[str, Any]]
EmbedFn = Callable[[list[str]], dict[str, list[float]]]

_STANCE_SYSTEM = (
    "You are a competitive-intelligence analyst judging whether a news signal about a "
    "Brazilian financial-services competitor supports, contradicts, or is unrelated to "
    "each of several existing SWOT beliefs about that competitor. SWOT is about the "
    "COMPETITOR: S/W are its internal strengths/weaknesses, O/T are external "
    "opportunities/threats acting on it. Be conservative: answer 'reinforces' or "
    "'contradicts' only when the signal clearly bears on the belief; otherwise "
    "'unrelated'. Return ONLY minified JSON, no prose, no markdown."
)


def _stance_prompt(entity_label: str, claim: str, bullets: list[dict[str, Any]]) -> str:
    lines = [
        f"Competitor: {entity_label}",
        f"News signal (pt-BR): {claim}",
        "",
        "Existing SWOT beliefs (judge the signal against EACH):",
    ]
    for i, b in enumerate(bullets):
        lines.append(f"[{i}] ({b['dimension']}) {b['text']}")
    if not bullets:
        lines.append("(none yet for this competitor)")
    lines += [
        "",
        "Return JSON with this exact shape:",
        '{"matches":[{"i":<index>,"stance":"reinforces|contradicts|unrelated",'
        '"confidence":0..1}],'
        '"new_bullet":{"dimension":"S|W|O|T","text":"<pt-BR, one sentence>",'
        '"confidence":0..1} or null}',
        "Rules: include one match object per listed belief. Propose new_bullet ONLY "
        "when the signal is materially SWOT-relevant AND no belief reinforces/"
        "contradicts it; otherwise new_bullet is null. Keep new_bullet.text in pt-BR, "
        "about the competitor, one sentence, no URLs.",
    ]
    return "\n".join(lines)


def _parse_stance(raw: str | None, n_bullets: int) -> dict[str, Any]:
    """Defensive JSON extraction from the model's reply. Safe empty result on failure."""
    empty = {"matches": [], "new_bullet": None}
    if not raw:
        return empty
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return empty
    try:
        data = json.loads(m.group(0))
    except Exception:
        return empty
    matches = []
    for item in data.get("matches") or []:
        try:
            i = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        if not 0 <= i < n_bullets:
            continue
        stance = str(item.get("stance") or "unrelated").lower()
        if stance not in ("reinforces", "contradicts", "unrelated"):
            stance = "unrelated"
        matches.append({"i": i, "stance": stance, "confidence": _score(item.get("confidence"))})
    nb = data.get("new_bullet")
    new_bullet = None
    if isinstance(nb, dict) and str(nb.get("dimension")) in DIMENSIONS and (nb.get("text") or "").strip():
        new_bullet = {
            "dimension": str(nb["dimension"]),
            "text": str(nb["text"]).strip()[:280],
            "confidence": _score(nb.get("confidence")),
        }
    return {"matches": matches, "new_bullet": new_bullet}


def llm_stance(entity_label: str, claim: str, bullets: list[dict[str, Any]]) -> dict[str, Any]:
    """Real stance_fn: one bounded Converse call over the k near bullets."""
    raw = converse(
        _stance_prompt(entity_label, claim, bullets),
        model_id=DEFAULT_SYNTH_MODEL,
        system=_STANCE_SYSTEM,
        max_tokens=500,
    )
    return _parse_stance(raw, len(bullets))


# --- pure reconcile core ----------------------------------------------------
def reconcile(
    narratives: list[dict[str, Any]],
    beliefs: dict[str, dict[str, Any]],
    *,
    run_date: str,
    embed_fn: EmbedFn,
    stance_fn: StanceFn,
    top_k: int = TOP_K,
    min_sim: float = MIN_SIM,
    max_candidates: int = MAX_CANDIDATES,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pure: (narratives, beliefs) -> (reinforcements, proposals). No I/O.

    `embed_fn(texts)->{text:vec}` and `stance_fn(label,claim,bullets)->stance dict`
    are injected (real ones use Bedrock; tests use deterministic fakes).
    """
    candidates = select_candidates(narratives, run_date, max_candidates=max_candidates)
    by_ent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for n in candidates:
        by_ent[n["entity"]].append(n)

    reinforcements: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []

    for ent, cands in by_ent.items():
        bullets = _bullets_of(beliefs.get(ent) or {})
        label = (beliefs.get(ent) or {}).get("label") or _entity_label(ent)

        # Embed all bullet + claim texts for this entity in one cached batch.
        claim_by_n = {n["id"]: key_claim(n) for n in cands if n.get("id")}
        vecs = embed_fn(list({b["text"] for b in bullets} | set(claim_by_n.values())))

        bullet_vecs = [(b, vecs[b["text"]]) for b in bullets if b["text"] in vecs]

        for n in cands:
            nid = n.get("id")
            claim = claim_by_n.get(nid)
            if not nid or not claim:
                continue
            near = embeddings.top_k(
                vecs.get(claim), bullet_vecs, k=top_k, min_sim=min_sim
            )
            near_bullets = [b for b, _ in near]
            sim_by_id = {b["id"]: s for b, s in near}
            result = stance_fn(label, claim, near_bullets)

            related = False
            for m in result.get("matches") or []:
                b = near_bullets[m["i"]]
                conf = m["confidence"]
                if m["stance"] == "reinforces" and conf >= REINFORCE_MIN:
                    related = True
                    reinforcements.append({
                        "entity": ent,
                        "base_key": b["source_key"],
                        "dimension": b["dimension"],
                        "bullet_id": b["id"],
                        "narrative_id": nid,
                        "date": _date_of(n),
                        "stance_conf": round(conf, 2),
                        "sim": round(sim_by_id.get(b["id"], 0.0), 3),
                    })
                elif m["stance"] == "contradicts" and conf >= CONTRADICT_MIN:
                    related = True
                    proposals.append({
                        "id": f"challenge:{ent}:{b['id']}:{nid}",
                        "kind": "challenge",
                        "entity": ent,
                        "label": label,
                        "dimension": b["dimension"],
                        "target_bullet_id": b["id"],
                        "target_text": b["text"],
                        "text": f"Sinal recente contradiz a crença: {claim}",
                        "narrative_id": nid,
                        "date": _date_of(n),
                        "stance_conf": round(conf, 2),
                        "status": "pending",
                        "created": run_date,
                    })

            nb = result.get("new_bullet")
            if nb and not related and nb["confidence"] >= NEW_MIN:
                proposals.append({
                    "id": f"new:{ent}:{nid}",
                    "kind": "new",
                    "entity": ent,
                    "label": label,
                    "dimension": nb["dimension"],
                    "target_bullet_id": None,
                    "target_text": None,
                    "text": nb["text"],
                    "narrative_id": nid,
                    "date": _date_of(n),
                    "stance_conf": round(nb["confidence"], 2),
                    "status": "pending",
                    "created": run_date,
                })

    return reinforcements, proposals


# --- durable, idempotent stores ---------------------------------------------
def _load_json(bucket: str, key: str, s3: Any) -> dict[str, Any]:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body.decode("utf-8"))
    except Exception:  # pragma: no cover - absent/unreadable
        return {}


def merge_reinforcements(
    existing: list[dict[str, Any]], fresh: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Accumulate reinforcement evidence, deduped by (narrative_id, bullet_id)."""
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for r in list(existing or []) + list(fresh or []):
        by_key[(r.get("narrative_id"), r.get("bullet_id"))] = r
    return sorted(by_key.values(), key=lambda r: r.get("date") or "", reverse=True)


def merge_proposals(
    existing: list[dict[str, Any]], fresh: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Accumulate proposals by stable id; preserve any human status already set."""
    by_id: dict[str, dict[str, Any]] = {p["id"]: p for p in (existing or []) if p.get("id")}
    for p in fresh:
        prior = by_id.get(p["id"])
        if prior:
            # keep the human-set status + original created date; refresh evidence view
            merged = {**p, "status": prior.get("status", "pending"),
                      "created": prior.get("created", p.get("created"))}
            by_id[p["id"]] = merged
        else:
            by_id[p["id"]] = p
    return sorted(by_id.values(), key=lambda p: (p.get("date") or "", p.get("id")), reverse=True)


def publish(
    reinforcements: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    bucket: str,
    *,
    s3: Any,
    as_of: str,
    embcache: dict[str, list[float]] | None = None,
) -> dict[str, int]:
    """Merge into the durable stores and overwrite them (idempotent)."""
    prev_r = _load_json(bucket, REINFORCEMENTS_KEY, s3).get("reinforcements", [])
    prev_p = _load_json(bucket, PROPOSALS_KEY, s3).get("proposals", [])
    merged_r = merge_reinforcements(prev_r, reinforcements)
    merged_p = merge_proposals(prev_p, proposals)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    s3.put_object(
        Bucket=bucket, Key=REINFORCEMENTS_KEY,
        Body=json.dumps({"generated_at": now, "as_of": as_of,
                         "reinforcements": merged_r}, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json", CacheControl="no-cache",
    )
    s3.put_object(
        Bucket=bucket, Key=PROPOSALS_KEY,
        Body=json.dumps({"generated_at": now, "as_of": as_of,
                         "proposals": merged_p}, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json", CacheControl="no-cache",
    )
    if embcache is not None:
        try:
            s3.put_object(
                Bucket=bucket, Key=EMBCACHE_KEY,
                Body=json.dumps(embcache).encode("utf-8"),
                ContentType="application/json", CacheControl="no-cache",
            )
        except Exception as exc:  # pragma: no cover - cache is best-effort
            print(f"Warning: embcache publish failed: {exc}")

    return {
        "reinforcements": len(merged_r),
        "reinforcements_new": len(merged_r) - len(prev_r),
        "proposals": len(merged_p),
        "proposals_pending": sum(1 for p in merged_p if p.get("status") == "pending"),
    }


def load_beliefs(bucket: str, entities: set[str], *, s3: Any) -> dict[str, dict[str, Any]]:
    """Read the per-entity belief files swot/{entity}.json (from step 2's builder)."""
    from src.synth import swot_store

    out: dict[str, dict[str, Any]] = {}
    for ent in entities:
        b = _load_json(bucket, f"{swot_store.SWOT_PREFIX}{ent}.json", s3)
        if b:
            out[ent] = b
    return out


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Reconcile today's free-text narratives against the per-entity belief store."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}
    if os.environ.get("ONCA_RECONCILE_ENABLED", "1") not in ("1", "true", "True"):
        return {"statusCode": 200, "body": json.dumps({"status": "disabled"})}

    s3 = boto3.client("s3")
    run_date = run_date_today()
    window = int(os.environ.get("ONCA_RECONCILE_WINDOW_DAYS", "3"))

    narratives = feature_store.load_history(digests_bucket, window, s3=s3)
    candidates = select_candidates(narratives, run_date)
    entities = {n["entity"] for n in candidates}
    beliefs = load_beliefs(digests_bucket, entities, s3=s3)

    embcache = _load_json(digests_bucket, EMBCACHE_KEY, s3)
    if not isinstance(embcache, dict):
        embcache = {}

    def embed_fn(texts: list[str]) -> dict[str, list[float]]:
        return embeddings.embed_texts(texts, cache=embcache)

    reinforcements, proposals = reconcile(
        narratives, beliefs, run_date=run_date, embed_fn=embed_fn, stance_fn=llm_stance
    )

    counts = {"reinforcements": 0, "proposals": 0, "proposals_pending": 0}
    try:
        counts = publish(reinforcements, proposals, digests_bucket, s3=s3,
                         as_of=run_date, embcache=embcache)
    except Exception as exc:  # pragma: no cover - publish best-effort
        print(f"Warning: reconcile publish failed: {exc}")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "status": "ok",
            "as_of": run_date,
            "candidates": len(candidates),
            "entities": len(entities),
            "reinforcements_found": len(reinforcements),
            "proposals_found": len(proposals),
            "run_at": run_at_now(),
            **counts,
        }),
    }
