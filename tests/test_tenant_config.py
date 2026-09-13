"""Phase D — per-tenant entitlement store + read-boundary scoping."""
from __future__ import annotations

from typing import Any

import pytest

from src.dashboard import tenant_config as tc
from src.dashboard.agent_ask import _scope_cards_to_modules


class _FakeTable:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def get_item(self, Key):
        it = self.items.get(Key["tenant_id"])
        return {"Item": it} if it else {}

    def put_item(self, Item):
        self.items[Item["tenant_id"]] = dict(Item)


def test_put_get_tenant_config_roundtrip():
    t = _FakeTable()
    tc.put_tenant_config("acme", "saas", ["Banking", "insurance", ""], table=t)
    cfg = tc.get_tenant_config("acme", table=t)
    assert cfg["tier"] == "saas"
    assert cfg["modules"] == ["banking", "insurance"]  # normalized, deduped, blanks dropped
    assert tc.get_tenant_config("nobody", table=t) is None  # unprovisioned ⇒ None


def test_not_ready_industries_rejected_on_any_tier_without_force():
    # issue #119: a not-ready sector must not slip into a SaaS/Sovereign entitlement either —
    # those tiers have no allow-list otherwise, so this is the only guard they get.
    t = _FakeTable()
    with pytest.raises(ValueError, match="not launch-ready"):
        tc.put_tenant_config("dp1", "saas", ["banking", "securitization"], table=t)
    assert tc.get_tenant_config("dp1", table=t) is None  # rejected, not partially provisioned


def test_not_ready_industries_allowed_with_explicit_force():
    t = _FakeTable()
    cfg = tc.put_tenant_config("dp2", "saas", ["private-markets"], table=t, force_not_ready=True)
    assert cfg["modules"] == ["private-markets"]


def test_put_rejects_bad_tier():
    with pytest.raises(ValueError):
        tc.put_tenant_config("x", "premium", ["banking"], table=_FakeTable())


def test_entry_tier_capped_to_entry_industries():
    t = _FakeTable()
    # an entry tenant may license the entry-tier verticals...
    cfg = tc.put_tenant_config("consorcio-co", "entry", ["consorcio", "betting"], table=t)
    assert cfg["modules"] == ["betting", "consorcio"]
    # ...but never a higher-tier industry — reject rather than silently drop.
    with pytest.raises(ValueError):
        tc.put_tenant_config("bad", "entry", ["consorcio", "banking"], table=t)
    with pytest.raises(ValueError):
        tc.put_tenant_config("bad2", "entry", ["banking"], table=t)


def test_delivery_plane_default_and_explicit():
    t = _FakeTable()
    # entry defaults to the portal plane; higher tiers default to saas.
    assert tc.put_tenant_config("e", "entry", ["consorcio"], table=t)["plane"] == "portal"
    assert tc.put_tenant_config("s", "saas", ["banking"], table=t)["plane"] == "saas"
    # tier-1 can be delivered as AWS Marketplace (in-account) instead of shared SaaS.
    mk = tc.put_tenant_config("tier1", "sovereign", ["banking"], plane="marketplace", table=t)
    assert mk["plane"] == "marketplace"
    assert tc.get_tenant_config("tier1", table=t)["plane"] == "marketplace"
    with pytest.raises(ValueError):
        tc.put_tenant_config("x", "saas", ["banking"], plane="bogus", table=t)


def test_higher_tiers_are_unrestricted():
    t = _FakeTable()
    # saas/sovereign may license any industry, including entry ones.
    assert tc.put_tenant_config("bank", "saas", ["banking", "consorcio"], table=t)["modules"] \
        == ["banking", "consorcio"]
    assert tc.put_tenant_config("tier1", "sovereign", ["investment-banking"], table=t)["modules"] \
        == ["investment-banking"]
    assert tc.allowed_industries_for_tier("entry") == frozenset(tc.ENTRY_INDUSTRIES)
    assert tc.allowed_industries_for_tier("saas") is None


def test_entitled_helper():
    cfg = {"modules": ["banking", "crypto"]}
    assert tc.entitled(cfg, ["banking"]) is True
    assert tc.entitled(cfg, ["insurance"]) is False
    assert tc.entitled({"modules": []}, ["banking"]) is False  # fail closed


class _FakeCognitoExceptions:
    class UserNotFoundException(Exception):
        pass


