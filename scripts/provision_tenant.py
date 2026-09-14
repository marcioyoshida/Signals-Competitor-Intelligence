#!/usr/bin/env python3
"""Provision Onça tenants in the `onca-tenant-config` entitlement table (ADR 002
Phase D + ADR 016). This is the operator path for real tenants — it goes through
`tenant_config.put_tenant_config`, so the entry-tier guard applies: an `entry`
tenant may only license the entry-tier verticals (agri-funds, betting, consorcio,
crypto, real-estate-funds); anything else is rejected.

    # provision a real tenant (modules space/comma separated)
    python scripts/provision_tenant.py put acme-consorcio entry consorcio betting \
        --table onca-tenant-config --profile my2027

    # also link a Cognito login (custom:tenant/custom:tier) for that tenant
    python scripts/provision_tenant.py put acme-consorcio entry consorcio betting \
        --email ops@acme.example --user-pool-id us-east-1_XXXXXXXXX --profile my2027

    # also let that person log in with "Continue with Google" instead of a password
    # (requires Google OAuth to be wired — see docs/google-oauth-runbook.md)
    python scripts/provision_tenant.py put acme-consorcio entry consorcio betting \
        --google-email ops@acme.example --profile my2027

    python scripts/provision_tenant.py list --profile my2027
    python scripts/provision_tenant.py get acme-consorcio
    python scripts/provision_tenant.py delete demo-banking

`--table` defaults to $ONCA_TENANT_CONFIG_TABLE then "onca-tenant-config".
`tier` selects the delivery plane (entry|saas|sovereign); `modules` are the
industry slugs the tenant is entitled to read (the Phase D read boundary).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import tenant_config as tc


def _resolve_table(name: str | None):
    import boto3

    table_name = name or os.environ.get("ONCA_TENANT_CONFIG_TABLE", "onca-tenant-config")
    return boto3.resource("dynamodb").Table(table_name), table_name


def _split_modules(raw: list[str]) -> list[str]:
    """Accept both space-separated and comma-separated module lists."""
    out: list[str] = []
    for token in raw:
        out.extend(p for p in token.replace(",", " ").split() if p)
    return out


def cmd_put(args) -> int:
    table, table_name = _resolve_table(args.table)
    modules = _split_modules(args.modules)
    try:
        cfg = tc.put_tenant_config(
            args.tenant_id, args.tier, modules, plane=args.plane, table=table,
            force_not_ready=args.force_not_ready)
    except ValueError as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2
    print(f"OK  {table_name}  {cfg['tenant_id']}  tier={cfg['tier']}  "
          f"plane={cfg['plane']}  modules={cfg['modules']}")
    # A tenant_config row alone isn't enough to log in — this links a Cognito user to it via
    # custom:tenant/custom:tier, closing the gap where only demo tenants had a scripted user
    # (see infra/app.py's deploy-time seed). Optional: omit --email to keep today's behavior.
    if args.email:
        pool_id = args.user_pool_id or os.environ.get("ONCA_USER_POOL_ID")
        if not pool_id:
            print("REJECTED: --email given but no --user-pool-id / $ONCA_USER_POOL_ID",
                  file=sys.stderr)
            return 2
        try:
            outcome = tc.cognito_upsert_user(pool_id, args.email, cfg["tenant_id"], cfg["tier"])
        except ValueError as exc:
            print(f"REJECTED (cognito): {exc}", file=sys.stderr)
            return 2
        print(f"OK  cognito  {args.email}  {outcome}  tenant={cfg['tenant_id']}  tier={cfg['tier']}")
    # Google OAuth ("Continue with Google", see docs/google-oauth-runbook.md): a federated
    # login can never get a real custom:tenant attribute (it's immutable and Cognito creates
    # that user internally with no attribute list this app controls), so it's resolved instead
    # from this email->tenant mapping by lambda_pretoken.py. Independent of --email above — a
    # person may have a password login, a Google login, both, or (if this is omitted) neither.
    if args.google_email:
        try:
            tc.map_federated_email(args.google_email, cfg["tenant_id"], cfg["tier"])
        except ValueError as exc:
            print(f"REJECTED (google): {exc}", file=sys.stderr)
            return 2
        print(f"OK  google  {args.google_email}  mapped  tenant={cfg['tenant_id']}  tier={cfg['tier']}")
    return 0


def cmd_get(args) -> int:
    table, _ = _resolve_table(args.table)
    cfg = tc.get_tenant_config(args.tenant_id, table=table)
    if cfg is None:
        print(f"(not provisioned) {args.tenant_id}")
        return 1
    print(f"{cfg['tenant_id']}  tier={cfg['tier']}  plane={cfg.get('plane')}  modules={cfg['modules']}")
    return 0


def cmd_list(args) -> int:
    table, table_name = _resolve_table(args.table)
    items = table.scan().get("Items", [])
    items.sort(key=lambda i: (str(i.get("tier")), str(i.get("tenant_id"))))
    print(f"{table_name}: {len(items)} tenant(s)")
    for it in items:
        mods = [str(m) for m in (it.get("modules") or [])]
        print(f"  {str(it.get('tenant_id')):24} {str(it.get('tier')):10} "
              f"{str(it.get('plane') or '-'):12} {mods}")
    return 0


def cmd_delete(args) -> int:
    table, _ = _resolve_table(args.table)
    table.delete_item(Key={"tenant_id": args.tenant_id})
    print(f"DELETED {args.tenant_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--table", default=None, help="DynamoDB table (default $ONCA_TENANT_CONFIG_TABLE)")
    p.add_argument("--profile", default=None, help="AWS profile (sets AWS_PROFILE before boto3)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("put", help="provision/update a tenant")
    sp.add_argument("tenant_id")
    sp.add_argument("tier", choices=tc.VALID_TIERS)
    sp.add_argument("modules", nargs="*", help="industry slugs (space or comma separated)")
    sp.add_argument("--plane", choices=tc.VALID_PLANES, default=None,
                    help="delivery plane (portal|saas|marketplace); defaults from tier")
    sp.add_argument("--force-not-ready", action="store_true",
                    help=f"override the #119 exclusion of {list(tc.NOT_READY_INDUSTRIES)} "
                         "(only for a named buyer with an explicit ingestion plan)")
    sp.add_argument("--email", default=None,
                    help="also create/update a Cognito user linked to this tenant "
                         "(custom:tenant/custom:tier); omit to write only the entitlement row")
    sp.add_argument("--user-pool-id", default=None,
                    help="Cognito User Pool id (default $ONCA_USER_POOL_ID); required with --email")
    sp.add_argument("--google-email", default=None,
                    help="also let this email log in with \"Continue with Google\" for this "
                         "tenant (writes the email->tenant mapping lambda_pretoken.py reads; "
                         "independent of --email)")
    sp.set_defaults(func=cmd_put)

    sg = sub.add_parser("get", help="show one tenant")
    sg.add_argument("tenant_id")
    sg.set_defaults(func=cmd_get)

    sl = sub.add_parser("list", help="list all tenants")
    sl.set_defaults(func=cmd_list)

    sd = sub.add_parser("delete", help="remove a tenant")
    sd.add_argument("tenant_id")
    sd.set_defaults(func=cmd_delete)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
