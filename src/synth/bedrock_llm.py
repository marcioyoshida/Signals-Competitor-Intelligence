"""Thin Bedrock Converse wrapper — always safe to call; returns None on failure."""
from __future__ import annotations

import os
from typing import Any

import boto3

# Prefer cheap/fast defaults; override via env when account has access.
DEFAULT_ROUTER_MODEL = os.environ.get(
    "ONCA_ROUTER_MODEL_ID", "amazon.nova-micro-v1:0"
)
DEFAULT_SYNTH_MODEL = os.environ.get(
    "ONCA_SYNTH_MODEL_ID", "amazon.nova-lite-v1:0"
)


def converse(
    prompt: str,
    *,
    model_id: str | None = None,
    system: str | None = None,
    max_tokens: int = 800,
) -> str | None:
    """Return assistant text or None if Bedrock is unavailable/denied."""
    model_id = model_id or DEFAULT_SYNTH_MODEL
    try:
        client = boto3.client("bedrock-runtime")
        kwargs: dict[str, Any] = {
            "modelId": model_id,
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": prompt}],
                }
            ],
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0.2},
        }
        if system:
            kwargs["system"] = [{"text": system}]
        resp = client.converse(**kwargs)
        parts = resp.get("output", {}).get("message", {}).get("content") or []
        texts = [p.get("text") for p in parts if p.get("text")]
        return "\n".join(texts).strip() or None
    except Exception as exc:  # pragma: no cover
        print(f"Warning: Bedrock Converse failed ({model_id}): {exc}")
        return None
