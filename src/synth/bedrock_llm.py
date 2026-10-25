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
# SURF-9 (#89): the strategy frameworks (Porter/PESTLE/Ansoff/BCG/Four-Corners/7S) ran on Nova Pro.
# Owner, 2026-10-25: no paying customers yet, so every hosted call runs on Nova Lite; anything
# that needs a stronger model is the customer's own (BYOK), never hosted Nova Pro. A read-only
# Lite trial against Pro's published proposals preceded the switch (same recipe as Anteater #199).
# tests/test_model_defaults.py fails if a hosted default names Nova Pro again.
FRAMEWORK_MODEL = os.environ.get("ONCA_FRAMEWORK_MODEL_ID", "amazon.nova-lite-v1:0")


def converse(
    prompt: str,
    *,
    model_id: str | None = None,
    system: str | None = None,
    max_tokens: int = 800,
    usage_out: dict[str, int] | None = None,
    images: list[bytes] | None = None,
    history: list[dict[str, str]] | None = None,
) -> str | None:
    """Return assistant text or None if Bedrock is unavailable/denied.

    ``usage_out``, if given, is filled in-place with ``input_tokens``/
    ``output_tokens`` from the Converse response — the Agentic API billing
    metering path (ADR, storefront/docs/adr-agentic-api-billing.md) reads
    this to record per-call token usage without changing the return
    contract every other caller relies on (text-only).

    ``images``, if given, is a list of raw PNG bytes appended as image content
    blocks alongside ``prompt`` — ADR 027 #132's Bedrock-vision QA check
    (``qa_pipeline/checks/vision.py``) is the first caller; every existing
    text-only caller is unaffected (defaults to no images).

    ``history`` (the /exec chat): prior turns as ``[{"role": "user"|"assistant", "text": ...}]``,
    sent as real Converse messages before ``prompt``. It must start with a user turn and
    alternate; anything else is dropped rather than sent malformed."""
    model_id = model_id or DEFAULT_SYNTH_MODEL
    try:
        client = boto3.client("bedrock-runtime")
        content: list[dict[str, Any]] = [{"text": prompt}]
        for img in images or []:
            content.append({"image": {"format": "png", "source": {"bytes": img}}})
        prior: list[dict[str, Any]] = []
        for i, turn in enumerate(history or []):
            role, text = turn.get("role"), str(turn.get("text") or "").strip()
            if role != ("user" if i % 2 == 0 else "assistant") or not text:
                prior = []
                break
            prior.append({"role": role, "content": [{"text": text}]})
        if prior and prior[-1]["role"] != "assistant":
            prior = prior[:-1]
        kwargs: dict[str, Any] = {
            "modelId": model_id,
            "messages": prior + [
                {
                    "role": "user",
                    "content": content,
                }
            ],
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0.2},
        }
        if system:
            kwargs["system"] = [{"text": system}]
        resp = client.converse(**kwargs)
        if usage_out is not None:
            usage = resp.get("usage") or {}
            usage_out["input_tokens"] = int(usage.get("inputTokens") or 0)
            usage_out["output_tokens"] = int(usage.get("outputTokens") or 0)
        parts = resp.get("output", {}).get("message", {}).get("content") or []
        texts = [p.get("text") for p in parts if p.get("text")]
        return "\n".join(texts).strip() or None
    except Exception as exc:  # pragma: no cover
        print(f"Warning: Bedrock Converse failed ({model_id}): {exc}")
        return None
