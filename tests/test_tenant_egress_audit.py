"""ADR 016 addendum (2026-09-22) Decision 5, check 2: the synth-time egress
assertion on the tenant CDK stack (`infra/egress_audit.py`).

Skipped entirely when `aws_cdk` isn't installed (it lives only in `.venv/`,
not the repo's main test environment — same as every other CDK-dependent
path in this repo, which is validated via `cdk synth` using the venv
interpreter, not through the main pytest run). Run this file directly with:
    .venv/bin/python -m pytest tests/test_tenant_egress_audit.py -q
"""
import sys
from pathlib import Path

import pytest

pytest.importorskip("aws_cdk")

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "infra"))

from aws_cdk import App, Stack  # noqa: E402
from aws_cdk import aws_iam as iam  # noqa: E402
from aws_cdk import aws_s3 as s3  # noqa: E402

from egress_audit import CrossAccountEgressError, assert_single_account_egress  # noqa: E402
from tenant_stack import OncaTenantStack  # noqa: E402


def test_the_real_tenant_stack_passes_clean():
    # The actual deployable — this is the assertion that matters: today's
    # OncaTenantStack grants nothing to any foreign account.
    app = App()
    stack = OncaTenantStack(app, "OncaTenantStackTest")
    assert_single_account_egress(stack)  # must not raise


def test_a_role_trusting_a_foreign_account_is_caught():
    # Proof the checker isn't a no-op: inject a real violation and confirm
    # it's caught, the same discipline test_no_hardcoded_vendor_refs.py's
    # meta-test applies to check 1.
    app = App()
    stack = Stack(app, "BadStack")
    iam.Role(
        stack, "LeakyRole",
        assumed_by=iam.AccountPrincipal("999999999999"),  # some OTHER account
    )
    with pytest.raises(CrossAccountEgressError, match="999999999999"):
        assert_single_account_egress(stack)


def test_a_bucket_policy_granting_a_foreign_account_is_caught():
    app = App()
    stack = Stack(app, "BadStack2")
    bucket = s3.Bucket(stack, "LeakyBucket")
    bucket.add_to_resource_policy(
        iam.PolicyStatement(
            actions=["s3:GetObject"],
            principals=[iam.AccountPrincipal("111111111111")],
            resources=[bucket.arn_for_objects("*")],
        )
    )
    with pytest.raises(CrossAccountEgressError, match="111111111111"):
        assert_single_account_egress(stack)


def test_a_same_account_service_principal_is_never_flagged():
    # bedrock.amazonaws.com / lambda.amazonaws.com assuming a role WITHIN this
    # same stack's account is not cross-account egress — must pass clean.
    app = App()
    stack = Stack(app, "GoodStack")
    iam.Role(stack, "OkRole", assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"))
    assert_single_account_egress(stack)  # must not raise


def test_a_deny_statement_with_wildcard_principal_is_never_flagged():
    # enforce_ssl=True on an S3 bucket generates exactly this shape (deny
    # non-TLS access to everyone) — a security best practice, not a grant.
    # Reproduced the false positive this test guards against live, against
    # the real OncaTenantStack, before fixing it (raw/digests buckets both
    # tripped the checker until Effect was checked).
    app = App()
    stack = Stack(app, "SslStack")
    s3.Bucket(stack, "SslBucket", enforce_ssl=True)
    assert_single_account_egress(stack)  # must not raise


def test_an_explicitly_allow_listed_account_id_is_permitted():
    import egress_audit

    app = App()
    stack = Stack(app, "AllowListedStack")
    iam.Role(stack, "TrustedCrossAccountRole", assumed_by=iam.AccountPrincipal("222222222222"))

    original = egress_audit.ALLOWED_CROSS_ACCOUNT_ACCOUNT_IDS
    try:
        egress_audit.ALLOWED_CROSS_ACCOUNT_ACCOUNT_IDS = frozenset({"222222222222"})
        assert_single_account_egress(stack)  # must not raise now that it's allow-listed
    finally:
        egress_audit.ALLOWED_CROSS_ACCOUNT_ACCOUNT_IDS = original
