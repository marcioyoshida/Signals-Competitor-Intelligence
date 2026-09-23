"""ADR 016 addendum (2026-09-22), Decision 5 check 2: the synth-time egress
assertion on the tenant CDK stack.

Decision 1 argues telemetry-off is structural (deployment locality), not a
flag; Decision 5 says that claim must be a CHECK, not a line in a runbook
someone remembers to eyeball. Check 1 (`tests/test_no_hardcoded_vendor_refs.py`)
is the static source-level half: no `src/` module may reference a vendor
resource identity by literal name. This module is the CDK-level half: no
resource this stack SYNTHESIZES may grant IAM access to any AWS account other
than the tenant's own — the "one governed, audited egress" ADR 016 describes,
turned into something that fails a `cdk synth`/`cdk deploy` instead of a
property a reviewer has to notice went missing.

**What "governed egress" turned out to mean once this was actually built.**
The addendum anticipated one deliberate exception: the `/resolve` trust
relationship from Decision 3. Building it revealed that exception doesn't
exist in THIS stack's own template at all — Decision 3 shipped `/resolve` as
a resource-based policy on the VENDOR's `OncaResolveApi` Lambda
(`infra/app.py`), granted to a tenant's `OncaResolveCallerRole` ARN only AFTER
a real tenant onboards. Nothing in `tenant_stack.py` ever grants access TO the
vendor account — the relationship is the vendor trusting the tenant's role,
not the reverse. So `ALLOWED_CROSS_ACCOUNT_ACCOUNT_IDS` below is correctly
empty today: this stack's egress surface is stronger than the addendum
originally assumed it would need to be, not weaker. If a future step
legitimately needs a named exception, add the specific account id here with a
comment saying why — never a wildcard, and never "any account."
"""
from __future__ import annotations

import re
from typing import Any

from aws_cdk import Stack
from aws_cdk.assertions import Template

_AWS_SERVICE_PRINCIPAL_RE = re.compile(r"^[a-z0-9.\-]+\.amazonaws\.com$")

# See the module docstring — deliberately empty. Add an exact 12-digit AWS
# account id here (never an ARN string: CDK builds a literal ARN via
# `Fn::Join`, so it's split across several template fragments and never
# appears as one whole string to match against — the account id is the one
# substring that survives the fragmentation intact), and say why, if a future
# step legitimately needs one.
ALLOWED_CROSS_ACCOUNT_ACCOUNT_IDS: frozenset[str] = frozenset()

_IAM_STATEMENT_RESOURCE_TYPES = ("AWS::IAM::Role", "AWS::IAM::Policy", "AWS::IAM::ManagedPolicy")
_RESOURCE_POLICY_TYPES = ("AWS::S3::BucketPolicy", "AWS::Lambda::Permission", "AWS::KMS::Key")


class CrossAccountEgressError(AssertionError):
    """Raised when the synthesized template grants IAM access to an AWS
    account other than the tenant's own, outside the allow-list above."""


def _iter_principal_strings(principal: Any):
    """Flatten an IAM `Principal` value (a bare string, `{"AWS": ...}` /
    `{"Service": ...}`, a list, or a CFN intrinsic like `Fn::Join`/`Fn::GetAtt`/
    `Ref`) down to every leaf string it's built from. An unresolved intrinsic
    referencing THIS stack's own account (`Ref: AWS::AccountId`, a `Fn::GetAtt`
    on a same-template resource) flattens into fragments that never contain a
    literal 12-digit account id, so it never false-positives here — only a
    LITERAL foreign account id or ARN does."""
    if principal is None:
        return
    if isinstance(principal, str):
        yield principal
        return
    if isinstance(principal, dict):
        for v in principal.values():
            yield from _iter_principal_strings(v)
        return
    if isinstance(principal, list):
        for item in principal:
            yield from _iter_principal_strings(item)


def _check_statement(resource_type: str, logical_id: str, stmt: dict[str, Any], violations: list[str]) -> None:
    """Check one IAM policy Statement's Principal — but only if it GRANTS
    something (`Effect: Allow`). A `Deny` statement with `Principal: "*"` is
    the OPPOSITE of a grant — `enforce_ssl=True` on this stack's S3 buckets
    generates exactly that (deny non-TLS access to everyone) — so checking it
    would flag a security best practice as an egress violation."""
    if str(stmt.get("Effect") or "Allow") != "Allow":
        return
    _check_principal(resource_type, logical_id, stmt.get("Principal"), violations)


def _check_principal(resource_type: str, logical_id: str, principal: Any, violations: list[str]) -> None:
    for p in _iter_principal_strings(principal):
        if not isinstance(p, str):
            continue
        if p == "*":
            violations.append(f'{resource_type} {logical_id}: wildcard Principal "*"')
            continue
        if _AWS_SERVICE_PRINCIPAL_RE.match(p):
            continue  # a same-account AWS service principal (bedrock.amazonaws.com, etc.)
        m = re.search(r"(\d{12})", p)
        if m and m.group(1) not in ALLOWED_CROSS_ACCOUNT_ACCOUNT_IDS:
            violations.append(
                f"{resource_type} {logical_id}: principal {p!r} names account "
                f"{m.group(1)} — not this stack's own account and not allow-listed"
            )


def assert_single_account_egress(stack: Stack) -> None:
    """Enumerate every IAM principal this stack's synthesized template grants
    ANYTHING to (role trust policies, inline/managed IAM policies, resource-
    based policies on S3/Lambda/KMS), and raise `CrossAccountEgressError` if
    any of them names a specific AWS account that is neither this stack's own
    nor in `ALLOWED_CROSS_ACCOUNT_ACCOUNT_IDS`.

    Called from `tenant_app.py` on every synth, so `cdk synth`/`cdk deploy`
    against this stack fails loudly the moment a change introduces an
    ungoverned cross-account grant — not later, when a reviewer might miss it.
    """
    template = Template.from_stack(stack)
    violations: list[str] = []

    for logical_id, props in template.find_resources("AWS::IAM::Role").items():
        trust = (props.get("Properties") or {}).get("AssumeRolePolicyDocument") or {}
        for stmt in trust.get("Statement") or []:
            _check_statement("AWS::IAM::Role", logical_id, stmt, violations)

    for res_type in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
        for logical_id, props in template.find_resources(res_type).items():
            doc = (props.get("Properties") or {}).get("PolicyDocument") or {}
            for stmt in doc.get("Statement") or []:
                _check_statement(res_type, logical_id, stmt, violations)

    for res_type in _RESOURCE_POLICY_TYPES:
        for logical_id, props in template.find_resources(res_type).items():
            properties = props.get("Properties") or {}
            if "PolicyDocument" in properties:
                for stmt in (properties["PolicyDocument"] or {}).get("Statement") or []:
                    _check_statement(res_type, logical_id, stmt, violations)
            if "Principal" in properties:
                # AWS::Lambda::Permission has no Effect field at all — the
                # resource itself IS the grant, so it's always checked.
                _check_principal(res_type, logical_id, properties.get("Principal"), violations)
            if "KeyPolicy" in properties:
                for stmt in (properties["KeyPolicy"] or {}).get("Statement") or []:
                    _check_statement(res_type, logical_id, stmt, violations)

    if violations:
        raise CrossAccountEgressError(
            "Tenant-stack egress audit failed — the following IAM grant(s) reach "
            "outside this stack's own account and are not allow-listed "
            "(ADR 016 addendum Decision 5, check 2):\n  " + "\n  ".join(violations)
        )
