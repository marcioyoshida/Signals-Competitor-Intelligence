"""#346 (BYOK story 1, ADR 031, #345): a tenant's OWN model behind ``bedrock_llm.converse``.

Owner direction (2026-10-25, see ``bedrock_llm.FRAMEWORK_MODEL``): hosted calls run on Nova Lite;
anything stronger is the customer's own model and the customer's own bill, never hosted Nova Pro.
A Claude Max/Pro chat subscription does not fund API calls, so Onça never hosts Claude itself.

A **target** says which model answers one call:

- ``None`` (or provider ``hosted``): Onça's Bedrock, exactly as before;
- provider ``anthropic_key``: the tenant's Anthropic API key, through the official ``anthropic``
  SDK. The SDK is imported lazily here, so the 20-odd Lambdas sharing the asset never load it.

Only tenant-request paths (Ask, the agent API, later the board brief) resolve a target. Pipeline
and synthesis calls never do; they stay hosted because their output is shared by every tenant.

**No fallback to hosted.** A failing client model raises :class:`ModelError` with a code the
reader can act on (``converse`` turns it into ``usage_out["model_error"]``); Onça never answers
on Nova in its place: the client chose its model, and a silent swap would misreport both quality
and who paid.

Ported from CORAX ``src/corax/byom.py`` (ADR 019, live since 2026-10-13); the codes match it.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Callable

HOSTED = "hosted"
ANTHROPIC_KEY = "anthropic_key"

#: A model may be connected only after it is measured on Onça's Ask regression (#352). Sonnet 5.5
#: is CORAX's measured model (faithfulness 33/41, 0 inversions); Onça's own run is #352.
MODELS: dict[str, dict[str, Any]] = {
    "claude-sonnet-5-5": {"provider": ANTHROPIC_KEY, "label": "Claude Sonnet 5.5",
                          "input_per_1m": 2.00, "output_per_1m": 10.00},
}
DEFAULT_MODEL = "claude-sonnet-5-5"
#: One Ask is ONE model call inside API Gateway's 29 s; KB retrieval runs before it. Thinking is
#: always on for these models, so depth stays at ``low`` and the SDK does not retry (a retry would
#: double the wall clock past the gateway).
EFFORT = "low"
TIMEOUT_S = 22.0
#: Thinking tokens count against ``max_tokens``: a Nova-sized cap (700-800) could be spent before
#: the answer starts. Billing follows actual use, so the floor costs nothing when unused.
MIN_MAX_TOKENS = 8_000
#: Anthropic's server-side fallback on a safety decline (claude-api reference): the request is
#: re-run on another Anthropic model, on the CLIENT's key. Not a fallback to hosted.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

KEY_REJECTED = "model_key_rejected"           # 401: wrong, revoked or deleted key
NO_CREDIT = "model_no_credit"                 # 402: no API credit (a chat plan funds none)
NO_ACCESS = "model_no_access"                 # 403/404: this key cannot use this model
NEEDS_WORKSPACE = "model_key_needs_workspace"  # 400: user-level key outside a workspace
UNAVAILABLE = "model_unavailable"             # 429/5xx/529/network: try again
REFUSED = "model_refused"                     # stop_reason refusal, after any fallback

#: What the tenant reads for each code (pt-BR, like the rest of /exec).
MESSAGES: dict[str, str] = {
    KEY_REJECTED: "A chave do seu modelo foi recusada pelo provedor (inválida, revogada ou "
                  "apagada). Atualize a chave na sua conta.",
    NO_CREDIT: "A conta do seu modelo está sem crédito de API. Uma assinatura de chat (Claude "
               "Pro/Max) não paga chamadas de API.",
    NO_ACCESS: "A chave conectada não tem acesso a este modelo.",
    NEEDS_WORKSPACE: "A chave não está vinculada a um workspace do provedor. Gere uma chave de "
                     "API dentro de um workspace.",
    UNAVAILABLE: "O provedor do seu modelo não respondeu agora. Tente de novo em instantes.",
    REFUSED: "O seu modelo recusou esta pergunta.",
}


class ModelError(RuntimeError):
    """A client-model failure with a reader-facing ``code``. Never carries the key."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}"[:300])
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class Target:
    """Which model answers a call. ``api_key`` is excluded from repr/eq so it can't reach a log
    line through an f-string or a failed assertion."""

    provider: str = HOSTED
    model: str = ""
    tenant: str = ""
    api_key: str = field(default="", repr=False, compare=False)

    @property
    def hosted(self) -> bool:
        return self.provider == HOSTED

    @property
    def client_billed(self) -> bool:
        return not self.hosted


def resolve_target(tenant: str | None, *, loader: Callable[[str], Target | None] | None = None
                   ) -> Target | None:
    """The tenant's own model, or None (hosted). ``loader`` reads the tenant's sealed key; the
    store lands with #347, so until then no tenant has one and every call stays hosted. A loader
    failure is NOT treated as "no key": it raises, because answering on hosted Nova for a tenant
    that connected its own model is exactly the silent fallback this module forbids."""
    if not tenant or loader is None:
        return None
    target = loader(tenant)
    if target is None or target.hosted:
        return None
    if target.model not in MODELS or MODELS[target.model]["provider"] != target.provider:
        raise ModelError(NO_ACCESS, "model not on the allow-list")
    return target


