"""Thin Bedrock Converse wrapper — always safe to call; returns None on failure."""
from __future__ import annotations

import json
import os
import re
from typing import Any

import boto3

# #291: Onça's tagged application inference profiles, one per model. The deployment passes
# MODEL_PROFILES = {"<model id>": "<profile ARN>"}; spend through a profile carries the
# project=onca tag (a bare model id can't carry one, so 2026-10 Bedrock spend landed untagged).
_PROFILE_ARN = re.compile(r"^arn:aws:bedrock:([a-z0-9-]+):\d{12}:application-inference-profile/[a-z0-9]+$")


def invoke_target(model_id: str) -> str:
    """What to send as ``modelId``: Onça's tagged profile for ``model_id`` when the deployment
    names one in ``MODEL_PROFILES``, else the model id itself. The model id stays the name used
    in logs, pricing and metering; only the invoke target changes. Each profile is copied from
    the in-region foundation model, so a profile ARN for another region is ignored."""
    try:
        profiles = json.loads(os.environ.get("MODEL_PROFILES") or "{}")
    except ValueError:
        return model_id
    arn = str((profiles or {}).get(model_id) or "")
    m = _PROFILE_ARN.match(arn)
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    return arn if m and m.group(1) == region else model_id

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


def guardrail_config() -> dict[str, str] | None:
    """#304: the Ask guardrail from the environment, or None when it's off or unconfigured."""
    if os.environ.get("ONCA_ASK_GUARDRAIL", "false").lower() not in ("1", "true", "yes"):
        return None
    gid, ver = os.environ.get("ONCA_GUARDRAIL_ID"), os.environ.get("ONCA_GUARDRAIL_VERSION")
    return {"id": gid, "version": ver} if gid and ver else None


def guarded_content(prompt: str, guard_text: str | None) -> list[dict[str, Any]]:
    """The prompt as Converse content blocks, with ``guard_text`` (the user's question) as the only
    guardContent. When the question occurs in the prompt, THAT occurrence becomes the guarded
    block, so the model reads exactly the same text in the same order. Appending a second copy
    changed answers (live 2026-11-02: a BTG question went from grounded to declined). Otherwise
    it is appended."""
    if not guard_text:
        return [{"text": prompt}]
    i = prompt.find(guard_text)
    if i < 0:
        return [{"text": prompt}, {"guardContent": {"text": {"text": guard_text}}}]
    head, tail = prompt[:i], prompt[i + len(guard_text):]
    return ([{"text": head}] if head else []) + [{"guardContent": {"text": {"text": guard_text}}}] + (
        [{"text": tail}] if tail else [])


def guardrail_blocked(trace: Any) -> bool:
    """Does a Converse guardrail trace hold any BLOCKED assessment? (ANONYMIZED is not a block.)
    Without a trace an intervention is treated as a block (fail closed)."""
    if not trace:
        return True

    def walk(o: Any) -> bool:
        if isinstance(o, dict):
            return o.get("action") == "BLOCKED" or any(walk(v) for v in o.values())
        if isinstance(o, list):
            return any(walk(v) for v in o)
        return False
    return walk(trace)


def converse(
    prompt: str,
    *,
    model_id: str | None = None,
    system: str | None = None,
    max_tokens: int = 800,
    usage_out: dict[str, int] | None = None,
    images: list[bytes] | None = None,
    history: list[dict[str, str]] | None = None,
    guardrail: dict[str, str] | None = None,
    guard_text: str | None = None,
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
    alternate; anything else is dropped rather than sent malformed.

    ``guardrail`` (#304) = ``{"id", "version"}`` attaches a Bedrock Guardrail. ``guard_text`` (the
    user's own question) goes in as the only ``guardContent`` block, so input policies evaluate
    the question and never the retrieved context. An intervention returns None and sets
    ``usage_out["guardrail"] = "intervened"``, so the caller can tell it from a Bedrock failure."""
    model_id = model_id or DEFAULT_SYNTH_MODEL
    try:
        client = boto3.client("bedrock-runtime")
        content: list[dict[str, Any]] = guarded_content(prompt, guard_text if guardrail else None)
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
            "modelId": invoke_target(model_id),
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
        if guardrail:
            kwargs["guardrailConfig"] = {"guardrailIdentifier": guardrail["id"],
                                         "guardrailVersion": guardrail["version"], "trace": "enabled"}
        resp = client.converse(**kwargs)
        if usage_out is not None:
            usage = resp.get("usage") or {}
            usage_out["input_tokens"] = int(usage.get("inputTokens") or 0)
            usage_out["output_tokens"] = int(usage.get("outputTokens") or 0)
        # An ANONYMIZE (PII masked in the answer) also reports "guardrail_intervened": only a
        # BLOCKED assessment is a refusal; a masked answer is returned as masked (live 2026-11-02).
        if resp.get("stopReason") == "guardrail_intervened" and guardrail_blocked(resp.get("trace")):
            if usage_out is not None:
                usage_out["guardrail"] = "intervened"
            return None
        parts = resp.get("output", {}).get("message", {}).get("content") or []
        texts = [p.get("text") for p in parts if p.get("text")]
        return "\n".join(texts).strip() or None
    except Exception as exc:  # pragma: no cover
        print(f"Warning: Bedrock Converse failed ({model_id}): {exc}")
        return None
