import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.diff.engine import DynamoDbState, commit_seen, detect_new


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


def test_detect_new_commit_false_does_not_burn():
    """commit=False computes fresh without marking anything seen (issue #23)."""
    table = FakeTable()
    state = DynamoDbState("trade_press", table=table)
    state.seen = {"a"}
    state.save()

    docs = [{"id": "a"}, {"id": "b"}]
    fresh = detect_new("trade_press", docs, state=state, commit=False)

    # Fresh set is correct ...
    assert fresh == [{"id": "b"}]
    # ... but nothing new was persisted: 'b' is NOT burned, so a fetch-only run
    # (or a synth that never consumed the slice) leaves it to re-surface.
    assert _collect_seen(table, "trade_press") == {"a"}


def test_commit_seen_marks_ids_after_consumption():
    """The deferred second phase: commit_seen persists the consumed ids."""
    table = FakeTable()
    state = DynamoDbState("trade_press", table=table)
    state.seen = {"a"}
    state.save()

    # Fresh computed without commit, then committed once consumed.
    detect_new("trade_press", [{"id": "a"}, {"id": "b"}], state=state, commit=False)
    size = commit_seen("trade_press", ["a", "b"], state=DynamoDbState("trade_press", table=table))

    assert size == 2
    assert _collect_seen(table, "trade_press") == {"a", "b"}
    # Idempotent: re-committing the same ids is a no-op union.
    assert commit_seen("trade_press", ["b"], state=DynamoDbState("trade_press", table=table)) == 2


def test_deferred_commit_round_trip_no_burn_until_synth():
    """End-to-end of the #23 fix: item stays fresh across fetch-only runs until
    a consumer commits it, then it stops re-firing."""
    table = FakeTable()
    # Run 1 (fetch only, no synth): compute fresh, do not commit.
    fresh1 = detect_new(
        "trade_press", [{"id": "x"}], state=DynamoDbState("trade_press", table=table), commit=False
    )
    assert fresh1 == [{"id": "x"}]
    # Run 2 (still no synth): 'x' is STILL fresh — never burned.
    fresh2 = detect_new(
        "trade_press", [{"id": "x"}], state=DynamoDbState("trade_press", table=table), commit=False
    )
    assert fresh2 == [{"id": "x"}]
    # Synth consumes and commits.
    commit_seen("trade_press", ["x"], state=DynamoDbState("trade_press", table=table))
    # Run 3: now correctly not fresh (no re-fire).
    fresh3 = detect_new(
        "trade_press", [{"id": "x"}], state=DynamoDbState("trade_press", table=table), commit=False
    )
    assert fresh3 == []


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
