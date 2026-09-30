#!/usr/bin/env python3
"""Pre-register a CONFIDENTIAL OAuth client on Onça's authorization server (#184).

For connectors that can't use Client ID Metadata Documents — e.g. a Microsoft Copilot Studio MCP
tool in "Manual" OAuth mode, which needs a client id + secret and gives you ITS callback URL. Only
the secret's SHA-256 is stored; the secret is written ONCE to a local file (mode 600) for the
tenant admin and never printed.

  AWS_PROFILE=my2027 .venv/bin/python scripts/oauth_register_client.py \\
      --name "Copilot Studio (Acme)" --redirect "https://global.consent.azure-apim.net/redirect/..." \\
      --out /secure/place/acme-copilot-client.txt
  Gemini Enterprise (A2A, #186): --resource /a2a --redirect https://vertexaisearch.cloud.google.com/oauth-redirect
      --redirect https://vertexaisearch.cloud.google.com/static/oauth/oauth.html
  ... --revoke <client_id>        # delete a registration (its grants stop at the next refresh)
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _table():
    import boto3

    # list_stack_resources, paginated: describe_stack_resources stops at 100 and this stack has more
    pages = boto3.client("cloudformation").get_paginator("list_stack_resources").paginate(StackName="OncaPrototypeStack")
    name = next(r["PhysicalResourceId"] for pg in pages for r in pg["StackResourceSummaries"]
                if r["ResourceType"] == "AWS::DynamoDB::Table" and r["LogicalResourceId"].startswith("OncaOAuthTable"))
    return boto3.resource("dynamodb").Table(name)


def main(argv: list[str] | None = None) -> int:
    from src.dashboard.oauth import tokens

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name")
    p.add_argument("--redirect", action="append", default=[])
    p.add_argument("--out")
    p.add_argument("--resource", default="/mcp", choices=["/mcp", "/a2a", "/mcp/ops"],
                   help="audience when the platform omits `resource` (Gemini Enterprise A2A: /a2a)")
    p.add_argument("--revoke")
    a = p.parse_args(argv)
    t = _table()
    if a.revoke:
        t.delete_item(Key={"pk": "client#" + a.revoke, "sk": "-"})
        print("revoked", a.revoke)
        return 0
    if not a.name or not a.redirect or not a.out:
        p.error("--name, --redirect and --out are required")
    for r in a.redirect:
        u = urlsplit(r)
        if u.scheme != "https" or u.fragment:
            p.error("redirect URIs must be https without a fragment: " + r)
    client_id = "onca-" + secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    t.put_item(Item={"pk": "client#" + client_id, "sk": "-", "client_id": client_id,
                     "client_name": a.name[:80], "redirect_uris": a.redirect, "confidential": True,
                     "secret_sha256": tokens.sha256_hex(secret), "default_resource": a.resource,
                     "created": int(time.time())})
    out = Path(a.out)
    out.write_text("client_id=%s\nclient_secret=%s\n" % (client_id, secret))
    os.chmod(out, 0o600)
    print("registered %s (%s) — secret written to %s" % (client_id, a.name, out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
