"""O2 (#137): Lambda entry point that runs the GDELT macro-theme query and lands the
result in S3. Thin wiring only — the actual query/curation logic is `gdelt_macro.py`
(kept pure and unit-testable without real GCP credentials); this module is the part
that needs the real cross-cloud bridge (O1, `gdelt_bridge.py`) and real AWS S3 access.

Writes ``lambda-digests/gdelt_macro/{date}.json`` in the shared digests bucket — same
bucket/prefix *style* as the existing news digest (``lambda-digests/news/{request_id}
.json``, see ``src/ingest/lambda_port.py``), but keyed by date rather than per-invocation
request id: this source runs once/day, not once/pipeline-run, and a stable date key
makes a manual re-run idempotent (overwrite, not accumulate) instead of leaving orphan
objects behind. NOT consumed by anything yet — see O3 (#138) for wiring this into
`candidates.py`/the digest merge; until then this is inert, same as O1's bridge was
before this module existed.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

import boto3

from gdelt_macro import fetch_macro_news

DIGESTS_BUCKET_ENV = "ONCA_DIGESTS_BUCKET"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    event = event or {}
    target_date = (
        dt.date.fromisoformat(event["target_date"])
        if event.get("target_date")
        else dt.date.today() - dt.timedelta(days=1)
    )

    from google.cloud import bigquery

    project = os.environ.get("GCP_PROJECT", "")
    client = bigquery.Client(project=project)
    news = fetch_macro_news(client, target_date=target_date)

    body = {"news": news, "source": "gdelt_macro", "date": target_date.isoformat(), "count": len(news)}
    bucket = os.environ.get(DIGESTS_BUCKET_ENV, "")
    key = f"lambda-digests/gdelt_macro/{target_date.isoformat()}.json"
    if bucket:
        boto3.client("s3").put_object(
            Bucket=bucket, Key=key, Body=json.dumps(body).encode(), ContentType="application/json"
        )

    result = {"ok": True, "date": target_date.isoformat(), "count": len(news), "bucket": bucket, "key": key}
    print(json.dumps(result))
    return result
