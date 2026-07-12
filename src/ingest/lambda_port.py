"""Lambda-style prototype for the Phase 1.5 ingestion pipeline.

This keeps the fetch functions pure and exposes a single event handler that
can later be wired to EventBridge + Lambda with minimal changes.
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3

from src.diff.engine import DynamoDbState, detect_new
from src.ingest import bcb_ifdata, bcb_normativos, cvm_fundos, raw_writer


def _new_since_last_run(source: str, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Diff docs against DynamoDB-backed state; degrade to 'everything is new' on failure."""
    try:
        return detect_new(source, docs, state=DynamoDbState(source))
    except Exception as exc:  # pragma: no cover - defensive handling for state-table issues
        print(f"Warning: {source} diff state unavailable, treating all as new: {exc}")
        return docs


def _populate_corpus_and_sync(new_docs: list[dict[str, Any]]) -> None:
    """Write new docs to the raw corpus bucket and trigger a KB ingestion sync.

    A corpus/sync failure must never break the digest response, matching the
    graceful-degradation pattern used for every other external call here.
    """
    raw_bucket = os.environ.get("ONCA_RAW_BUCKET")
    if not raw_bucket:
        return

    try:
        written = raw_writer.write_raw_documents(raw_bucket, new_docs)
    except Exception as exc:  # pragma: no cover - defensive handling for S3 write failures
        print(f"Warning: raw corpus write failed: {exc}")
        return

    if not written:
        return

    kb_id = os.environ.get("ONCA_KB_ID")
    data_source_id = os.environ.get("ONCA_KB_DATA_SOURCE_ID")
    if not kb_id or not data_source_id:
        return

    try:
        boto3.client("bedrock-agent").start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=data_source_id)
    except Exception as exc:  # pragma: no cover - defensive handling for KB sync failures
        print(f"Warning: KB ingestion sync failed: {exc}")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Return a small digest payload for downstream Lambda/CDK wiring."""
    lookback_days = int(os.environ.get("ONCA_LOOKBACK_DAYS", "7"))
    competitors = [c for c in os.environ.get("ONCA_COMPETITORS", "").split(",") if c]

    try:
        normativos = bcb_normativos.fetch_recent(days=lookback_days)
    except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
        normativos = []
        print(f"Warning: BCB normativos fetch failed: {exc}")

    try:
        funds = cvm_fundos.fetch_funds(watchlist_admins=competitors)
    except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
        funds = []
        print(f"Warning: CVM funds fetch failed: {exc}")

    try:
        base_date = bcb_ifdata.latest_base_date()
        rows = bcb_ifdata.fetch_institutions(base_date=base_date)
        names = bcb_ifdata.fetch_institution_names(base_date)
        market = bcb_ifdata.market_share(rows, institution_names=names)[:10]
    except Exception as exc:  # pragma: no cover - defensive handling for upstream API issues
        market = []
        print(f"Warning: IF.data market fetch failed: {exc}")

    new_normativos = _new_since_last_run("bcb_normativos", normativos)
    new_funds = _new_since_last_run("cvm_fundos", funds)

    _populate_corpus_and_sync(new_normativos + new_funds)

    payload = {
        "regulatory": {"count": len(normativos), "new_count": len(new_normativos), "items": new_normativos[:5]},
        "competitor": {"count": len(funds), "new_count": len(new_funds), "items": new_funds[:5]},
        "market": {"count": len(market), "items": market},
        "source": "lambda_port",
    }

    bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if bucket:
        try:
            s3 = boto3.client("s3")
            key = f"lambda-digests/{getattr(context, 'aws_request_id', 'local')}.json"
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        except Exception as exc:  # pragma: no cover - defensive handling for S3 write failures
            print(f"Warning: S3 upload failed: {exc}")

    return {"statusCode": 200, "body": json.dumps(payload, ensure_ascii=False, indent=2)}
