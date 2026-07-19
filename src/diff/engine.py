"""Diff engine — detects what changed between ingestion runs.

The product's atomic unit: 'X is new since last run'. State can live in a
local JSON file for local runs or in DynamoDB for Lambda deployments.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal
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
    """Set-of-seen-IDs state, stored as one DynamoDB item per source."""

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
        self.seen = set(seen) if seen else set()

    def save(self) -> None:
        if self.table is None:
            return
        self.table.put_item(
            Item={"source": self.source, "id": "__meta__", "seen": sorted(self.seen)}
        )


def detect_new(source: str, docs: list[dict[str, Any]], state: Any | None = None) -> list[dict[str, Any]]:
    """Return only docs not seen in previous runs, then persist state.

    For 'new item appeared' signals: fund filings, normativos, offerings,
    newly authorized entities.
    """
    state = state or JsonState(source)
    if hasattr(state, "load"):
        state.load()
    fresh = [d for d in docs if d["id"] not in state.seen]
    state.seen.update(d["id"] for d in docs)
    state.save()
    return fresh


class ValueState:
    """Tracks the last-seen numeric value per key, for time-series deltas.

    Unlike detect_new (item existence), this answers 'did this number
    move, and by how much' — the actual signal for metrics like a
    competitor's monthly Pix volume or market share.

    Lambda port note: JSON file → DynamoDB item per source.
    """

    def __init__(self, source: str):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.path = STATE_DIR / f"{source}_values.json"
        self.values: dict[str, float] = {}
        if self.path.exists():
            self.values = {k: float(v) for k, v in json.loads(self.path.read_text()).items()}

    def save(self) -> None:
        self.path.write_text(json.dumps(self.values, indent=1))


class DynamoDbValueState:
    """Numeric last-seen values, stored as one DynamoDB item per source."""

    def __init__(self, source: str, table: Any | None = None):
        self.source = source
        self.table = table
        self.values: dict[str, float] = {}
        if self.table is None:
            import boto3

            self.table = boto3.resource("dynamodb").Table(
                os.environ.get("ONCA_STATE_TABLE", "onca-state")
            )

    def load(self) -> None:
        if self.table is None:
            return
        resp = self.table.get_item(Key={"source": self.source, "id": "__values__"})
        item = resp.get("Item") or {}
        raw = item.get("values") or {}
        self.values = {str(k): float(v) for k, v in raw.items()}

    def save(self) -> None:
        if self.table is None:
            return
        # DynamoDB requires Decimal for numbers; keep keys as strings.
        payload = {k: Decimal(str(v)) for k, v in self.values.items()}
        self.table.put_item(
            Item={"source": self.source, "id": "__values__", "values": payload}
        )


def detect_moves(
    source: str,
    items: list[dict[str, Any]],
    key_field: str,
    value_field: str,
    min_pct: float = 10.0,
    state: Any | None = None,
) -> list[dict[str, Any]]:
    """Return items whose value moved >= min_pct since last run.

    Each returned item is annotated with prev_value, delta, and pct_change.
    First run establishes a baseline and returns nothing (no prior value
    to compare against) — this is intentional, not a bug.
    """
    state = state or ValueState(source)
    if hasattr(state, "load"):
        state.load()
    moves = []
    for item in items:
        key = str(item.get(key_field))
        curr = item.get(value_field)
        if curr is None:
            continue
        curr = float(curr)
        prev = state.values.get(key)
        state.values[key] = curr
        if prev is None or prev == 0:
            continue
        pct = 100 * (curr - prev) / abs(prev)
        if abs(pct) >= min_pct:
            moves.append(
                {
                    **item,
                    "prev_value": prev,
                    "delta": round(curr - prev, 2),
                    "pct_change": round(pct, 2),
                }
            )
    state.save()
    return sorted(moves, key=lambda m: abs(m["pct_change"]), reverse=True)
