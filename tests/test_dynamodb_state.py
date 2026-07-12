import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.diff.engine import DynamoDbState, detect_new


def test_detect_new_uses_dynamodb_state_when_available(monkeypatch):
    calls = []

    class FakeTable:
        def __init__(self):
            self.items = {}

        def get_item(self, Key):
            return {"Item": {"id": self.items.get(Key["id"])} } if Key["id"] in self.items else {}

        def put_item(self, Item):
            self.items[Item["id"]] = Item["id"]
            calls.append(Item)

    state = DynamoDbState("demo", table=FakeTable())
    state.seen = {"a"}
    state.save()

    docs = [{"id": "a"}, {"id": "b"}]
    fresh = detect_new("demo", docs, state=state)

    assert fresh == [{"id": "b"}]
    assert calls
