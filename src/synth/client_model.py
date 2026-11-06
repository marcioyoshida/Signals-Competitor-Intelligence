"""#346 (BYOK story 1, ADR 031, #345): a tenant's OWN model behind ``bedrock_llm.converse``.

Owner direction (2026-10-25, see ``bedrock_llm.FRAMEWORK_MODEL``): hosted calls run on Nova Lite;
anything stronger is the customer's own model and the customer's own bill, never hosted Nova Pro.
A Claude Max/Pro chat subscription does not fund API calls, so Onça never hosts Claude itself.

A **target** says which model answers one call:

- ``None`` (or provider ``hosted``): Onça's Bedrock, exactly as before;
- provider ``anthropic_key``: the tenant's Anthropic API key, through the official ``anthropic``
  SDK. The SDK is imported lazily here, so the 20-odd Lambdas sharing the asset never load it;
- provider ``bedrock_role`` (#350): Claude in Amazon Bedrock in the CLIENT's AWS account, through
  the SDK's ``AnthropicBedrockMantle`` client (the Messages API on Bedrock). Onça assumes the
  client's IAM role with a per-tenant external id, by way of one fixed-name broker role in Onça's
  account, so the client's trust policy names a principal that never changes.

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
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

HOSTED = "hosted"
ANTHROPIC_KEY = "anthropic_key"
BEDROCK_ROLE = "bedrock_role"

#: A model may be connected only after it is measured on Onça's Ask regression (#352). Sonnet 5.5
#: is CORAX's measured model (faithfulness 33/41, 0 inversions); Onça's own run is #352.
MODELS: dict[str, dict[str, Any]] = {
    "claude-sonnet-5-5": {"provider": ANTHROPIC_KEY, "label": "Claude Sonnet 5.5",
                          "input_per_1m": 2.00, "output_per_1m": 10.00},
    # #350: the same model in the client's own Bedrock (Bedrock prices it; the bill is theirs).
    # The client enables model access for it in their Bedrock console (it is not "open" access).
    "anthropic.claude-sonnet-5-5": {"provider": BEDROCK_ROLE,
                                    "label": "Claude Sonnet 5.5 (Amazon Bedrock da sua conta)"},
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
ROLE_DENIED = "model_role_denied"             # #350: AssumeRole refused (trust or external id)

#: What the tenant reads for each code (pt-BR, like the rest of /exec).
MESSAGES: dict[str, str] = {
    KEY_REJECTED: "A chave do seu modelo foi recusada pelo provedor (inválida, revogada ou "
                  "apagada). Atualize a chave na sua conta.",
    NO_CREDIT: "A conta do seu modelo está sem crédito de API. Uma assinatura de chat (Claude "
               "Pro/Max) não paga chamadas de API.",
    NO_ACCESS: "A credencial conectada não tem acesso a este modelo. No Amazon Bedrock, confira o "
               "acesso ao modelo na sua conta e a permissão bedrock-mantle:CreateInference do papel.",
    NEEDS_WORKSPACE: "A chave não está vinculada a um workspace do provedor. Gere uma chave de "
                     "API dentro de um workspace.",
    UNAVAILABLE: "O provedor do seu modelo não respondeu agora. Tente de novo em instantes.",
    REFUSED: "O seu modelo recusou esta pergunta.",
    ROLE_DENIED: "A Onça não conseguiu assumir o papel (IAM role) na sua conta AWS. Confira a "
                 "relação de confiança do papel e o external id mostrado na sua conta.",
}

#: #350: the regions where Claude in Amazon Bedrock is served (platform.claude.com, "Claude in
#: Amazon Bedrock" -> Regions, read 2026-11-06). sa-east-1 is GLOBAL routing only: a client
#: needing in-country processing can't get it there today.
BEDROCK_REGIONS = (
    "us-east-1", "us-east-2", "us-west-1", "us-west-2", "ca-central-1", "ca-west-1", "sa-east-1",
    "eu-central-1", "eu-central-2", "eu-north-1", "eu-south-1", "eu-south-2", "eu-west-1",
    "eu-west-2", "eu-west-3", "il-central-1", "me-central-1", "af-south-1", "ap-northeast-1",
    "ap-northeast-2", "ap-northeast-3", "ap-south-1", "ap-south-2", "ap-southeast-1",
    "ap-southeast-2", "ap-southeast-3", "ap-southeast-4")
#: The one principal a client's role trusts (infra/app.py ``OncaClientModelBroker``, fixed name,
#: RETAIN: a re-created role gets a new unique id and every client's trust would silently break).
BROKER_ROLE_ENV = "ONCA_MODEL_BROKER_ROLE"
ROLE_ARN_RE = r"arn:aws:iam::\d{12}:role/[\w+=,.@/-]{1,512}"
#: Assumed credentials are reused within a warm container until shortly before they expire; the
#: tenant's row is still read on EVERY call, so a disconnect stops the next call at once.
ROLE_SESSION_S = 900
_CRED_CACHE: dict[tuple[str, ...], tuple[float, dict[str, str]]] = {}


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
    # #350 bedrock_role: not secret (the external id is unique per tenant, not a password), but
    # tenant-scoped: they come from the tenant's own row, never from the request.
    role_arn: str = ""
    external_id: str = ""
    region: str = ""

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


def _session_name(target: Target) -> str:
    """What the client sees in its CloudTrail for Onça's calls: ``onca-<tenant>``."""
    return re.sub(r"[^\w+=,.@-]", "-", f"onca-{target.tenant or 'tenant'}")[:64]


