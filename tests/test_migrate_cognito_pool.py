"""#262 P1: scripts/migrate_cognito_pool.py planning logic (pure — no AWS, no secrets)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import migrate_cognito_pool as m  # noqa: E402

CDK = m.cdk_declared_users()
GROUPS = {"banking", "fintech", "operator", "crypto"}


def _u(email, status="CONFIRMED", groups=(), **attrs):
    return {"username": f"sub-{email}", "status": status, "groups": list(groups),
            "attributes": {"email": email, **attrs}}


SOURCE = [
    _u("owner@example.com", groups=["banking", "fintech", "crypto"],
       **{"custom:tenant": "operator", "custom:tier": "operator"}),
    _u("directory-reviewer@onca.example", **{"custom:tenant": "directory-reviewer", "custom:tier": "saas"}),
    _u("qa-test-admin@onca.example", groups=["operator"],
       **{"custom:tenant": "qa-internal-admin", "custom:tier": "sovereign"}),
    _u("fleet-health-check@onca.example", **{"custom:tenant": "acquiring", "custom:tier": "saas"}),
    _u("demo-fintech@onca.example", status="FORCE_CHANGE_PASSWORD"),
    _u("someone@gmail.com", status="EXTERNAL_PROVIDER", groups=["banking"],
       identities='[{"providerName":"Google"}]'),
]
STORED = {"directory-reviewer@onca.example", "qa-test-admin@onca.example", "fleet-health-check@onca.example"}


def _by(actions, kind):
    return [(a.email, a.detail) for a in actions if a.kind == kind]


def _fresh_v2():
    """V2 right after deploy: only the CDK users exist, in FORCE_CHANGE_PASSWORD, with the CDK grant."""
    return {e: {"username": e, "status": "FORCE_CHANGE_PASSWORD", "groups": spec["groups"]}
            for e, spec in CDK.items()}


def test_creates_only_non_cdk_native_users_with_tenant_and_tier():
    acts = m.plan_migration(SOURCE, _fresh_v2(), cdk_users=CDK, password_emails=STORED, target_groups=GROUPS)
    created = dict(_by(acts, "create"))
    assert set(created) == {"owner@example.com", "directory-reviewer@onca.example"}
    assert created["owner@example.com"]["custom:tenant"] == "operator"
    assert created["owner@example.com"]["custom:tier"] == "operator"
    assert created["directory-reviewer@onca.example"]["attributes"] == "email,email_verified,custom:tenant,custom:tier"


def test_federated_users_are_skipped_entirely():
    acts = m.plan_migration(SOURCE, _fresh_v2(), cdk_users=CDK, password_emails=STORED, target_groups=GROUPS)
    assert [a.kind for a in acts if a.email == "someone@gmail.com"] == ["skip"]


def test_group_memberships_copied_for_every_native_user_by_email():
    acts = m.plan_migration(SOURCE, _fresh_v2(), cdk_users=CDK, password_emails=STORED, target_groups=GROUPS)
    groups = _by(acts, "add_group")
    assert sorted(g["group"] for e, g in groups if e == "owner@example.com") == ["banking", "crypto", "fintech"]
    # the QA admin's operator grant is already declared by CDK on V2 -> nothing to add
    assert not [g for e, g in groups if e == "qa-test-admin@onca.example"]


def test_passwords_only_where_stored_and_never_for_the_owner():
    acts = m.plan_migration(SOURCE, _fresh_v2(), cdk_users=CDK, password_emails=STORED, target_groups=GROUPS)
    assert {e for e, _ in _by(acts, "set_password")} == STORED
    assert ("owner@example.com", {"why": "no stored password: user sets their own"}) in _by(acts, "skip")
    assert not [e for e, _ in _by(acts, "set_password") if e.startswith("demo-")]


def test_idempotent_after_apply():
    target = _fresh_v2()
    target["owner@example.com"] = {"status": "FORCE_CHANGE_PASSWORD", "groups": ["banking", "fintech", "crypto"]}
    target["directory-reviewer@onca.example"] = {"status": "CONFIRMED", "groups": []}
    for e in ("qa-test-admin@onca.example", "fleet-health-check@onca.example", "qa-test-entry@onca.example"):
        target[e]["status"] = "CONFIRMED"
    acts = m.plan_migration(SOURCE, target, cdk_users=CDK, password_emails=STORED, target_groups=GROUPS)
    assert [a for a in acts if a.kind in ("create", "add_group", "set_password")] == []


def test_dry_run_before_v2_exists_warns_instead_of_creating_cdk_users():
    acts = m.plan_migration(SOURCE, {}, cdk_users=CDK, password_emails=STORED, target_groups=None)
    warned = {e for e, _ in _by(acts, "warn")}
    assert {"qa-test-admin@onca.example", "fleet-health-check@onca.example", "demo-fintech@onca.example"} <= warned
    assert {e for e, _ in _by(acts, "create")} == {"owner@example.com", "directory-reviewer@onca.example"}


def test_missing_target_group_is_a_warning_not_an_action():
    src = [_u("owner@example.com", groups=["not-a-group"], **{"custom:tenant": "operator"})]
    acts = m.plan_migration(src, {}, cdk_users=CDK, target_groups={"banking"})
    assert not _by(acts, "add_group")
    assert _by(acts, "warn") == [("owner@example.com", {"why": "group 'not-a-group' missing in target pool"})]


def test_email_match_is_case_insensitive_and_ignores_username():
    src = [_u("Directory-Reviewer@onca.example")]
    target = {"directory-reviewer@onca.example": {"status": "CONFIRMED", "groups": []}}
    acts = m.plan_migration(src, target, cdk_users=CDK, target_groups=GROUPS)
    assert not _by(acts, "create")


def test_password_holder_never_reveals_values():
    pw = m._Passwords()
    pw.add("a@b.c", "s3cret-value")
    assert "s3cret-value" not in repr(pw) and "s3cret-value" not in str(pw)
    assert pw.emails() == {"a@b.c"}