class _FakeCognito:
    """Mirrors _FakeTable's DI pattern — no moto/localstack needed for cognito_upsert_user."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, str]] = {}
        self.groups: dict[str, set[str]] = {}
        self.exceptions = _FakeCognitoExceptions

    def admin_get_user(self, UserPoolId, Username):
        if Username not in self.users:
            raise self.exceptions.UserNotFoundException(Username)
        attrs = self.users[Username]
        return {"UserAttributes": [{"Name": k, "Value": v} for k, v in attrs.items()]}

    def admin_create_user(self, UserPoolId, Username, UserAttributes, DesiredDeliveryMediums=None):
        self.users[Username] = {a["Name"]: a["Value"] for a in UserAttributes}

    def admin_update_user_attributes(self, UserPoolId, Username, UserAttributes):
        self.users[Username].update({a["Name"]: a["Value"] for a in UserAttributes})

    def admin_add_user_to_group(self, UserPoolId, Username, GroupName):
        self.groups.setdefault(Username, set()).add(GroupName)

    def admin_remove_user_from_group(self, UserPoolId, Username, GroupName):
        self.groups.get(Username, set()).discard(GroupName)

    def admin_list_groups_for_user(self, UserPoolId, Username):
        return {"Groups": [{"GroupName": g} for g in sorted(self.groups.get(Username, set()))]}


def test_cognito_upsert_user_creates_new_user():
    c = _FakeCognito()
    outcome = tc.cognito_upsert_user("pool1", "dp@example.com", "acme", "saas", client=c)
    assert outcome == "created"
    assert c.users["dp@example.com"]["custom:tenant"] == "acme"
    assert c.users["dp@example.com"]["custom:tier"] == "saas"


def test_cognito_upsert_user_updates_tier_for_same_tenant():
    c = _FakeCognito()
    tc.cognito_upsert_user("pool1", "dp@example.com", "acme", "entry", client=c)
    outcome = tc.cognito_upsert_user("pool1", "dp@example.com", "acme", "saas", client=c)
    assert outcome == "updated"
    assert c.users["dp@example.com"]["custom:tier"] == "saas"
    assert c.users["dp@example.com"]["custom:tenant"] == "acme"  # unchanged (immutable)


def test_cognito_upsert_user_rejects_tenant_mismatch():
    c = _FakeCognito()
    tc.cognito_upsert_user("pool1", "dp@example.com", "acme", "saas", client=c)
    with pytest.raises(ValueError, match="already linked"):
        tc.cognito_upsert_user("pool1", "dp@example.com", "other-tenant", "saas", client=c)


def test_cognito_grant_industry_group_creates_user_without_tenant_attrs():
    c = _FakeCognito()
    outcome = tc.cognito_grant_industry_group("pool1", "analyst@buyer.example", "banking", client=c)
    assert outcome == "created"
    assert "banking" in c.groups["analyst@buyer.example"]
    # group is the entitlement — no tenant_config row / custom:tenant needed at all
    assert "custom:tenant" not in c.users["analyst@buyer.example"]


def test_cognito_grant_industry_group_grants_existing_user():
    c = _FakeCognito()
    tc.cognito_grant_industry_group("pool1", "analyst@buyer.example", "banking", client=c)
    outcome = tc.cognito_grant_industry_group("pool1", "analyst@buyer.example", "fintech", client=c)
    assert outcome == "granted"
    assert c.groups["analyst@buyer.example"] == {"banking", "fintech"}


def test_cognito_grant_industry_group_rejects_unknown_industry():
    c = _FakeCognito()
    with pytest.raises(ValueError, match="not a known industry"):
        tc.cognito_grant_industry_group("pool1", "analyst@buyer.example", "not-a-real-industry", client=c)


def test_scope_cards_to_modules_read_boundary():
    feed = {"entity_attrs": {
        "itau": {"industries": ["banking"]},
        "binance": {"industries": ["crypto"]},
    }}
    cards = [{"id": "c1", "entity": "itau"}, {"id": "c2", "entity": "binance"},
             {"id": "c3", "entity": None}]  # unattributed → never entitled to a scoped tenant
    assert [c["id"] for c in _scope_cards_to_modules(cards, feed, ["banking"])] == ["c1"]
    assert _scope_cards_to_modules(cards, feed, []) == []  # empty modules ⇒ fail closed
    assert {c["id"] for c in _scope_cards_to_modules(cards, feed, ["banking", "crypto"])} == {"c1", "c2"}
