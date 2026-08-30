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
