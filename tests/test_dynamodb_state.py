import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.diff.engine import DynamoDbState, detect_new


class FakeTable:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get((Key["source"], Key["id"]))
        return {"Item": item} if item is not None else {}

    def put_item(self, Item):
        self.items[(Item["source"], Item["id"])] = Item


def _collect_seen(table, source):
    """Reconstruct the persisted seen set from sharded items."""
    meta = table.items.get((source, "__meta__"), {})
    seen = set()
    for i in range(int(meta.get("shard_count", 0) or 0)):
        seen.update(table.items[(source, f"__seen__#{i}")]["seen"])
    return seen


def test_detect_new_uses_dynamodb_state_when_available():
    table = FakeTable()
    state = DynamoDbState("demo", table=table)
    state.seen = {"a"}
    state.save()

    docs = [{"id": "a"}, {"id": "b"}]
    fresh = detect_new("demo", docs, state=state)

    assert fresh == [{"id": "b"}]
    # Persisted (sharded) state carries both ids.
    assert _collect_seen(table, "demo") == {"a", "b"}


def test_dynamodb_state_shards_large_set_and_round_trips():
    table = FakeTable()
    ids = {f"id-{n}" for n in range(2500)}  # > 2 shards at SHARD_SIZE=1000

    writer = DynamoDbState("big", table=table)
    writer.seen = set(ids)
    writer.save()

    assert table.items[("big", "__meta__")]["shard_count"] == 3
    assert all(
        len(table.items[("big", f"__seen__#{i}")]["seen"]) <= DynamoDbState.SHARD_SIZE
        for i in range(3)
    )

    reader = DynamoDbState("big", table=table)
    reader.load()
    assert reader.seen == ids


def test_dynamodb_state_migrates_legacy_single_item_seen():
    table = FakeTable()
    # Legacy layout: a single __meta__ item carrying the whole seen list.
    table.items[("demo", "__meta__")] = {
        "source": "demo",
        "id": "__meta__",
        "seen": ["x", "y"],
    }
    state = DynamoDbState("demo", table=table)
    state.load()
    assert state.seen == {"x", "y"}

    state.seen.add("z")
    state.save()
    # Migrated to shards; __meta__ now only holds shard_count.
    assert "seen" not in table.items[("demo", "__meta__")]
    assert _collect_seen(table, "demo") == {"x", "y", "z"}
