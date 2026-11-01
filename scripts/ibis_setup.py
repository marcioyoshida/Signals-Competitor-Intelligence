#!/usr/bin/env python3
"""One-time wiring of Onça to Ibis (#293). Nothing secret is printed or written to disk.

    AWS_PROFILE=my2027 .venv/bin/python scripts/ibis_setup.py url
    AWS_PROFILE=my2027 .venv/bin/python scripts/ibis_setup.py key  --ibis-repo /path/to/Signals-License-Intelligence --ibis-python /path/to/ibis/venv/bin/python
    AWS_PROFILE=my2027 .venv/bin/python scripts/ibis_setup.py register [--url https://onssa.org/api/hooks/ibis]
    AWS_PROFILE=my2027 .venv/bin/python scripts/ibis_setup.py test

- ``url``: the Ibis API endpoint, from Ibis's own CloudFormation output, into SSM
  ``/onca/ibis/base-url`` (kept out of this public repo).
- ``key``: issues Onça's Ibis API key with Ibis's ``scripts/api_keys.py create`` (client
  ``onca-brazil-module``), written to a 0600 temp file, copied into SSM SecureString
  ``/onca/ibis/api-key`` and the temp file removed. Refuses if the parameter already exists.
- ``register``: ``PUT /v1/webhook`` with Onça's receiver; the reply's signing secret (shown once)
  goes straight into SSM SecureString ``/onca/ibis/webhook-secret``.
- ``test``: ``POST /v1/webhook/test``, then waits for the signed ``ibis.test`` delivery to land in
  the ``onca-ibis`` table (proves the URL, CloudFront, the route and the signature end to end).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import boto3

KEY_PARAM, SECRET_PARAM, URL_PARAM = "/onca/ibis/api-key", "/onca/ibis/webhook-secret", "/onca/ibis/base-url"
RECEIVER = "https://onssa.org/api/hooks/ibis"


def _ssm():
    return boto3.client("ssm", region_name="us-east-1")


def _get(name: str) -> str:
    return _ssm().get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def _put(name: str, value: str, *, overwrite: bool = False) -> None:
    _ssm().put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=overwrite,
                         Description="Onça <-> Ibis (#293)",
                         **({} if overwrite else {"Tags": [{"Key": "project", "Value": "onssa"}]}))


def _ibis(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(_get(URL_PARAM).rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"x-api-key": _get(KEY_PARAM), "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def cmd_url(_a) -> None:
    outs = boto3.client("cloudformation", region_name="us-east-1").describe_stacks(
        StackName="IbisLookupStack")["Stacks"][0]["Outputs"]
    url = next(o["OutputValue"] for o in outs if "ApiEndpoint" in o["OutputKey"])
    _put(URL_PARAM, url, overwrite=True)
    print(f"{URL_PARAM} set")


def cmd_key(a) -> None:
    try:
        _get(KEY_PARAM)
        sys.exit(f"{KEY_PARAM} already exists: revoke it on Ibis first if you mean to replace it")
    except _ssm().exceptions.ParameterNotFound:
        pass
    fd, path = tempfile.mkstemp(prefix="ibis-key-", dir="/var/tmp")
    os.close(fd)
    os.chmod(path, 0o600)
    try:
        r = subprocess.run([a.ibis_python, "scripts/api_keys.py", "create", "--client", "onca-brazil-module",
                            "--out", path], cwd=a.ibis_repo, capture_output=True, text=True)
        if r.returncode:
            sys.exit(f"api_keys.py failed: {r.stderr.strip()[-300:]}")
        print(r.stdout.strip())                     # "key for … written to …; hash <12 chars>…" — no key
        key = open(path, encoding="utf-8").read().strip()
        if not key.startswith("ibis_"):
            sys.exit("unexpected key file content")
        _put(KEY_PARAM, key)
        print(f"{KEY_PARAM} set")
    finally:
        os.remove(path)


def cmd_register(a) -> None:
    status, out = _ibis("PUT", "/v1/webhook", {"url": a.url})
    if status >= 300 or not out.get("secret"):
        sys.exit(f"PUT /v1/webhook -> {status} {out.get('error', '')}")
    _put(SECRET_PARAM, out["secret"], overwrite=True)
    print(f"webhook registered for {out.get('url')}; {SECRET_PARAM} set")


def cmd_test(_a) -> None:
    status, out = _ibis("POST", "/v1/webhook/test")
    if status >= 300:
        sys.exit(f"POST /v1/webhook/test -> {status} {out}")
    did = out.get("delivery_id")
    print(f"queued {did}")
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("onca-ibis")
    for _ in range(24):
        it = table.get_item(Key={"pk": f"dlv#{did}", "sk": "-"}).get("Item")
        if it:
            print(f"received: type={it.get('type')} at {it.get('received_at')}")
            return
        time.sleep(5)
    sys.exit("not received within 2 minutes: check GET /v1/webhook/deliveries and the OncaIbis logs")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("url")
    k = sub.add_parser("key")
    k.add_argument("--ibis-repo", required=True)
    k.add_argument("--ibis-python", required=True)
    r = sub.add_parser("register")
    r.add_argument("--url", default=RECEIVER)
    sub.add_parser("test")
    a = ap.parse_args(argv)
    {"url": cmd_url, "key": cmd_key, "register": cmd_register, "test": cmd_test}[a.cmd](a)


if __name__ == "__main__":
    main()
