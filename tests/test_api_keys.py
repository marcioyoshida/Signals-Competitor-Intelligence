"""Agentic API key store — hashing, lookup, revoke, tenant-scoped listing."""
from __future__ import annotations

from typing import Any

import pytest

from src.dashboard import api_keys


class _FakeKeysTable:
    """In-memory stand-in for `onca-api-keys` + its `tenant-index` GSI."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def put_item(self, Item):
        self.items[Item["key_hash"]] = dict(Item)

    def get_item(self, Key):
        it = self.items.get(Key["key_hash"])
        return {"Item": it} if it else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues,
                     ExpressionAttributeNames=None):
        it = self.items.get(Key["key_hash"])
        if it is None:
            raise KeyError(Key["key_hash"])
        if "last_used_at" in UpdateExpression:
            it["last_used_at"] = ExpressionAttributeValues[":t"]
        if "#s" in UpdateExpression:
            it["status"] = ExpressionAttributeValues[":r"]

    def query(self, IndexName, KeyConditionExpression, ExpressionAttributeValues):
        tenant = ExpressionAttributeValues[":t"]
        key_id = ExpressionAttributeValues.get(":k")
        out = [it for it in self.items.values() if it["tenant_id"] == tenant]
        if key_id is not None:
            out = [it for it in out if it["key_id"] == key_id]
        return {"Items": out}


def test_generate_key_roundtrip_and_hash_never_stored_plaintext():
    t = _FakeKeysTable()
    created = api_keys.generate_key("acme", "prod agent", ["ask"], "owner@acme.com", table=t)
    assert created["secret"].startswith("sk_onca_")
    # The stored row has no "secret" field — only its hash.
    stored = t.items[created["key_hash"]]
    assert "secret" not in stored
    assert stored["key_hash"] == api_keys._hash(created["secret"])


def test_lookup_key_finds_active_rejects_wrong_secret_and_revoked():
    t = _FakeKeysTable()
    created = api_keys.generate_key("acme", "k1", ["ask"], "owner@acme.com", table=t)
    row = api_keys.lookup_key(created["secret"], table=t)
    assert row is not None and row["tenant_id"] == "acme"

    assert api_keys.lookup_key("sk_onca_" + "0" * 48, table=t) is None
    assert api_keys.lookup_key("not-even-the-right-format", table=t) is None

    ok = api_keys.revoke_key("acme", created["key_id"], table=t)
    assert ok is True
    assert api_keys.lookup_key(created["secret"], table=t) is None  # revoked ⇒ fails closed


def test_revoke_is_scoped_to_the_owning_tenant():
    t = _FakeKeysTable()
    created = api_keys.generate_key("acme", "k1", ["ask"], "owner@acme.com", table=t)
    # A different tenant guessing the key_id must not be able to revoke it.
    assert api_keys.revoke_key("someone-else", created["key_id"], table=t) is False
    assert api_keys.lookup_key(created["secret"], table=t) is not None


def test_list_keys_is_redacted_and_tenant_scoped():
    t = _FakeKeysTable()
    api_keys.generate_key("acme", "k1", ["ask"], "a@acme.com", table=t)
    api_keys.generate_key("acme", "k2", ["ask"], "a@acme.com", table=t)
    api_keys.generate_key("other-tenant", "k3", ["ask"], "b@other.com", table=t)

    rows = api_keys.list_keys("acme", table=t)
    assert len(rows) == 2
    assert all("key_hash" not in r for r in rows)
    assert {r["label"] for r in rows} == {"k1", "k2"}


def test_generate_key_rejects_unknown_or_empty_scopes():
    t = _FakeKeysTable()
    with pytest.raises(ValueError):
        api_keys.generate_key("acme", "k", ["act"], "a@acme.com", table=t)
    with pytest.raises(ValueError):
        api_keys.generate_key("acme", "k", [], "a@acme.com", table=t)


def test_touch_last_used_is_best_effort_on_missing_row():
    t = _FakeKeysTable()
    # Must not raise even though the hash doesn't exist.
    api_keys.touch_last_used("nonexistent-hash", table=t)
