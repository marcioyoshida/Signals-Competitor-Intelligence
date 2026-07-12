"""Diff engine — detects what changed between ingestion runs.

The product's atomic unit: 'X is new since last run'. State is a local
JSON file of previously-seen IDs per source.

Lambda port note: swap JsonState for a DynamoDB table keyed on doc id.
"""
from __future__ import annotations

import json
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


def detect_new(source: str, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only docs not seen in previous runs, then persist state."""
    state = JsonState(source)
    fresh = [d for d in docs if d["id"] not in state.seen]
    state.seen.update(d["id"] for d in docs)
    state.save()
    return fresh
