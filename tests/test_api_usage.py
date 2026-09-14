"""Agentic API per-tenant token-usage counters — atomic ADD semantics."""
from __future__ import annotations

from typing import Any

from src.dashboard import api_usage


class _FakeUsageTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues):
        k = (Key["tenant_id"], Key["period"])
        it = self.items.setdefault(k, {"tokens_used": 0, "calls": 0})
        it["tokens_used"] += ExpressionAttributeValues[":t"]
        it["calls"] += ExpressionAttributeValues[":c"]
        it["updated_at"] = ExpressionAttributeValues[":u"]

    def get_item(self, Key):
        it = self.items.get((Key["tenant_id"], Key["period"]))
        return {"Item": it} if it else {}


def test_record_usage_accumulates_across_calls():
    t = _FakeUsageTable()
    api_usage.record_usage("acme", 120, table=t)
    api_usage.record_usage("acme", 380, table=t)
    usage = api_usage.get_usage("acme", table=t)
    assert usage["tokens_used"] == 500
    assert usage["calls"] == 2


def test_record_usage_zero_or_negative_is_a_noop():
    t = _FakeUsageTable()
    api_usage.record_usage("acme", 0, table=t)
    api_usage.record_usage("acme", -5, table=t)
    assert t.items == {}


def test_get_usage_defaults_to_zero_for_unseen_tenant():
    t = _FakeUsageTable()
    usage = api_usage.get_usage("nobody-yet", table=t)
    assert usage == {"period": usage["period"], "tokens_used": 0, "calls": 0}


def test_usage_is_scoped_per_tenant():
    t = _FakeUsageTable()
    api_usage.record_usage("acme", 100, table=t)
    api_usage.record_usage("other", 50, table=t)
    assert api_usage.get_usage("acme", table=t)["tokens_used"] == 100
    assert api_usage.get_usage("other", table=t)["tokens_used"] == 50
