#!/usr/bin/env python3
"""Copy the hand-made native users of the original Onça pool into pool V2 (#262 P1).

    python scripts/migrate_cognito_pool.py --from-pool us-east-1_LfGQGWq56 --to-pool us-east-1_XXXX
    python scripts/migrate_cognito_pool.py --from-pool ... --to-pool ... --apply

Dry-run by default: prints the plan and changes nothing. `--apply` executes it. Idempotent —
re-running after a partial apply only does what is still missing.

What it does (docs/2026-10-12-cognito-pool-v2-migration.md):
- EXTERNAL_PROVIDER (Google/Apple) users are skipped: they are re-created by their first
  federated sign-in on V2, and their tenant comes from the email->tenant map (P0).
- Native users NOT declared in CDK (infra/identity.py — the owner, directory-reviewer, ...) are
  created in V2 with AdminCreateUser: email, email_verified=true, custom:tenant, custom:tier,
  MessageAction=SUPPRESS (no invite mail).
- CDK-declared users (demo, fleet health check, QA personas) are created by the stack itself;
  they are never created here.
- Group memberships are copied for EVERY native user, matched by email.
- Permanent passwords are set ONLY where the password is already stored: directory-reviewer
  (Secrets Manager signalscompetitor/onca/directory-reviewer), the QA personas (Secrets Manager
  signalscompetitor/onca/qa-test-credentials) and the fleet health-check user (SSM
  /fleet-monitor/authtest/onca-user + onca-pass). Values are read in-process and never printed
  or written. Everyone else — the owner included — sets their own password:
  `--prompt-password EMAIL` asks for it on the terminal (getpass, never echoed) during --apply.
  NB Cognito refuses "forgot password" for a FORCE_CHANGE_PASSWORD user, so a migrated user
  without a stored password needs that (or `aws cognito-idp admin-set-user-password`).
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra"))

from identity import cdk_declared_users  # noqa: E402  (plain data module, no synth)

DIRECTORY_REVIEWER_SECRET = "signalscompetitor/onca/directory-reviewer"
QA_SECRET = "signalscompetitor/onca/qa-test-credentials"
FLEET_USER_PARAM = "/fleet-monitor/authtest/onca-user"
FLEET_PASS_PARAM = "/fleet-monitor/authtest/onca-pass"
COPIED_ATTRIBUTES = ("custom:tenant", "custom:tier")


@dataclass
class Action:
    kind: str      # create | add_group | set_password | skip | warn
    email: str
    detail: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        d = ", ".join(f"{k}={v}" for k, v in self.detail.items())
        return f"{self.kind:<13} {self.email}" + (f"  ({d})" if d else "")


def _email(user: dict) -> str:
    return (user.get("attributes", {}).get("email") or user.get("username") or "").strip().lower()


def plan_migration(source_users: Iterable[dict], target_users: dict[str, dict], *,
                   cdk_users: dict[str, dict], password_emails: Iterable[str] = (),
                   target_groups: set[str] | None = None) -> list[Action]:
    """Pure: what to do in the target pool. No AWS, no secrets.

    source_users: [{username, status, attributes: {name: value}, groups: [..]}] from the old pool.
    target_users: {email: {username, status, groups: [..]}} already in the new pool.
    cdk_users: username/email -> spec for users the stack declares (never created here).
    password_emails: emails with a STORED password (only the names; values stay elsewhere).
    target_groups: group names that exist in the target pool (None = unknown, e.g. pool not
    deployed yet in a dry run)."""
    cdk = {e.lower() for e in cdk_users}
    with_password = {e.lower() for e in password_emails}
    actions: list[Action] = []
    for u in sorted(source_users, key=_email):
        email = _email(u)
        attrs = u.get("attributes", {})
        if u.get("status") == "EXTERNAL_PROVIDER" or attrs.get("identities"):
            actions.append(Action("skip", email, {"why": "federated (re-created on first sign-in)"}))
            continue
        if not email:
            actions.append(Action("warn", u.get("username", "?"), {"why": "native user without email"}))
            continue
        target = target_users.get(email)
        is_cdk = email in cdk
        created = False
        if target is None:
            if is_cdk:
                actions.append(Action("warn", email, {"why": "CDK-declared but not in target yet: deploy first"}))
            else:
                copied = {k: attrs[k] for k in COPIED_ATTRIBUTES if attrs.get(k)}
                actions.append(Action("create", email, {"attributes": ",".join(
                    ["email", "email_verified"] + sorted(copied))} | {k: v for k, v in copied.items()}))
                created = True
        have = set((target or {}).get("groups", []))
        for g in sorted(set(u.get("groups", [])) - have):
            if target_groups is not None and g not in target_groups:
                actions.append(Action("warn", email, {"why": f"group {g!r} missing in target pool"}))
                continue
            if target is None and is_cdk:
                continue   # nothing to attach to yet; the warn above covers it
            actions.append(Action("add_group", email, {"group": g}))
        if email in with_password and (created or (target and target.get("status") != "CONFIRMED")):
            actions.append(Action("set_password", email, {"source": "stored", "permanent": True}))
        elif created:
            actions.append(Action("skip", email, {"why": "no stored password: user sets their own"}))
    return actions


# --- AWS side (thin) --------------------------------------------------------------------------

def _list_users(cip, pool_id: str, *, with_groups: bool) -> list[dict]:
    out = []
    for page in cip.get_paginator("list_users").paginate(UserPoolId=pool_id):
        for u in page["Users"]:
            rec = {"username": u["Username"], "status": u.get("UserStatus"),
                   "attributes": {a["Name"]: a["Value"] for a in u.get("Attributes", [])}, "groups": []}
            if with_groups and rec["status"] != "EXTERNAL_PROVIDER":
                rec["groups"] = [g["GroupName"] for page2 in cip.get_paginator(
                    "admin_list_groups_for_user").paginate(UserPoolId=pool_id, Username=u["Username"])
                    for g in page2["Groups"]]
            out.append(rec)
    return out


def _target_state(cip, pool_id: str) -> tuple[dict[str, dict], set[str] | None]:
    try:
        users = _list_users(cip, pool_id, with_groups=True)
        groups = {g["GroupName"] for page in cip.get_paginator("list_groups").paginate(UserPoolId=pool_id)
                  for g in page["Groups"]}
    except cip.exceptions.ResourceNotFoundException:
        print(f"note: target pool {pool_id} not found — planning against an empty pool", file=sys.stderr)
        return {}, None
    return {_email(u): u for u in users if _email(u)}, groups


class _Passwords:
    """email -> password, loaded in-process. Never printed: repr shows only the emails."""

    def __init__(self) -> None:
        self._pw: dict[str, str] = {}

    def __repr__(self) -> str:
        return f"_Passwords({sorted(self._pw)})"

    def add(self, email: str | None, password: str | None) -> None:
        if email and password:
            self._pw[email.strip().lower()] = password

    def emails(self) -> set[str]:
        return set(self._pw)

    def get(self, email: str) -> str:
        return self._pw[email]


def _load_passwords(session) -> _Passwords:
    pw = _Passwords()
    sm, ssm = session.client("secretsmanager"), session.client("ssm")
    for name, extract in (
        (DIRECTORY_REVIEWER_SECRET, lambda d: [(d.get("username"), d.get("password"))]),
        (QA_SECRET, lambda d: [(p.get("username"), p.get("password"))
                               for p in (d.get("personas") or {}).values()]),
    ):
        try:
            data = json.loads(sm.get_secret_value(SecretId=name)["SecretString"])
            for email, password in extract(data):
                pw.add(email, password)
        except Exception as e:  # noqa: BLE001 — report the source, never the content
            print(f"note: password source {name} unavailable ({type(e).__name__})", file=sys.stderr)
    try:
        user = ssm.get_parameter(Name=FLEET_USER_PARAM)["Parameter"]["Value"]
        pw.add(user, ssm.get_parameter(Name=FLEET_PASS_PARAM, WithDecryption=True)["Parameter"]["Value"])
    except Exception as e:  # noqa: BLE001
        print(f"note: password source {FLEET_PASS_PARAM} unavailable ({type(e).__name__})", file=sys.stderr)
    return pw


def _apply(cip, pool_id: str, actions: list[Action], source: dict[str, dict],
           passwords: _Passwords, prompt_for: set[str]) -> int:
    failures = 0
    for a in actions:
        try:
            if a.kind == "create":
                attrs = source[a.email]["attributes"]
                ua = [{"Name": "email", "Value": a.email}, {"Name": "email_verified", "Value": "true"}]
                ua += [{"Name": k, "Value": attrs[k]} for k in COPIED_ATTRIBUTES if attrs.get(k)]
                cip.admin_create_user(UserPoolId=pool_id, Username=a.email, UserAttributes=ua,
                                      MessageAction="SUPPRESS")
            elif a.kind == "add_group":
                cip.admin_add_user_to_group(UserPoolId=pool_id, Username=a.email, GroupName=a.detail["group"])
            elif a.kind == "set_password":
                cip.admin_set_user_password(UserPoolId=pool_id, Username=a.email,
                                            Password=passwords.get(a.email), Permanent=True)
            else:
                continue
            print(f"done   {a.describe()}")
        except Exception as e:  # noqa: BLE001 — keep going, report at the end
            failures += 1
            print(f"FAILED {a.describe()}: {type(e).__name__}", file=sys.stderr)
    for email in sorted(prompt_for):
        pw = getpass.getpass(f"new permanent password for {email} (not echoed): ")
        try:
            cip.admin_set_user_password(UserPoolId=pool_id, Username=email, Password=pw, Permanent=True)
            print(f"done   set_password  {email}  (typed)")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAILED set_password {email}: {type(e).__name__}", file=sys.stderr)
    return failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from-pool", required=True, help="the original pool id")
    ap.add_argument("--to-pool", required=True, help="the V2 pool id (stack output OncaUserPoolV2Id)")
    ap.add_argument("--apply", action="store_true", help="execute the plan (default: dry run)")
    ap.add_argument("--prompt-password", action="append", default=[], metavar="EMAIL",
                    help="with --apply: ask for this user's new permanent password (repeatable)")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--region", default="us-east-1")
    args = ap.parse_args(argv)
    if args.from_pool == args.to_pool:
        ap.error("--from-pool and --to-pool must differ")

    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    cip = session.client("cognito-idp")
    source = _list_users(cip, args.from_pool, with_groups=True)
    target, target_groups = _target_state(cip, args.to_pool)
    passwords = _load_passwords(session)
    actions = plan_migration(source, target, cdk_users=cdk_declared_users(),
                             password_emails=passwords.emails(), target_groups=target_groups)

    print(f"{'APPLY' if args.apply else 'DRY RUN'}: {args.from_pool} -> {args.to_pool}")
    for a in actions:
        print("  " + a.describe())
    counts = {k: sum(1 for a in actions if a.kind == k) for k in ("create", "add_group", "set_password", "warn")}
    print("  " + ", ".join(f"{k}: {v}" for k, v in counts.items()))
    if not args.apply:
        print("dry run — nothing changed; re-run with --apply")
        return 0
    if target_groups is None:
        print("REFUSED: target pool not found — deploy V2 first", file=sys.stderr)
        return 2
    by_email = {_email(u): u for u in source}
    return 1 if _apply(cip, args.to_pool, actions, by_email, passwords,
                       {e.strip().lower() for e in args.prompt_password}) else 0


if __name__ == "__main__":
    raise SystemExit(main())
