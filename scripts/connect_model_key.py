"""Operator tool (BYOK #347/#352): connect a model API key to a tenant without a browser.

For a tenant no human model admin signs in to (the QA tenant the model eval runs on), an operator
connects the key here. The key is typed at a hidden prompt: it is never an argument (shell
history), never printed, never written to disk. It goes once to the act Lambda's
``connect_model`` intent, which verifies it with one minimal real call, seals it under the
model-key KMS key and journals only its last 4 characters.

  python -m scripts.connect_model_key --tenant qa-internal-admin
  python -m scripts.connect_model_key --tenant qa-internal-admin --status
  python -m scripts.connect_model_key --tenant qa-internal-admin --disconnect

Needs AWS credentials for the Onça account (``AWS_PROFILE=my2027``).
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any


def _act(lam: Any, fn: str, tenant: str, intent: str, args: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    claims = {"sub": "operator-cli", "custom:tenant": tenant, "cognito:groups": "operator"}
    ev = {"body": json.dumps({"intent": intent, "args": args}), "headers": {},
          "rawPath": "/api/me/act", "requestContext": {"http": {"method": "POST", "path": "/api/me/act"},
                                                        "authorizer": {"jwt": {"claims": claims}}}}
    r = json.loads(lam.invoke(FunctionName=fn, Payload=json.dumps(ev).encode())["Payload"].read())
    return int(r.get("statusCode") or 0), json.loads(r.get("body") or "{}")


def _show(conn: dict[str, Any] | None) -> str:
    if not conn:
        return "no own model connected (answers use Onça's hosted model)"
    cred = f"role {conn['role_arn']} ({conn['region']})" if conn.get("role_arn") else f"key ••••{conn.get('last4')}"
    err = f" | last error: {conn['last_error']}" if conn.get("last_error") else ""
    return f"{conn.get('label') or conn.get('model')} | {cred} | verified {conn.get('verified_at') or '-'}{err}"


def main(argv: list[str] | None = None) -> int:
    import boto3

    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--model", default="claude-sonnet-5-5")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true")
    g.add_argument("--disconnect", action="store_true")
    a = ap.parse_args(argv)

    lam = boto3.client("lambda", region_name="us-east-1")
    fns = [f["FunctionName"] for p in lam.get_paginator("list_functions").paginate()
           for f in p["Functions"] if "OncaActApi" in f["FunctionName"]]
    if len(fns) != 1:
        print(f"could not find the act Lambda ({len(fns)} matches)")
        return 2
    fn = fns[0]

    status, body = _act(lam, fn, a.tenant, "model_status", {})
    if status != 200:
        print(f"model_status failed ({status}): {body.get('error') or body}")
        return 2
    print(f"{a.tenant}: {_show(body.get('connection'))}")
    if a.status:
        return 0
    if a.disconnect:
        status, body = _act(lam, fn, a.tenant, "disconnect_model", {})
        print("disconnected" if status == 200 else f"failed ({status}): {body.get('error')}")
        return 0 if status == 200 else 1

    ack = body.get("acknowledgement") or ""
    print("\n" + ack + "\n")
    if input("Accept and connect? Type 'sim': ").strip().lower() != "sim":
        print("not connected")
        return 1
    key = getpass.getpass("Anthropic API key (hidden): ").strip()
    if not key:
        print("no key typed; nothing sent")
        return 1
    try:
        status, body = _act(lam, fn, a.tenant, "connect_model",
                            {"api_key": key, "model": a.model, "acknowledged": True})
    finally:
        key = ""   # noqa: F841 - drop the only reference
    if status == 200 and body.get("connection"):
        print(f"connected: {_show(body['connection'])}")
        return 0
    print(f"not connected ({status}): {body.get('reason') or ''} {body.get('error') or ''}".strip())
    return 1


if __name__ == "__main__":
    sys.exit(main())
