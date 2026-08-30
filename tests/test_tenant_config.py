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