def _assumed_credentials(target: Target, *, sts_factory: Callable[[dict[str, str] | None], Any]
                         | None = None, now: Callable[[], float] = time.time) -> dict[str, str]:
    """#350: credentials in the client's account. Two hops: Onça's Lambda role assumes the broker
    role, the broker assumes the client's role with the tenant's external id. A failure on the
    first hop is Onça's own (``model_unavailable``); an AccessDenied on the second is the client's
    trust policy or external id (``model_role_denied``)."""
    key = (target.tenant, target.role_arn, target.external_id)
    hit = _CRED_CACHE.get(key)
    if hit and hit[0] - 120 > now():
        return hit[1]
    broker = os.environ.get(BROKER_ROLE_ENV, "")
    if not broker:
        raise ModelError(UNAVAILABLE, "client Bedrock is not enabled here")
    if sts_factory is None:
        import boto3

        def sts_factory(creds: dict[str, str] | None) -> Any:
            if creds is None:
                return boto3.client("sts")
            return boto3.client("sts", aws_access_key_id=creds["AccessKeyId"],
                                aws_secret_access_key=creds["SecretAccessKey"],
                                aws_session_token=creds["SessionToken"])
    try:
        hop1 = sts_factory(None).assume_role(RoleArn=broker, RoleSessionName=_session_name(target),
                                             DurationSeconds=ROLE_SESSION_S)["Credentials"]
    except Exception as exc:  # noqa: BLE001 - Onça's own broker: never the client's fault
        print(f"Warning: model broker AssumeRole failed: {type(exc).__name__}")
        raise ModelError(UNAVAILABLE, "the model broker could not be assumed") from None
    try:
        creds = sts_factory(hop1).assume_role(
            RoleArn=target.role_arn, RoleSessionName=_session_name(target),
            ExternalId=target.external_id, DurationSeconds=ROLE_SESSION_S)["Credentials"]
    except Exception as exc:  # noqa: BLE001
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        if code in ("AccessDenied", "AccessDeniedException", "InvalidClientTokenId") or \
                "AccessDenied" in type(exc).__name__:
            raise ModelError(ROLE_DENIED, "the client role refused AssumeRole") from None
        raise ModelError(UNAVAILABLE, f"AssumeRole failed: {code or type(exc).__name__}") from None
    out = {"AccessKeyId": creds["AccessKeyId"], "SecretAccessKey": creds["SecretAccessKey"],
           "SessionToken": creds["SessionToken"]}
    exp = creds.get("Expiration")
    _CRED_CACHE[key] = (exp.timestamp() if hasattr(exp, "timestamp") else now() + ROLE_SESSION_S, out)
    return out


