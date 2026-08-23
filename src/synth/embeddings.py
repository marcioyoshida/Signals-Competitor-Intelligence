"""Thin Bedrock Titan embeddings wrapper — the retrieval substrate for ADR 004 step 3.

Always safe to call: returns None (or leaves a text un-embedded) when Bedrock is
unavailable/denied, so the reconcile loop degrades to "no near bullet found" rather
than failing the pipeline. At Onça's scale (a handful to a few dozen bullets per
entity, a bounded set of surfaced narratives per run) "check against the belief" is
an **in-process cosine** over these vectors — no vector DB (ADR 004, The model).

Vectors are cached by content hash (`embed_texts` takes/updates a `{sha1: vec}` dict
the caller persists as `swot/embcache.json`): narrative and bullet texts are stable,
so steady-state runs re-embed almost nothing and stay inside the cost envelope.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Any

import boto3

DEFAULT_EMBED_MODEL = os.environ.get(
    "ONCA_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0"
)


def text_hash(text: str) -> str:
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()


def embed(text: str, *, model_id: str | None = None) -> list[float] | None:
    """Return one embedding vector, or None if Bedrock is unavailable/denied."""
    text = (text or "").strip()
    if not text:
        return None
    model_id = model_id or DEFAULT_EMBED_MODEL
    try:
        client = boto3.client("bedrock-runtime")
        resp = client.invoke_model(
            modelId=model_id, body=json.dumps({"inputText": text[:8000]})
        )
        payload = json.loads(resp["body"].read())
        vec = payload.get("embedding")
        return [float(x) for x in vec] if vec else None
    except Exception as exc:  # pragma: no cover - network/permission dependent
        print(f"Warning: Titan embed failed ({model_id}): {exc}")
        return None


def embed_texts(
    texts: list[str],
    *,
    cache: dict[str, list[float]] | None = None,
    model_id: str | None = None,
) -> dict[str, list[float]]:
    """Embed each distinct text, reusing `cache` (keyed by content hash).

    Returns {text: vector} for the texts that embedded successfully; mutates
    `cache` in place with any newly computed vectors so the caller can persist it.
    """
    cache = cache if cache is not None else {}
    out: dict[str, list[float]] = {}
    for t in {(t or "").strip() for t in texts if (t or "").strip()}:
        h = text_hash(t)
        vec = cache.get(h)
        if vec is None:
            vec = embed(t, model_id=model_id)
            if vec is None:
                continue
            cache[h] = vec
        out[t] = vec
    return out


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity in [-1, 1]; 0.0 when either vector is missing/degenerate."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def top_k(
    query: list[float] | None,
    candidates: list[tuple[Any, list[float]]],
    *,
    k: int = 4,
    min_sim: float = 0.0,
) -> list[tuple[Any, float]]:
    """Return up to k (item, similarity) pairs, highest cosine first, sim >= min_sim."""
    if not query:
        return []
    scored = [(item, cosine(query, vec)) for item, vec in candidates]
    scored = [(item, s) for item, s in scored if s >= min_sim]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:k]
