"""Phase D — per-tenant entitlement store + read-boundary scoping."""
from __future__ import annotations

import re
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


def test_resolve_caller_role_arn_roundtrips_and_defaults_to_none():
    t = _FakeTable()
    tc.put_tenant_config("s", "saas", ["banking"], table=t)
    assert tc.get_tenant_config("s", table=t)["resolve_caller_role_arn"] is None

    arn = "arn:aws:iam::123456789012:role/OncaResolveCallerRole"
    mk = tc.put_tenant_config(
        "tier1", "sovereign", ["banking"], plane="marketplace",
        resolve_caller_role_arn=arn, table=t,
    )
    assert mk["resolve_caller_role_arn"] == arn
    assert tc.get_tenant_config("tier1", table=t)["resolve_caller_role_arn"] == arn


def test_resolve_caller_role_arn_must_be_an_iam_role_arn():
    t = _FakeTable()
    with pytest.raises(ValueError):
        tc.put_tenant_config(
            "tier1", "sovereign", ["banking"], plane="marketplace",
            resolve_caller_role_arn="not-an-arn", table=t,
        )


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


class _FakeFederatedTable:
    """Mirrors _FakeTable's DI pattern, keyed by email (Google OAuth email->tenant map)."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def get_item(self, Key):
        it = self.items.get(Key["email"])
        return {"Item": it} if it else {}

    def put_item(self, Item):
        self.items[Item["email"]] = dict(Item)


def test_map_federated_email_roundtrip_and_normalizes_case():
    t = _FakeFederatedTable()
    row = tc.map_federated_email("Ops@Acme.Example", "acme", "saas", table=t)
    assert row == {"email": "ops@acme.example", "tenant_id": "acme", "tier": "saas"}
    assert t.items["ops@acme.example"] == row


def test_map_federated_email_rejects_unknown_tier():
    t = _FakeFederatedTable()
    with pytest.raises(ValueError, match="unknown tier"):
        tc.map_federated_email("ops@acme.example", "acme", "bogus", table=t)


def test_map_federated_email_rejects_blank_email():
    t = _FakeFederatedTable()
    with pytest.raises(ValueError, match="email is required"):
        tc.map_federated_email("   ", "acme", "saas", table=t)


def test_self_register_starts_a_14_day_trial_of_one_sector_and_maps_the_email():
    t, ft = _FakeTable(), _FakeFederatedTable()
    cfg = tc.self_register_entry_tenant(
        "New.User@Example.com", ["Crypto", "not-a-real-industry"], table=t, federated_table=ft)
    assert cfg["tier"] == "entry" and cfg["modules"] == ["crypto"]  # unknown dropped
    assert re.fullmatch(r"entry-[0-9a-f]{12}", cfg["tenant_id"])
    assert "new" not in cfg["tenant_id"] and "user" not in cfg["tenant_id"]  # #202: opaque
    assert cfg["billing"]["state"] == "trial"
    live = tc.get_tenant_config(cfg["tenant_id"], table=t)
    assert live["modules"] == ["crypto"] and live["billing"]["state"] == "trial"
    assert t.items[cfg["tenant_id"]]["modules"] == []  # entitlement comes from the trial only
    assert ft.items["new.user@example.com"] == {
        "email": "new.user@example.com", "tenant_id": cfg["tenant_id"], "tier": "entry"}


def test_self_register_requires_exactly_one_entry_sector():
    t, ft = _FakeTable(), _FakeFederatedTable()
    for picks in (["banking"], [], ["crypto", "betting"]):
        with pytest.raises(ValueError, match="exactly one"):
            tc.self_register_entry_tenant("a@b.com", picks, table=t, federated_table=ft)


def _at(days):
    import datetime as dt
    return dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc) + dt.timedelta(days=days)


def test_trial_expires_then_a_purchase_restores_then_a_lapse_removes():
    item = {"tenant_id": "e1", "tier": "entry", "modules": [], "billing_managed": True,
            "trial_until": _at(14).isoformat(), "trial_modules": ["crypto"]}
    assert tc.effective_entitlement(item, _at(13))["modules"] == ["crypto"]
    gone = tc.effective_entitlement(item, _at(15))
    assert gone["modules"] == [] and gone["billing"]["state"] == "trial_expired"
    t = _FakeTable(); t.items["e1"] = dict(item)
    tc.apply_purchase("e1", "entry", "crypto", "grant", event_id="evt_1", table=t)
    paid = tc.effective_entitlement(t.items["e1"], _at(30))
    assert paid["modules"] == ["crypto"] and paid["tier"] == "entry"
    assert paid["billing"]["state"] == "active"
    tc.apply_purchase("e1", "saas_premium", "banking", "grant", event_id="evt_2", table=t)
    up = tc.effective_entitlement(t.items["e1"], _at(30))
    assert up["tier"] == "saas" and up["modules"] == ["banking", "crypto"]
    tc.apply_purchase("e1", "saas_premium", "banking", "revoke", event_id="evt_3", table=t)
    tc.apply_purchase("e1", "entry", "crypto", "revoke", event_id="evt_4", table=t)
    end = tc.effective_entitlement(t.items["e1"], _at(30))
    assert end["modules"] == [] and end["billing"]["state"] == "lapsed"


def test_a_band_only_covers_its_own_modules():
    assert tc.module_allowed("saas_premium", "banking")
    assert not tc.module_allowed("saas_entry", "banking")      # never banking at the 2.900 price
    assert not tc.module_allowed("entry", "insurance")
    assert not tc.module_allowed("saas_premium", "private-markets")  # not launch-ready (#119)
    t = _FakeTable(); t.items["e1"] = {"tenant_id": "e1", "tier": "entry", "modules": []}
    with pytest.raises(ValueError):
        tc.apply_purchase("e1", "saas_entry", "banking", "grant", event_id="evt_x", table=t)
    with pytest.raises(KeyError):
        tc.apply_purchase("nope", "entry", "crypto", "grant", event_id="evt_y", table=t)


def test_operator_tenants_are_unchanged_and_a_reput_keeps_paid_subscriptions():
    t = _FakeTable()
    tc.put_tenant_config("acme", "saas", ["insurance"], table=t)
    cfg = tc.get_tenant_config("acme", table=t)
    assert cfg["modules"] == ["insurance"] and cfg["billing"] is None
    tc.apply_purchase("acme", "saas_mid", "acquiring", "grant", event_id="evt_5", table=t)
    tc.put_tenant_config("acme", "saas", ["insurance", "banking"], table=t)  # operator edit
    cfg = tc.get_tenant_config("acme", table=t)
    assert cfg["modules"] == ["acquiring", "banking", "insurance"]


def test_self_register_entry_tenant_rejects_blank_email():
    with pytest.raises(ValueError, match="email is required"):
        tc.self_register_entry_tenant("  ", ["crypto"], table=_FakeTable(), federated_table=_FakeFederatedTable())


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