def _client_for(target: Target, *, sts_factory: Any = None) -> Any:
    """The SDK client for a target, or a :class:`ModelError` (never a hosted client)."""
    try:
        import anthropic
    except ImportError as exc:      # the asset was staged without the SDK
        raise ModelError(UNAVAILABLE, "client model support is not installed") from exc
    if target.provider == ANTHROPIC_KEY:
        return anthropic.Anthropic(api_key=target.api_key, timeout=TIMEOUT_S, max_retries=0)
    if target.provider == BEDROCK_ROLE:
        if not (re.fullmatch(ROLE_ARN_RE, target.role_arn or "") and target.external_id
                and target.region in BEDROCK_REGIONS):
            raise ModelError(NO_ACCESS, "the Bedrock connection is incomplete")
        c = _assumed_credentials(target, sts_factory=sts_factory)
        return anthropic.AnthropicBedrockMantle(
            aws_access_key=c["AccessKeyId"], aws_secret_key=c["SecretAccessKey"],
            aws_session_token=c["SessionToken"], aws_region=target.region,
            timeout=TIMEOUT_S, max_retries=0)
    raise ModelError(NO_ACCESS, "unknown model provider")


def _create(client: Any, target: Target, **kwargs: Any) -> Any:
    """One Messages call. The Anthropic API gets the server-side refusal fallback (re-run on
    another Anthropic model, on the CLIENT's key); Bedrock has no server-side fallback, so a
    decline there is a plain ``model_refused``."""
    kwargs.update(model=target.model, output_config={"effort": EFFORT})
    if target.provider == ANTHROPIC_KEY:
        return client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
    return client.messages.create(**kwargs)


def anthropic_converse(target: Target, prompt: str, *, system: str | None = None,
                       max_tokens: int = 800, history: list[dict[str, str]] | None = None,
                       images: list[bytes] | None = None, client: Any = None
                       ) -> tuple[str, dict[str, Any]]:
    """One call on the tenant's own model (its Anthropic key or its own Bedrock). Returns
    (text, usage); raises :class:`ModelError`."""
    if target.model not in MODELS or MODELS[target.model]["provider"] != target.provider:
        raise ModelError(NO_ACCESS, "model not on the allow-list")
    if client is None:
        client = _client_for(target)
    kwargs: dict[str, Any] = {"max_tokens": max(int(max_tokens), MIN_MAX_TOKENS),
                              "messages": _messages(prompt, history, images)}
    if system:
        kwargs["system"] = system
    try:
        resp = _create(client, target, **kwargs)
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
    """The reader of a tenant's sealed key (#347), or None where the store isn't configured (local
    runs, the pipeline Lambdas): there, every call is hosted. A store failure becomes a visible
    ``model_unavailable``, never None, because None would mean answering on hosted Nova."""
    from src.synth import model_keys
    if not model_keys.configured():
        return None

    def load(tenant: str) -> Target | None:
        try:
            return model_keys.load_target(tenant)
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: model-key store failed: {type(exc).__name__}")
            raise ModelError(UNAVAILABLE, "the key store could not be read") from None
    return load


def record_failure(target: Target | None, code: str) -> None:
    """Stamp a key-level failure on the tenant's row so the account sheet can show it (#348).
    A transient ``model_unavailable`` and a per-question refusal say nothing about the key."""
    if target is None or not target.tenant or code in (UNAVAILABLE, REFUSED):
        return
    from src.synth import model_keys
    if model_keys.configured():
        model_keys.record_check(target.tenant, error=code)


def verify(target: Target, *, client: Any = None, sts_factory: Any = None) -> None:
    """One minimal real call: it proves the key (or the role), the model and the account's
    credit (a chat subscription surfaces here as ``model_no_credit``). Raises
    :class:`ModelError`."""
    if target.model not in MODELS or MODELS[target.model]["provider"] != target.provider:
        raise ModelError(NO_ACCESS, "model not on the allow-list")
    if client is None:
        client = _client_for(target, sts_factory=sts_factory)
    try:
        client.messages.create(model=target.model, max_tokens=16,
                               messages=[{"role": "user", "content": "Reply with OK."}],
                               output_config={"effort": EFFORT})
    except Exception as exc:  # noqa: BLE001 - classified, never swallowed
        raise _classify(exc) from None


def error_result(code: str) -> dict[str, Any]:
    """The Ask/agent-API answer for a failed client model: the code and what to do, never a
    hosted answer in its place."""
    return {"answer": MESSAGES.get(code, MESSAGES[UNAVAILABLE]), "refused": False,
            "grounded": False, "reason": "client-model-error", "model_error": code,
            "citations": []}
