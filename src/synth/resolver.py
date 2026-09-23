"""ADR 016 addendum (2026-09-22), Decision 4 step 3: the entity-resolution
MODE SEAM — internal registry lookup (vendor/SaaS stack, unchanged) vs. a live
`POST /resolve` call to the vendor (Sovereign/in-account stack) — so synth code
above this module never branches on which plane it's running in.

**Scope, stated precisely, because it does NOT cover everything `entities.py`
does.** Only KNOWN-ID resolution seams cleanly: "here is a CNPJ root / ticker /
exact name I already extracted from a structured field — which entity is it."
That maps exactly onto `entity_registry.resolve_by_cnpj`/`resolve_by_alias`/
`resolve_by_name` (each a single `get_item` by an exact key — never a scan) and
onto the ADR 005 §2 `/resolve` contract (`{name?, cnpj_root?, ispb?, ticker?} ->
one result`). This module seams THAT.

`entities.resolve_entities(item)` — free-text alias-substring matching over an
entire narrative blob — is a fundamentally DIFFERENT operation: it scans a
candidate's text against `_alias_map()`, which is the FULL curated alias corpus
for every entity, in memory, every call. `/resolve`'s response deliberately
withholds the alias set ("never the alias set... the discovery memory" — ADR 005
§2) precisely so a caller can't reconstruct the corpus one lookup at a time.
That means free-text resolution has NO safe remote equivalent under the current
contract, not even a degraded one — there's no alias data to cache and match
against locally. This is a real, unresolved product gap for Sovereign, not an
oversight this module papers over: `guard_free_text_resolution_unavailable()`
below makes the gap loud (one warning, not a crash, not a silent guess) rather
than pretending it's solved. Closing it for real needs a different mechanism
entirely (e.g. the tenant's own local NER extracting CANDIDATE name strings,
each then resolved one at a time via THIS module) — that's follow-on design
work, not implied by "flip the resolution mode."
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

REMOTE = "remote"
REGISTRY = "registry"
_VALID_MODES = (REGISTRY, REMOTE)

# ADR 016 addendum Decision 4 step 4: "the resolve contract is the
# compatibility boundary" (ADR 005 §Costs) — so THIS is the one number that
# actually needs pinning, not internal stack innards. Every remote-mode call
# declares which contract shape it speaks via the `X-Onca-Resolve-Contract-
# Version` header; a future vendor-side `/resolve` (Decision 3, not yet built)
# checks it and rejects a stale caller with a clear error instead of silently
# mismatching request/response shape. Bump this — and `infra/tenant_stack.py`'s
# `TENANT_STACK_VERSION` alongside it — only when the `{name?, cnpj_root?,
# ispb?, ticker?} -> {entity_id, display_name, canonical_id, industries[],
# confidence}` request/response shape changes; internal refactors that don't
# touch that shape never need to. See docs/tenant-stack-versioning.md for the
# full policy (what counts as breaking, support window, upgrade procedure).
RESOLVE_CONTRACT_VERSION = "1"

# Mirrors infra/tenant_stack.py's ENTITY_CACHE_TTL_DAYS — kept as an independent
# constant (src/ must not import from infra/, the two live on opposite sides of
# the account boundary this whole seam exists to enforce) but the VALUE should
# track together; a change to one is a reason to look at the other.
ENTITY_CACHE_TTL_DAYS = 30

_warned_free_text = False


def mode() -> str:
    """Which resolution mode this deployment runs in. Defaults to `registry` —
    the vendor/SaaS, unchanged-behavior path — so an unset/misconfigured env
    never silently starts making outbound calls a SaaS tenant never expected."""
    m = str(os.environ.get("ONCA_RESOLUTION_MODE") or REGISTRY).strip().lower()
    if m not in _VALID_MODES:
        print(f"Warning: ONCA_RESOLUTION_MODE={m!r} unrecognised, defaulting to {REGISTRY!r}")
        return REGISTRY
    return m


def _cache_table(table: Any | None = None) -> Any:
    if table is not None:
        return table
    import boto3

    name = os.environ.get("ONCA_ENTITY_CACHE_TABLE")
    if not name:
        raise RuntimeError(
            "ONCA_ENTITY_CACHE_TABLE is not configured — required in remote "
            "resolution mode (infra/tenant_stack.py's OncaTenantEntityCache)"
        )
    return boto3.resource("dynamodb").Table(name)


def _cache_key(*, cnpj_root: str | None, name: str | None, ispb: str | None,
               ticker: str | None) -> str:
    if cnpj_root:
        return f"RESOLVE#cnpj#{cnpj_root}"
    if ispb:
        return f"RESOLVE#ispb#{ispb}"
    if ticker:
        return f"RESOLVE#ticker#{ticker.upper()}"
    if name:
        return f"RESOLVE#name#{name.strip().upper()}"
    raise ValueError("resolve_known_id requires at least one of cnpj_root/name/ispb/ticker")


def _cache_get(key: str, *, table: Any | None = None) -> dict[str, Any] | None:
    try:
        item = _cache_table(table).get_item(Key={"pk": key}).get("Item")
    except Exception as exc:  # pragma: no cover - transient
        print(f"Warning: entity-cache read failed for {key}: {exc}")
        return None
    if not item:
        return None
    return {k: item[k] for k in ("entity_id", "display_name", "canonical_id",
                                 "industries", "confidence") if k in item}


def _cache_put(key: str, result: dict[str, Any], *, table: Any | None = None) -> None:
    # Per-encounter, TTL'd, never a bulk pull (ADR 005 §2) — one item, one lookup
    # this tenant actually performed, expiring on its own.
    item = {"pk": key, "ttl": int(time.time()) + ENTITY_CACHE_TTL_DAYS * 86400,
            **{k: v for k, v in result.items() if v is not None}}
    try:
        _cache_table(table).put_item(Item=item)
    except Exception as exc:  # pragma: no cover - transient
        # Fail open on the CACHE WRITE only: a lookup that resolved correctly must
        # not be turned into a failure because the cache couldn't be written —
        # worst case is re-resolving (an extra /resolve call) next time, which the
        # rate limiter is what's meant to police, not this write path.
        print(f"Warning: entity-cache write failed for {key}: {exc}")


def _sign_and_post(url: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """POST `payload` to the vendor's `/resolve`, SigV4-signed with THIS Lambda's
    own execution-role credentials (`OncaResolveCallerRole`, infra/tenant_stack.py)
    — no separate assume-role call, no long-lived secret; whatever role this
    Lambda already runs as is the identity the vendor's trust policy grants
    (ADR 016 addendum Decision 3: per-tenant IAM role, not a shared API key).

    Returns the parsed response dict, or None for "not found" / any failure —
    callers must treat None as "unresolved", never synthesize a fallback answer.
    """
    import boto3
    import urllib.error
    import urllib.request
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    creds = boto3.Session().get_credentials()
    if creds is None:
        print("Warning: no AWS credentials available to sign the /resolve call")
        return None
    req = AWSRequest(
        method="POST", url=url, data=body,
        headers={
            "content-type": "application/json",
            # Included inside the AWSRequest (not added after signing) so it's
            # covered by the SigV4 signature — a caller can't be downgraded to
            # an older contract version by a header stripped in transit.
            "x-onca-resolve-contract-version": RESOLVE_CONTRACT_VERSION,
        },
    )
    # service="execute-api": the vendor /resolve endpoint is assumed to be an
    # IAM-authenticated API Gateway route (matching the existing auth_api pattern
    # in infra/app.py), not a raw Lambda function URL. Confirm this against the
    # actual endpoint once Decision 3's build item lands; a Lambda function URL
    # would sign as service="lambda" instead.
    SigV4Auth(creds, "execute-api", region).add_auth(req)
    signed = req.prepare()
    http_req = urllib.request.Request(
        url, data=body, method="POST", headers=dict(signed.headers)
    )
    try:
        with urllib.request.urlopen(http_req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # a genuine "no such entity" — not an error
        print(f"Warning: /resolve call failed ({exc.code}): {exc.reason}")
        return None
    except Exception as exc:  # pragma: no cover - network/timeout
        print(f"Warning: /resolve call failed: {exc}")
        return None


def resolve_known_id(
    *, cnpj_root: str | None = None, name: str | None = None,
    ispb: str | None = None, ticker: str | None = None,
    table: Any | None = None, cache_table: Any | None = None,
) -> dict[str, Any] | None:
    """The seam. One known identifier in, one result out (or None) — same shape
    in both modes, so a caller written against this function never needs to know
    which plane it's running in.

    Registry mode (default): delegates to `entity_registry`'s single-`get_item`
    resolve functions, unchanged — this is exactly today's SaaS behavior, just
    reshaped into the `/resolve` response contract so both modes look identical
    to the caller.

    Remote mode: read-through the tenant's local encounter cache
    (`ONCA_ENTITY_CACHE_TABLE`); on a miss, call the vendor's `/resolve`
    (`ONCA_RESOLVE_API_URL`) and cache a hit. A miss is never cached — a newly
    discovered/registered entity should become resolvable on the tenant's very
    next lookup, not wait out a stale negative TTL.
    """
    if mode() == REGISTRY:
        from src.synth import entity_registry

        entity_id = None
        if cnpj_root:
            entity_id = entity_registry.resolve_by_cnpj(cnpj_root, table=table)
        elif ticker:
            entity_id = entity_registry.resolve_by_alias(f"TICKER:{ticker.upper()}", table=table)
        elif name:
            hits = entity_registry.resolve_by_name(name, table=table)
            entity_id = hits[0] if len(hits) == 1 else None
        # ispb has no registry index today — remote-only field for now (the
        # /resolve contract carries it per ADR 005 §2; nothing populates it
        # locally yet, so it's a documented gap, not a silent wrong answer).
        if not entity_id:
            return None
        ent = entity_registry.get_entity(entity_id, table=table)
        if not ent:
            return None
        return {
            "entity_id": entity_id,
            "display_name": ent.get("display_name") or entity_id,
            "canonical_id": ent.get("canonical_id") or entity_id,
            "industries": list(ent.get("industries") or []),
            "confidence": 1.0,  # an exact-key registry hit, never fuzzy
        }

    key = _cache_key(cnpj_root=cnpj_root, name=name, ispb=ispb, ticker=ticker)
    cached = _cache_get(key, table=cache_table)
    if cached is not None:
        return cached

    url = os.environ.get("ONCA_RESOLVE_API_URL")
    if not url:
        # Fail closed: remote mode with no configured endpoint must not silently
        # behave as "nothing resolves" forever without saying why.
        raise RuntimeError("ONCA_RESOLVE_API_URL is not configured in remote resolution mode")
    payload = {k: v for k, v in (("cnpj_root", cnpj_root), ("name", name),
                                 ("ispb", ispb), ("ticker", ticker)) if v}
    result = _sign_and_post(url, payload)
    if result is not None:
        _cache_put(key, result, table=cache_table)
    return result


def guard_free_text_resolution_unavailable() -> bool:
    """Call at the top of any free-text alias-scanning path (today, only
    `entities.resolve_entities`). Returns True exactly once per process — the
    first time free-text resolution is attempted in remote mode — so the caller
    can log ONE loud warning instead of one per narrative item, and returns
    False every time after (the caller already knows). Never raises: crashing
    synth over a known, documented gap is worse than an under-attributed feed
    the operator can see and act on.
    """
    global _warned_free_text
    if mode() != REMOTE or _warned_free_text:
        return False
    _warned_free_text = True
    return True
