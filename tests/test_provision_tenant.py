"""scripts/provision_tenant.py `put`: what it prints, returns and writes.

Written against the CLI as it was BEFORE #379 moved its writes into ``src.dashboard.onboard``
(this file passes unchanged on that version), so it pins the operator-visible behaviour: the same
lines on the same streams, the same exit codes, the same rows, the same Cognito call.
"""
from __future__ import annotations

from typing import Any

import pytest

from scripts import provision_tenant as cli
from src.dashboard import tenant_config as tc


class _Table:
    def __init__(self, key: str) -> None:
        self.key, self.items = key, {}

    def get_item(self, Key):
        it = self.items.get(Key[self.key])
        return {"Item": dict(it)} if it else {}

    def put_item(self, Item):
        self.items[Item[self.key]] = dict(Item)


class _Cognito:
    class exceptions:
        class UserNotFoundException(Exception):
            pass

    def __init__(self) -> None:
        self.users: dict[str, dict[str, str]] = {}
        self.created: list[dict[str, Any]] = []

    def admin_get_user(self, UserPoolId, Username):
        if Username not in self.users:
            raise self.exceptions.UserNotFoundException(Username)
        return {"Username": Username,
                "UserAttributes": [{"Name": k, "Value": v} for k, v in self.users[Username].items()]}

    def admin_create_user(self, **kw):
        self.created.append(kw)
        self.users[kw["Username"]] = {a["Name"]: a["Value"] for a in kw["UserAttributes"]}

    def admin_update_user_attributes(self, UserPoolId, Username, UserAttributes):
        self.users[Username].update({a["Name"]: a["Value"] for a in UserAttributes})


@pytest.fixture
def world(monkeypatch):
    import boto3

    tenants, fed, cognito = _Table("tenant_id"), _Table("email"), _Cognito()
    monkeypatch.setattr(cli, "_resolve_table", lambda name: (tenants, name or "onca-tenant-config"))
    monkeypatch.setattr(tc, "_federated_table", lambda table=None: fed)
    monkeypatch.setattr(boto3, "client", lambda service, **kw: cognito)
    monkeypatch.delenv("ONCA_USER_POOL_ID", raising=False)
    return tenants, fed, cognito


def test_put_writes_the_row_and_prints_one_ok_line(world, capsys):
    tenants, fed, cognito = world
    assert cli.main(["put", "acme", "saas", "insurance,banking", "fintech"]) == 0
    out = capsys.readouterr()
    assert out.out == ("OK  onca-tenant-config  acme  tier=saas  plane=saas  "
                       "modules=['banking', 'fintech', 'insurance']\n")
    assert out.err == ""
    assert tenants.items == {"acme": {"tenant_id": "acme", "tier": "saas", "plane": "saas",
                                      "modules": ["banking", "fintech", "insurance"]}}
    assert not fed.items and not cognito.users


def test_put_names_the_table_it_was_given_and_takes_a_plane(world, capsys):
    tenants, _fed, _cognito = world
    assert cli.main(["--table", "t-live", "put", "acme", "saas", "banking",
                     "--plane", "marketplace"]) == 0
    assert capsys.readouterr().out == ("OK  t-live  acme  tier=saas  plane=marketplace  "
                                       "modules=['banking']\n")
    assert tenants.items["acme"]["plane"] == "marketplace"


