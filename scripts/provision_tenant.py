#!/usr/bin/env python3
"""Provision Onça tenants in the `onca-tenant-config` entitlement table (ADR 002
Phase D + ADR 016). This is the operator path for real tenants — it goes through
`tenant_config.put_tenant_config`, so the entry-tier guard applies: an `entry`
tenant may only license the entry-tier verticals (agri-funds, betting, consorcio,
crypto, real-estate-funds); anything else is rejected.

    # provision a real tenant (modules space/comma separated)
    python scripts/provision_tenant.py put acme-consorcio entry consorcio betting \
        --table onca-tenant-config --profile my2027

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
        cfg = tc.put_tenant_config(args.tenant_id, args.tier, modules, table=table)
    except ValueError as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2
    print(f"OK  {table_name}  {cfg['tenant_id']}  tier={cfg['tier']}  modules={cfg['modules']}")
    return 0


def cmd_get(args) -> int:
    table, _ = _resolve_table(args.table)
    cfg = tc.get_tenant_config(args.tenant_id, table=table)
    if cfg is None:
        print(f"(not provisioned) {args.tenant_id}")
        return 1
    print(f"{cfg['tenant_id']}  tier={cfg['tier']}  modules={cfg['modules']}")
    return 0


def cmd_list(args) -> int:
    table, table_name = _resolve_table(args.table)
    items = table.scan().get("Items", [])
    items.sort(key=lambda i: (str(i.get("tier")), str(i.get("tenant_id"))))
    print(f"{table_name}: {len(items)} tenant(s)")
    for it in items:
        mods = [str(m) for m in (it.get("modules") or [])]
        print(f"  {str(it.get('tenant_id')):28} {str(it.get('tier')):10} {mods}")
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
