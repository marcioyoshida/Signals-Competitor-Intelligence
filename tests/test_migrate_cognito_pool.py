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


# --- --invite (#262 P3): Cognito emails a temporary password; the user sets their own ----------

def test_invite_creates_with_invitation_and_never_sets_a_password():
    acts = m.plan_migration(SOURCE, _fresh_v2(), cdk_users=CDK, password_emails=STORED | {"owner@example.com"},
                            target_groups=GROUPS, invite_emails=["Owner@Example.com"])
    owner = [a for a in acts if a.email == "owner@example.com"]
    create = [a for a in owner if a.kind == "create"]
    assert len(create) == 1 and create[0].detail["invite"] is True
    assert create[0].detail["custom:tenant"] == "operator"
    assert not [a for a in owner if a.kind == "set_password"]          # even with a stored one
    assert not [a for a in owner if a.kind == "skip" and "no stored password" in a.detail.get("why", "")]
    assert sorted(a.detail["group"] for a in owner if a.kind == "add_group") == ["banking", "crypto", "fintech"]
    # the federated twin of the same email is still skipped, not invited
    assert [a.kind for a in acts if a.email == "someone@gmail.com"] == ["skip"]
    # everyone else is unaffected: no invite flag on directory-reviewer's create
    (dr,) = [a for a in acts if a.kind == "create" and a.email == "directory-reviewer@onca.example"]
    assert "invite" not in dr.detail


def test_invite_resends_only_while_force_change_password():
    target = _fresh_v2()
    target["owner@example.com"] = {"status": "FORCE_CHANGE_PASSWORD", "groups": ["banking", "fintech", "crypto"]}
    acts = m.plan_migration(SOURCE, target, cdk_users=CDK, target_groups=GROUPS, invite_emails=["owner@example.com"])
    assert [(a.kind, a.email) for a in acts if a.email == "owner@example.com"] == [("resend_invite", "owner@example.com")]
    target["owner@example.com"]["status"] = "CONFIRMED"
    acts = m.plan_migration(SOURCE, target, cdk_users=CDK, target_groups=GROUPS, invite_emails=["owner@example.com"])
    assert [a for a in acts if a.email == "owner@example.com"] == []


def test_invite_refuses_cdk_and_unknown_users():
    acts = m.plan_migration(SOURCE, _fresh_v2(), cdk_users=CDK, password_emails=STORED, target_groups=GROUPS,
                            invite_emails=["qa-test-admin@onca.example", "someone@gmail.com", "nobody@x.y"])
    warns = {(a.email, a.detail["why"]) for a in acts if a.kind == "warn"}
    assert ("qa-test-admin@onca.example", "--invite: CDK-declared user, never invited") in warns
    assert ("someone@gmail.com", "--invite: not a native user of the source pool") in warns
    assert ("nobody@x.y", "--invite: not a native user of the source pool") in warns
    assert not [a for a in acts if a.kind in ("create", "resend_invite") and a.detail.get("invite") is not None
                and a.email != "owner@example.com"]
    # the CDK QA admin still gets its stored password, not an invitation
    assert ("set_password", "qa-test-admin@onca.example") in {(a.kind, a.email) for a in acts}


class _FakeCip:
    def __init__(self):
        self.calls = []

    def admin_create_user(self, **kw):
        self.calls.append(("admin_create_user", kw))

    def admin_add_user_to_group(self, **kw):
        self.calls.append(("admin_add_user_to_group", kw))

    def admin_set_user_password(self, **kw):
        self.calls.append(("admin_set_user_password", kw))


def test_apply_sends_the_invitation_instead_of_suppressing_it():
    acts = m.plan_migration(SOURCE, _fresh_v2(), cdk_users=CDK, target_groups=GROUPS,
                            invite_emails=["owner@example.com"])
    cip = _FakeCip()
    by_email = {m._email(u): u for u in SOURCE if u["status"] != "EXTERNAL_PROVIDER"}
    assert m._apply(cip, "us-east-1_V2", acts, by_email, m._Passwords(), set()) == 0
    creates = {kw["Username"]: kw for name, kw in cip.calls if name == "admin_create_user"}
    owner = creates["owner@example.com"]
    assert "MessageAction" not in owner and owner["DesiredDeliveryMediums"] == ["EMAIL"]
    assert {"Name": "custom:tenant", "Value": "operator"} in owner["UserAttributes"]
    assert creates["directory-reviewer@onca.example"]["MessageAction"] == "SUPPRESS"
    assert not [kw for name, kw in cip.calls if name == "admin_set_user_password"]


def test_apply_resend_uses_message_action_resend():
    cip = _FakeCip()
    assert m._apply(cip, "p", [m.Action("resend_invite", "owner@example.com")], {}, m._Passwords(), set()) == 0
    assert cip.calls == [("admin_create_user", {"UserPoolId": "p", "Username": "owner@example.com",
                                                "MessageAction": "RESEND", "DesiredDeliveryMediums": ["EMAIL"]})]


def test_cli_rejects_invite_and_prompt_password_for_the_same_user():
    import pytest
    with pytest.raises(SystemExit):
        m.main(["--from-pool", "a", "--to-pool", "b", "--invite", "x@y.z", "--prompt-password", "X@y.z"])
