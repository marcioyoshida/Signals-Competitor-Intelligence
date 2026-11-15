"""Lambda tasks for the OncaBoardStack state machine (#322).

One function, three actions, each a state of the run:

- ``admit``  — spend gate (#329), then create the run item (idempotent per request key);
- ``pack``   — build + seal + store the Shared Context Pack from the tenant's SCOPED feed;
- ``run``    — the phase runner (#326) under the token cap, then persist every response.

Only small ids travel between states (run id, pack sha), never positions or packs: Step
Functions caps a state's payload at 256 KB (the #173 incident).

Environment (all set by infra/board_stack.py):
``ONCA_BOARD_RUNS_TABLE``, ``ONCA_BOARD_BUCKET`` (packs), ``ONCA_SITE_BUCKET`` (feed.json),
``ONCA_TENANT_CONFIG_TABLE``, and for BYOK ``ONCA_MODEL_KEYS_TABLE`` + ``ONCA_MODEL_KEY_KMS`` +
``ONCA_MODEL_BROKER_ROLE``; ``ONCA_SYNTH_MODEL_ID`` + ``MODEL_PROFILES`` for hosted calls;
``ONCA_GUARDRAIL_ID`` / ``ONCA_GUARDRAIL_VERSION`` for client-model guardrail parity.
"""
from __future__ import annotations

import json
import os
from typing import Any

from src.board import contracts, gate, pack as packmod, runner, store

MAX_TOKENS_PER_CALL = 1800


def _table() -> Any:
    import boto3
    return boto3.resource("dynamodb").Table(os.environ["ONCA_BOARD_RUNS_TABLE"])


def _s3() -> Any:
    import boto3
    return boto3.client("s3")


def _raw_tenant_row(tenant: str) -> dict[str, Any] | None:
    import boto3
    t = boto3.resource("dynamodb").Table(os.environ["ONCA_TENANT_CONFIG_TABLE"])
    return t.get_item(Key={"tenant_id": tenant}).get("Item")


def _client_connection(tenant: str) -> Any:
    from src.synth import model_keys
    return model_keys.connection(tenant) if model_keys.configured() else None


def admit(event: dict[str, Any]) -> dict[str, Any]:
    from src.dashboard.tenant_config import get_tenant_config

    tenant, key = str(event.get("tenant") or ""), str(event.get("idempotency_key") or "")
    target, task = str(event.get("target") or ""), str(event.get("task") or "").strip()
    if not (tenant and key and target and task):
        return {"allowed": False, "reason": "bad_request"}
    d = gate.decide(tenant, config=get_tenant_config, raw_row=_raw_tenant_row,
                    client_model=_client_connection)
    run_id = store.run_id_for(tenant, key)
    item, created = store.create_run(_table(), tenant=tenant, run_id=run_id, task=task,
                                     target=target, requested_by=event.get("requested_by"))
    if not created:  # a retried request: report the existing run, never start a second one
        return {"allowed": False, "reason": "duplicate", "run_id": run_id,
                "status": item.get("status")}
    if not d.allowed:
        store.set_status(_table(), tenant=tenant, run_id=run_id, status="refused", reason=d.reason)
        return {"allowed": False, "reason": d.reason, "run_id": run_id}
    store.set_status(_table(), tenant=tenant, run_id=run_id, status="running",
                     gate=d.reason, client_billed=d.client_billed, token_cap=d.token_cap)
    return {"allowed": True, "reason": d.reason, "run_id": run_id, "tenant": tenant,
            "target": target, "token_cap": d.token_cap, "client_billed": d.client_billed}


def _scoped_feed(tenant: str) -> dict[str, Any]:
    """The tenant's own view of feed.json: the same projection /api/feed serves it."""
    from src.dashboard.feed_builder import scope_feed_for_entry_tenant, scope_feed_to_modules
    from src.dashboard.tenant_config import get_tenant_config

    cfg = get_tenant_config(tenant) or {}
    modules = list(cfg.get("modules") or [])
    if not modules:
        raise PermissionError("tenant has no entitled modules")
    raw = _s3().get_object(Bucket=os.environ["ONCA_SITE_BUCKET"], Key="feed.json")["Body"].read()
    project = scope_feed_for_entry_tenant if cfg.get("tier") == "entry" else scope_feed_to_modules
    return project(json.loads(raw), modules)


