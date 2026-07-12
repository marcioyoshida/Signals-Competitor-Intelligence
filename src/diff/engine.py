"""Diff engine — detects what changed between ingestion runs.

The product's atomic unit: 'X is new since last run'. State can live in a
local JSON file for local runs or in DynamoDB for Lambda deployments.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

STATE_DIR = Path(__file__).resolve().parents[2] / "data" / "state"


class JsonState:
    def __init__(self, source: str):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.path = STATE_DIR / f"{source}.json"
        self.seen: set[str] = set()
        if self.path.exists():
            self.seen = set(json.loads(self.path.read_text()))

    def save(self) -> None:
        self.path.write_text(json.dumps(sorted(self.seen), indent=1))


class DynamoDbState:
    def __init__(self, source: str, table: Any | None = None):
        self.source = source
        self.table = table
        self.seen: set[str] = set()
        if self.table is None:
            import boto3

            self.table = boto3.resource("dynamodb").Table(
                os.environ.get("ONCA_STATE_TABLE", "onca-state")
            )

    def load(self) -> None:
        if self.table is None:
            return
        resp = self.table.get_item(Key={"source": self.source, "id": "__meta__"})
        item = resp.get("Item") or {}
        seen = item.get("seen", [])
        self.seen = set(seen) if seen else self.seen

    def save(self) -> None:
        if self.table is None:
            return
        self.table.put_item(Item={"source": self.source, "id": "__meta__", "seen": sorted(self.seen)})


def detect_new(source: str, docs: list[dict[str, Any]], state: Any | None = None) -> list[dict[str, Any]]:
    """Return only docs not seen in previous runs, then persist state."""
    state = state or JsonState(source)
    if hasattr(state, "load"):
        state.load()
    fresh = [d for d in docs if d["id"] not in state.seen]
    state.seen.update(d["id"] for d in docs)
    state.save()
    return fresh