def _classify(exc: Exception) -> ModelError:
    """The SDK's typed errors, most specific first. Detail strings are ours, never the SDK's
    message, which could echo request content."""
    import anthropic
    if isinstance(exc, anthropic.AuthenticationError):
        return ModelError(KEY_REJECTED, "the provider rejected the key")
    if isinstance(exc, (anthropic.PermissionDeniedError, anthropic.NotFoundError)):
        return ModelError(NO_ACCESS, "this key cannot use this model")
    if isinstance(exc, anthropic.RateLimitError):
        return ModelError(UNAVAILABLE, "rate limited")
    said = f"{exc} {getattr(exc, 'body', '')}"
    if isinstance(exc, anthropic.BadRequestError) and (
            "not scoped to a workspace" in said or "anthropic-workspace-id" in said):
        return ModelError(NEEDS_WORKSPACE, "the key is not scoped to a workspace")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 402 or getattr(exc, "type", "") == "billing_error":
            return ModelError(NO_CREDIT, "the account has no API credit")
        if exc.status_code >= 500:
            return ModelError(UNAVAILABLE, f"provider error {exc.status_code}")
        return ModelError(UNAVAILABLE, f"provider refused the request {exc.status_code}")
    if isinstance(exc, anthropic.APIConnectionError):      # includes timeouts
        return ModelError(UNAVAILABLE, "could not reach the provider")
    return ModelError(UNAVAILABLE, type(exc).__name__)


def _messages(prompt: str, history: list[dict[str, str]] | None,
              images: list[bytes] | None) -> list[dict[str, Any]]:
    """Same history rule as the hosted path: user/assistant alternating from a user turn, else
    dropped; the last prior turn must be the assistant's."""
    prior: list[dict[str, Any]] = []
    for i, turn in enumerate(history or []):
        role, text = turn.get("role"), str(turn.get("text") or "").strip()
        if role != ("user" if i % 2 == 0 else "assistant") or not text:
            prior = []
            break
        prior.append({"role": role, "content": text})
    if prior and prior[-1]["role"] != "assistant":
        prior = prior[:-1]
    content: list[dict[str, Any]] = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.standard_b64encode(img).decode()}}
        for img in images or []]
    content.append({"type": "text", "text": prompt})
    return prior + [{"role": "user", "content": content}]


def anthropic_converse(target: Target, prompt: str, *, system: str | None = None,
                       max_tokens: int = 800, history: list[dict[str, str]] | None = None,
                       images: list[bytes] | None = None, client: Any = None
                       ) -> tuple[str, dict[str, Any]]:
    """One call on the tenant's key. Returns (text, usage); raises :class:`ModelError`."""
    if target.model not in MODELS:
        raise ModelError(NO_ACCESS, "model not on the allow-list")
    if client is None:
        try:
            import anthropic
        except ImportError as exc:      # the asset was staged without the SDK
            raise ModelError(UNAVAILABLE, "client model support is not installed") from exc
        client = anthropic.Anthropic(api_key=target.api_key, timeout=TIMEOUT_S, max_retries=0)
    kwargs: dict[str, Any] = {
        "model": target.model, "max_tokens": max(int(max_tokens), MIN_MAX_TOKENS),
        "messages": _messages(prompt, history, images),
        "output_config": {"effort": EFFORT},
        "betas": [FALLBACK_BETA], "fallbacks": "default"}
    if system:
        kwargs["system"] = system
    try:
        resp = client.beta.messages.create(**kwargs)
    except Exception as exc:  # noqa: BLE001 - classified, never swallowed
        raise _classify(exc) from None
    if resp.stop_reason == "refusal":
        raise ModelError(REFUSED, "the model declined this request")
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    u = resp.usage
    cache_read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    return text, {"input_tokens": int(u.input_tokens) + cache_read + cache_write,
                  "output_tokens": int(u.output_tokens),
                  "cache_read": cache_read, "cache_write": cache_write,
                  "model": getattr(resp, "model", target.model) or target.model,
                  "stop_reason": resp.stop_reason}


# --- guardrail parity (#349) ------------------------------------------------------------------
# The hosted path attaches the Bedrock guardrail to Converse. A client model is outside Bedrock,
# so the same guardrail runs standalone (ApplyGuardrail) on the question before the call and on
# the answer after it. A BLOCKED assessment refuses; ANONYMIZED masks the text and continues.

def _blocked(assessments: Any) -> bool:
    def walk(o: Any) -> bool:
        if isinstance(o, dict):
            return o.get("action") == "BLOCKED" or any(walk(v) for v in o.values())
        if isinstance(o, list):
            return any(walk(v) for v in o)
        return False
    return walk(assessments)


def apply_guardrail(guardrail: dict[str, str], text: str, source: str, *, client: Any = None
                    ) -> tuple[bool, str]:
    """(blocked, text_to_use) for ``source`` "INPUT" or "OUTPUT". Fails closed: an error is a
    block, as the hosted path treats an intervention without a trace."""
    import boto3
    try:
        rt = client or boto3.client("bedrock-runtime")
        resp = rt.apply_guardrail(guardrailIdentifier=guardrail["id"],
                                  guardrailVersion=guardrail["version"], source=source,
                                  content=[{"text": {"text": text}}])
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: ApplyGuardrail failed ({source}): {type(exc).__name__}")
        return True, ""
    if resp.get("action") != "GUARDRAIL_INTERVENED":
        return False, text
    if _blocked(resp.get("assessments")):
        return True, ""
    masked = "".join(o.get("text", "") for o in resp.get("outputs") or [])
    return False, masked or text


def tenant_loader() -> Callable[[str], Target | None] | None:
    """The reader of a tenant's sealed key. None until the key store lands (#347): no tenant can
    have connected a model yet, so every call resolves to hosted."""
    return None


def error_result(code: str) -> dict[str, Any]:
    """The Ask/agent-API answer for a failed client model: the code and what to do, never a
    hosted answer in its place."""
    return {"answer": MESSAGES.get(code, MESSAGES[UNAVAILABLE]), "refused": False,
            "grounded": False, "reason": "client-model-error", "model_error": code,
            "citations": []}