def build_pack(event: dict[str, Any]) -> dict[str, Any]:
    tenant, run_id, target = event["tenant"], event["run_id"], event["target"]
    try:
        p = packmod.seal(packmod.build_pack(_scoped_feed(tenant), target))
    except (ValueError, PermissionError) as exc:
        # an unknown or out-of-scope target: the run ends here, visibly
        store.set_status(_table(), tenant=tenant, run_id=run_id, status="failed", reason=str(exc)[:300])
        return {"ok": False, "run_id": run_id, "reason": str(exc)[:300]}
    packmod.put_pack(_s3(), os.environ["ONCA_BOARD_BUCKET"], p)
    store.set_status(_table(), tenant=tenant, run_id=run_id, status="running", pack_sha=p["sha256"],
                     n_facts=len(p["facts"]), pack_gaps=p["gaps"])
    return {"ok": True, "run_id": run_id, "tenant": tenant, "pack_sha": p["sha256"],
            "token_cap": event.get("token_cap"), "client_billed": event.get("client_billed")}


def _guardrail() -> dict[str, str] | None:
    gid, ver = os.environ.get("ONCA_GUARDRAIL_ID"), os.environ.get("ONCA_GUARDRAIL_VERSION")
    return {"id": gid, "version": ver} if gid and ver else None


def run(event: dict[str, Any]) -> dict[str, Any]:
    from src.dashboard import api_usage
    from src.synth import bedrock_llm, client_model

    tenant, run_id, sha = event["tenant"], event["run_id"], event["pack_sha"]
    p = packmod.get_pack(_s3(), os.environ["ONCA_BOARD_BUCKET"], sha)
    ctx = packmod.context_text(p)
    item = store.get_run(_table(), tenant, run_id) or {}
    target = client_model.resolve_target(tenant, loader=client_model.tenant_loader())
    meter = gate.Meter(cap=int(event.get("token_cap") or gate.DEFAULT_TOKEN_CAP))
    guard = _guardrail()  # hosted: #304 Converse guardrail; client model: #349 ApplyGuardrail parity

    def call(officer: str, phase: str, system: str, prompt: str) -> str | None:
        usage: dict[str, Any] = {}
        text = bedrock_llm.converse(prompt, system=system, max_tokens=MAX_TOKENS_PER_CALL,
                                    usage_out=usage, target=target, context=ctx,
                                    guardrail=guard, guard_text=str(item.get("task") or "") if guard else None)
        meter.add(usage)
        return text

    result = runner.run_brief(run_id=run_id, pack_sha=sha, pack_ids=packmod.fact_ids(p),
                              task=str(item.get("task") or ""), call=call, stop=meter.stop)
    store.record_result(_table(), tenant=tenant, result=result)
    usage = meter.summary()
    if target is not None:
        api_usage.record_client_usage(tenant, meter.total, provider=getattr(target, "provider", ""),
                                      model=getattr(target, "model", ""))
    else:
        from src.dashboard.tenant_config import get_tenant_config
        api_usage.record_usage(tenant, meter.total, quota=api_usage.quota_for(get_tenant_config(tenant)))
    store.set_status(_table(), tenant=tenant, run_id=run_id, status=result["status"],
                     usage=usage, gaps=result["gaps"], dissents=result["dissents"],
                     synthesis=result["synthesis"], dropped=result["dropped"],
                     elapsed_s=result["elapsed_s"])
    return {"run_id": run_id, "status": result["status"], "calls": usage["calls"],
            "n_gaps": len(result["gaps"])}


def fail(event: dict[str, Any]) -> dict[str, Any]:
    """The state machine's catch: mark the run failed unless it already finished."""
    tenant, run_id = event.get("tenant"), event.get("run_id")
    if tenant and run_id:
        try:
            store.set_status(_table(), tenant=tenant, run_id=run_id, status="failed",
                             reason=str(event.get("error") or "")[:300])
        except ValueError:
            pass  # already terminal
    return {"run_id": run_id, "status": "failed"}


ACTIONS = {"admit": admit, "pack": build_pack, "run": run, "fail": fail}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    action = (event or {}).get("action")
    if action not in ACTIONS:
        raise ValueError(f"unknown board action {action!r}")
    return ACTIONS[action](event)


__all__ = ["lambda_handler", "contracts"]
