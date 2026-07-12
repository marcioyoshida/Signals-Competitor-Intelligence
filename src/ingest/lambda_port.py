"""Lambda-style prototype for the Phase 1.5 ingestion pipeline.

This keeps the fetch functions pure and exposes a single event handler that
can later be wired to EventBridge + Lambda with minimal changes.
"""
from __future__ import annotations

import json
from typing import Any

from src.ingest import bcb_ifdata, bcb_normativos, cvm_fundos


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Return a small digest payload for downstream Lambda/CDK wiring."""
    normativos = bcb_normativos.fetch_recent(days=7)
    funds = cvm_fundos.fetch_funds(watchlist_admins=[])

    try:
        base_date = bcb_ifdata.latest_base_date()
        rows = bcb_ifdata.fetch_institutions(base_date=base_date)
        market = bcb_ifdata.market_share(rows)[:10]
    except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
        market = []
        print(f"Warning: IF.data market fetch failed: {exc}")

    payload = {
        "regulatory": {"count": len(normativos), "items": normativos[:5]},
        "competitor": {"count": len(funds), "items": funds[:5]},
        "market": {"count": len(market), "items": market},
        "source": "lambda_port",
    }
    return {"statusCode": 200, "body": json.dumps(payload, ensure_ascii=False, indent=2)}
