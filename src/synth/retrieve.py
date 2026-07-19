"""Optional Bedrock Knowledge Base Retrieve — degrades to no-op when blocked."""
from __future__ import annotations

import os
from typing import Any

import boto3


def enrich_with_kb(
    candidate: dict[str, Any],
    *,
    kb_id: str | None = None,
    max_results: int = 5,
) -> list[dict[str, Any]]:
    """Return extra source dicts from KB retrieve, or [] on any failure."""
    kb_id = kb_id or os.environ.get("ONCA_KB_ID")
    if not kb_id:
        return []
    seed = candidate.get("seed") or {}
    query = " ".join(
        str(seed.get(k) or "")
        for k in ("subject", "doc_type", "number", "fund_name", "issuer", "name", "institution")
    ).strip()
    if not query:
        query = "Brazilian financial regulatory competitor signal"
    try:
        client = boto3.client("bedrock-agent-runtime")
        resp = client.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query[:1000]},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": max_results}
            },
        )
    except Exception as exc:  # pragma: no cover - live KB often blocked on quota
        print(f"Warning: KB retrieve skipped: {exc}")
        return []

    extras: list[dict[str, Any]] = []
    for i, result in enumerate(resp.get("retrievalResults") or []):
        content = (result.get("content") or {}).get("text") or ""
        meta = result.get("metadata") or {}
        url = meta.get("url") or meta.get("x-amz-bedrock-kb-source-uri")
        extras.append(
            {
                "id": f"kb:{i}:{meta.get('id') or result.get('location', {})}",
                "source": meta.get("source") or "KB",
                "kind": meta.get("kind") or "retrieved",
                "subject": content[:500],
                "url": url,
                "date": meta.get("date"),
            }
        )
    return extras
