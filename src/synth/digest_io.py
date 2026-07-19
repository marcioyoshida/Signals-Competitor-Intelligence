"""Load the latest ingest digest from S3 or a provided event body."""
from __future__ import annotations

import json
import os
from typing import Any

import boto3


def load_digest_from_event(event: dict[str, Any] | None) -> dict[str, Any] | None:
    """Prefer inline digest in the event; else fetch latest from S3."""
    event = event or {}
    if isinstance(event.get("digest"), dict):
        return event["digest"]
    body = event.get("body")
    if isinstance(body, str) and body.strip().startswith("{"):
        try:
            parsed = json.loads(body)
            if isinstance(parsed.get("digest"), dict):
                return parsed["digest"]
            # Full digest posted as body
            if "regulatory" in parsed or "source" in parsed:
                return parsed
        except json.JSONDecodeError:
            pass
    return load_latest_digest_from_s3()


def load_latest_digest_from_s3(
    bucket: str | None = None,
    prefix: str = "lambda-digests/",
) -> dict[str, Any] | None:
    bucket = bucket or os.environ.get("ONCA_DIGESTS_BUCKET")
    if not bucket:
        return None
    s3 = boto3.client("s3")
    try:
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    except Exception as exc:  # pragma: no cover
        print(f"Warning: list digests failed: {exc}")
        return None
    contents = resp.get("Contents") or []
    if not contents:
        return None
    latest = max(contents, key=lambda o: o["LastModified"])
    key = latest["Key"]
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception as exc:  # pragma: no cover
        print(f"Warning: read digest s3://{bucket}/{key} failed: {exc}")
        return None


def write_narrative(
    narrative: dict[str, Any],
    bucket: str | None = None,
    prefix: str = "narratives/",
) -> str | None:
    """Write one narrative JSON object; return S3 key or None."""
    bucket = bucket or os.environ.get("ONCA_DIGESTS_BUCKET")
    if not bucket:
        return None
    nid = narrative.get("id") or "unknown"
    date = (narrative.get("as_of") or "unknown")[:10]
    key = f"{prefix}{date}/{nid}.json"
    try:
        boto3.client("s3").put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(narrative, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        return key
    except Exception as exc:  # pragma: no cover
        print(f"Warning: write narrative failed: {exc}")
        return None
