"""OncaAgentApi — the machine-callable Agentic API (`POST /api/v1/agent/ask`).

ADR: storefront/docs/adr-agentic-api-billing.md (canonical), this repo's
docs/2026-09-13-adr025-agentic-api-billing.md (pilot notes).

A DISTINCT auth mode from the human Cognito-JWT `/api/ask` (agent_ask.py):
a paying tenant's own bot/agent authenticates with a long-lived API key
(`Authorization: Bearer sk_onca_...`), issued and managed through the
tenant's own admin panel (api_keys_api.py). Tenant is resolved ONLY from the
key's own DynamoDB row — never from the request body — the same fail-closed
discipline `auth.identity_from_event` already enforces for the JWT path.

Reuses the SAME grounded-RAG core (`agent_ask.answer`) the human path calls,
so the grounding/citation contract is identical; the only difference is the
auth mode and that every call here is metered (Bedrock token usage recorded
per tenant per billing cycle) for the Agentic API's overage billing.
"""
from __future__ import annotations

import os
from typing import Any

from src.dashboard.agent_ask import _body, _kb_retrieve, _load_feed, _resp, answer


def _bearer_key(event: dict[str, Any]) -> str:
    headers = {str(k).lower(): v for k, v in ((event or {}).get("headers") or {}).items()}
    auth = str(headers.get("authorization") or "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    from src.dashboard import api_keys, api_usage
    from src.dashboard.tenant_config import get_tenant_config
    from src.synth.bedrock_llm import converse, guardrail_config

    raw_key = _bearer_key(event)
    if not raw_key:
        return _resp(401, {"error": "missing Authorization: Bearer <api key>"})
    row = api_keys.lookup_key(raw_key)
    if row is None:
        return _resp(401, {"error": "invalid or revoked API key"})
    if "ask" not in (row.get("scopes") or []):
        return _resp(403, {"error": "this key is not scoped for 'ask'"})

    tenant_id = row["tenant_id"]
    cfg = get_tenant_config(tenant_id)
    refusal = api_keys.api_access_refusal(cfg)
    if refusal:
        return _resp(403, {"error": refusal})
    modules = list(cfg["modules"])

    body = _body(event)
    if body is None:
        return _resp(400, {"error": "invalid JSON body"})
    q = str(body.get("q") or "").strip()
    if not q:
        return _resp(400, {"error": "q (question) required"})
    if len(q) > 500:
        q = q[:500]
    scope = body.get("scope") if isinstance(body.get("scope"), dict) else None

    bucket = os.environ.get("ONCA_SITE_BUCKET")
    if not bucket:
        return _resp(500, {"error": "not configured"})

    usage: dict[str, Any] = {}
    guard = guardrail_config()   # #304
    # #346 BYOK: a tenant that connected its own model is answered on it, never on hosted Nova.
    from src.synth import client_model
    try:
        target = client_model.resolve_target(tenant_id, loader=client_model.tenant_loader())
    except client_model.ModelError as exc:
        return _resp(200, client_model.error_result(exc.code))

    def _metered_converse(prompt: str, *, system: str | None = None, max_tokens: int = 800) -> str | None:
        extra: dict[str, Any] = {"guardrail": guard, "guard_text": q} if guard else {}
        if target is not None:          # hosted calls keep their exact pre-BYOK signature
            extra["target"] = target
        return converse(prompt, system=system, max_tokens=max_tokens, usage_out=usage, **extra)

    kb_fn = _kb_retrieve if os.environ.get("ONCA_KB_ID") else None
    try:
        feed = _load_feed(bucket)
        result = answer(
            q, feed=feed, scope=scope, converser=_metered_converse,
            kb_retrieve=kb_fn, modules=modules, persona=None,
        )
        if usage.get("guardrail") == "intervened":
            from src.dashboard.agent_ask import guardrail_refusal
            result = guardrail_refusal()
        elif usage.get("model_error"):
            result = client_model.error_result(str(usage["model_error"]))
            client_model.record_failure(target, str(usage["model_error"]))
    except Exception as exc:  # pragma: no cover - defensive; never leak a stack
        print(f"agent_api error: {exc}")
        return _resp(500, {"error": "internal error"})

    total_tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
    # #346/#353: tokens on the tenant's OWN model are on the tenant's bill, so they don't draw the
    # included quota (that would charge overage for tokens Onça never paid). Still reported below.
    client_billed = bool(usage.get("client_billed"))
    # Both best-effort: a metering hiccup must never turn a good answer into a 500.
    # #305: past the included quota the answer is still served; the excess is marked overage.
    quota = api_usage.quota_for(cfg)
    metered = api_usage.record_usage(tenant_id, 0 if client_billed else total_tokens, quota=quota)
    if metered is None:                  # nothing metered (no model call) or the write failed
        cur = api_usage.get_usage(tenant_id)
        metered = {"period": cur["period"], "tokens_used": cur["tokens_used"], "quota": quota,
                   "remaining": max(0, quota - cur["tokens_used"]), "overage_tokens": 0}
    api_keys.touch_last_used(row["key_hash"])

    result["usage"] = {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "total_tokens": total_tokens,
        "overage_tokens": metered.get("overage_tokens", 0),
    }
    if client_billed:                   # hosted responses keep the published shape
        result["usage"].update(provider=usage.get("provider"), client_billed=True)
    result["quota"] = {"period": metered["period"], "included_tokens": quota,
                       "used_tokens": metered["tokens_used"], "remaining_tokens": metered.get("remaining", 0)}
    out = _resp(200, result)
    out["headers"].update(api_usage.quota_headers(quota, metered["tokens_used"], metered["period"]))
    return out