def test_the_entry_guard_rejects_on_stderr_with_exit_2_and_writes_nothing(world, capsys):
    tenants, _fed, _cognito = world
    assert cli.main(["put", "acme", "entry", "banking"]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.startswith("REJECTED: tier 'entry' may only license entry-tier industries")
    assert not tenants.items


def test_not_ready_needs_the_force_flag(world, capsys):
    tenants, _fed, _cognito = world
    assert cli.main(["put", "acme", "saas", "securitization"]) == 2
    assert "not launch-ready" in capsys.readouterr().err
    assert cli.main(["put", "acme", "saas", "securitization", "--force-not-ready"]) == 0
    assert tenants.items["acme"]["modules"] == ["securitization"]


def test_email_creates_the_cognito_user_with_cognitos_own_invitation(world, capsys):
    tenants, _fed, cognito = world
    argv = ["put", "acme", "saas", "banking", "--email", "Ops@Acme.example",
            "--user-pool-id", "pool-1"]
    assert cli.main(argv) == 0
    assert capsys.readouterr().out == (
        "OK  onca-tenant-config  acme  tier=saas  plane=saas  modules=['banking']\n"
        "OK  cognito  Ops@Acme.example  created  tenant=acme  tier=saas\n")
    # the invitation is Cognito's (no MessageAction), and the address is passed as typed
    assert cognito.created == [{
        "UserPoolId": "pool-1", "Username": "Ops@Acme.example",
        "UserAttributes": [{"Name": "email", "Value": "Ops@Acme.example"},
                           {"Name": "email_verified", "Value": "true"},
                           {"Name": "custom:tenant", "Value": "acme"},
                           {"Name": "custom:tier", "Value": "saas"}],
        "DesiredDeliveryMediums": ["EMAIL"]}]
    assert cli.main(argv) == 0   # again: the user is updated, not re-created
    assert capsys.readouterr().out.endswith("OK  cognito  Ops@Acme.example  updated  tenant=acme  tier=saas\n")
    assert len(cognito.created) == 1


def test_the_pool_id_falls_back_to_the_environment(world, capsys, monkeypatch):
    _tenants, _fed, cognito = world
    monkeypatch.setenv("ONCA_USER_POOL_ID", "pool-env")
    assert cli.main(["put", "acme", "saas", "banking", "--email", "ops@acme.example"]) == 0
    assert cognito.created[0]["UserPoolId"] == "pool-env"


def test_email_without_a_pool_is_rejected_after_the_row_was_written(world, capsys):
    tenants, _fed, cognito = world
    assert cli.main(["put", "acme", "saas", "banking", "--email", "ops@acme.example"]) == 2
    out = capsys.readouterr()
    assert out.out == "OK  onca-tenant-config  acme  tier=saas  plane=saas  modules=['banking']\n"
    assert out.err == "REJECTED: --email given but no --user-pool-id / $ONCA_USER_POOL_ID\n"
    assert "acme" in tenants.items and not cognito.users


def test_an_email_linked_to_another_tenant_is_rejected_by_the_cognito_guard(world, capsys):
    _tenants, _fed, cognito = world
    cognito.users["ops@acme.example"] = {"custom:tenant": "other", "custom:tier": "saas"}
    assert cli.main(["put", "acme", "saas", "banking", "--email", "ops@acme.example",
                     "--user-pool-id", "pool-1"]) == 2
    out = capsys.readouterr()
    assert out.out == "OK  onca-tenant-config  acme  tier=saas  plane=saas  modules=['banking']\n"
    assert out.err == ("REJECTED (cognito): 'ops@acme.example' is already linked to tenant 'other', "
                       "not 'acme' — custom:tenant is immutable, use a different email or a new user\n")
    assert cognito.users["ops@acme.example"]["custom:tenant"] == "other"


def test_google_email_maps_the_login_and_both_logins_print_in_order(world, capsys):
    _tenants, fed, _cognito = world
    assert cli.main(["put", "acme", "entry", "crypto", "--email", "ops@acme.example",
                     "--user-pool-id", "pool-1", "--google-email", "Ana@Acme.example"]) == 0
    assert capsys.readouterr().out == (
        "OK  onca-tenant-config  acme  tier=entry  plane=portal  modules=['crypto']\n"
        "OK  cognito  ops@acme.example  created  tenant=acme  tier=entry\n"
        "OK  google  Ana@Acme.example  mapped  tenant=acme  tier=entry\n")
    assert fed.items == {"ana@acme.example": {"email": "ana@acme.example", "tenant_id": "acme",
                                              "tier": "entry"}}


def test_a_blank_google_email_is_rejected_by_the_mapping_guard(world, capsys):
    _tenants, fed, _cognito = world
    assert cli.main(["put", "acme", "saas", "banking", "--google-email", "  "]) == 2
    out = capsys.readouterr()
    assert out.err == "REJECTED (google): email is required\n"
    assert not fed.items


def test_a_reput_keeps_what_the_storefront_manages(world):
    tenants, _fed, _cognito = world
    assert cli.main(["put", "acme", "saas", "insurance"]) == 0
    tc.apply_purchase("acme", "saas_mid", "acquiring", "grant", event_id="evt_1", table=tenants)
    assert cli.main(["put", "acme", "saas", "insurance", "banking"]) == 0
    row = tenants.items["acme"]
    assert row["modules"] == ["banking", "insurance"] and row["billing_managed"] is True
    assert row["subs"]["acquiring"]["status"] == "active"
    assert "onboard" not in row   # the CLI never stamps: a hand-provisioned tenant
