#!/usr/bin/env python3
"""Grant/revoke the lightweight, no-tenant-config industry Cognito group access
(auth.industry_groups / tenant_config.cognito_grant_industry_group): adding someone
to the "banking" group hands them exactly banking's dashboard/feed/ask, no
DynamoDB tenant_config row and no custom:tenant/custom:tier attributes needed.

Coarse for now — one group per industry (infra/app.py's per-industry
CfnUserPoolGroup), not yet per officer-within-industry.

    python scripts/grant_industry_group.py grant analyst@buyer.example banking \
        --user-pool-id us-east-1_XXXXXXXXX --profile my2027

    python scripts/grant_industry_group.py list-groups analyst@buyer.example \
        --user-pool-id us-east-1_XXXXXXXXX

    python scripts/grant_industry_group.py revoke analyst@buyer.example banking \
        --user-pool-id us-east-1_XXXXXXXXX

`--user-pool-id` defaults to $ONCA_USER_POOL_ID.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import tenant_config as tc


def _pool_id(args) -> str:
    pool_id = args.user_pool_id or os.environ.get("ONCA_USER_POOL_ID")
    if not pool_id:
        print("REJECTED: --user-pool-id / $ONCA_USER_POOL_ID required", file=sys.stderr)
        raise SystemExit(2)
    return pool_id


def cmd_grant(args) -> int:
    try:
        outcome = tc.cognito_grant_industry_group(_pool_id(args), args.email, args.industry)
    except ValueError as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2
    print(f"OK  cognito  {args.email}  {outcome}  group={args.industry}")
    return 0


def cmd_revoke(args) -> int:
    import boto3

    client = boto3.client("cognito-idp")
    client.admin_remove_user_from_group(
        UserPoolId=_pool_id(args), Username=args.email, GroupName=args.industry,
    )
    print(f"OK  cognito  {args.email}  revoked  group={args.industry}")
    return 0


def cmd_list_groups(args) -> int:
    import boto3

    client = boto3.client("cognito-idp")
    resp = client.admin_list_groups_for_user(UserPoolId=_pool_id(args), Username=args.email)
    groups = [g["GroupName"] for g in resp.get("Groups", [])]
    print(f"{args.email}: {groups or '(no groups)'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    from src.synth.entity_registry import INDUSTRIES

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--user-pool-id", default=None, help="Cognito User Pool id (default $ONCA_USER_POOL_ID)")
    p.add_argument("--profile", default=None, help="AWS profile (sets AWS_PROFILE before boto3)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sg = sub.add_parser("grant", help="add a user to an industry group (creates the user if needed)")
    sg.add_argument("email")
    sg.add_argument("industry", choices=sorted(INDUSTRIES))
    sg.set_defaults(func=cmd_grant)

    sr = sub.add_parser("revoke", help="remove a user from an industry group")
    sr.add_argument("email")
    sr.add_argument("industry", choices=sorted(INDUSTRIES))
    sr.set_defaults(func=cmd_revoke)

    sl = sub.add_parser("list-groups", help="show a user's current group memberships")
    sl.add_argument("email")
    sl.set_defaults(func=cmd_list_groups)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
