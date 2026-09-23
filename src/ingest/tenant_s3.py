"""ADR 016 addendum (2026-09-22), Decision 4 step 2 / ADR 005 §3: the tenant
private-S3 lens.

**What this is.** A Sovereign tenant's own documents — deal memos, internal
notes, CRM exports — parked in buckets they already own, ingested exactly like
any other `src/ingest/*` source: enumerate objects, diff via
`src/diff/engine.py`'s seen-set, normalize to the same raw-doc dict shape every
other ingester produces, hand off to `raw_writer.write_raw_documents` so Stage-A
KB ingestion vectorizes them into the tenant's OWN knowledge base
(`infra/tenant_stack.py`'s `OncaTenantKnowledgeBase`). Synthesis then fuses this
private corpus with public regulatory/competitor signal — "narratives no
competitor can reproduce," per ADR 005 §3.

**What this module is NOT.** It never touches `entity_registry` or
`entities.py` — free-text entity tagging of these documents happens downstream,
through the same resolution seam (`src/synth/resolver.py`) every other source
uses, not by anything in here. This module's only job is enumerate → diff →
normalize → hand off; it has no opinion about entities at all.

**Locality, not a flag.** Which bucket gets read (`ONCA_TENANT_PRIVATE_BUCKETS`)
and which bucket gets written (`ONCA_RAW_BUCKET`) are both plain config. In a
SaaS/vendor deployment neither variable is set, so `collect()` returns `[]` and
`ingest()` no-ops — this module is inert until a tenant configures it, exactly
the same "telemetry off falls out of locality" argument the addendum's
Decision 1 makes for engagement data, applied here to inbound private data
instead of outbound telemetry.

**Deliberately out of scope for this pass** (see the ADR 005 §3 text this
implements): office-document formats (PDF/DOCX/XLSX) need real text extraction,
not a `.decode("utf-8")` — only plain-text-ish formats are read today. Not
wired into any Lambda yet; the tenant's own ingest Lambda doesn't exist until
Decision 4 step 1's stack gets one (see `infra/tenant_stack.py`'s docstring).
"""
from __future__ import annotations

import os
from typing import Any

from src.diff import engine as diff_engine

SOURCE = "tenant_s3"

# Office formats (PDF/DOCX/XLSX/...) need real extraction, not a decode — out
# of scope here (see module docstring); skip rather than ingest raw bytes.
TEXT_EXTENSIONS = (".txt", ".md", ".csv", ".json")

# One huge object must not exhaust an ingest run's time/memory budget — the
# tenant's own documents are unbounded in size, unlike the vendor's own curated
# sources, which is exactly the risk this cap exists to bound.
MAX_OBJECT_BYTES = 2_000_000


def _configured_buckets() -> list[tuple[str, str]]:
    """Parse `ONCA_TENANT_PRIVATE_BUCKETS` — comma-separated `bucket[:prefix]`
    pairs. Unset/empty means no private lens configured yet, never an error: a
    tenant that hasn't pointed this at their own documents should ingest zero
    private docs, not fail the pipeline."""
    raw = os.environ.get("ONCA_TENANT_PRIVATE_BUCKETS") or ""
    pairs: list[tuple[str, str]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        bucket, _, prefix = chunk.partition(":")
        pairs.append((bucket.strip(), prefix.strip()))
    return pairs


def _doc_id(bucket: str, key: str, etag: str) -> str:
    # The etag rides in the id so a document EDITED IN PLACE (same key, new
    # content — a corrected deal memo re-uploaded over the old one) is treated
    # as new. A private lens that silently never re-ingests a correction would
    # be a worse failure than re-ingesting once.
    return f"{bucket}/{key}#{etag}"


def _list_objects(s3: Any, bucket: str, prefix: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            if key.endswith("/"):
                continue  # a folder marker, not a document
            if not key.lower().endswith(TEXT_EXTENSIONS):
                continue
            objects.append(obj)
    return objects


def _fetch_text(s3: Any, bucket: str, key: str) -> str | None:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as exc:  # pragma: no cover - transient
        print(f"Warning: tenant_s3 failed to read s3://{bucket}/{key}: {exc}")
        return None
    if len(body) > MAX_OBJECT_BYTES:
        print(
            f"Warning: tenant_s3 skipping s3://{bucket}/{key} — "
            f"{len(body)} bytes over the {MAX_OBJECT_BYTES} cap"
        )
        return None
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return body.decode("latin-1")
        except Exception:  # pragma: no cover - defensive
            return None


def collect(*, s3: Any = None, state: Any = None, commit: bool = True) -> list[dict[str, Any]]:
    """Enumerate every configured tenant bucket/prefix, diff against the
    seen-set, and return NEW raw docs in the same shape every other
    `src/ingest/*` source produces (`id`/`source`/`kind` at minimum) — so this
    source needs no special-casing downstream, per ADR 005 §3's "treats
    configured tenant buckets/prefixes like any other source."
    """
    pairs = _configured_buckets()
    if not pairs:
        return []
    if s3 is None:
        import boto3

        s3 = boto3.client("s3")

    docs: list[dict[str, Any]] = []
    for bucket, prefix in pairs:
        for obj in _list_objects(s3, bucket, prefix):
            key = obj["Key"]
            etag = str(obj.get("ETag") or "").strip('"')
            text = _fetch_text(s3, bucket, key)
            if not text or not text.strip():
                continue
            last_modified = obj.get("LastModified")
            docs.append(
                {
                    "id": _doc_id(bucket, key, etag),
                    "source": SOURCE,
                    "kind": "tenant_private",
                    "bucket": bucket,
                    "key": key,
                    "title": key.rsplit("/", 1)[-1],
                    "text": text.strip(),
                    "date": last_modified.date().isoformat() if last_modified else None,
                    "url": f"s3://{bucket}/{key}",
                }
            )
    return diff_engine.detect_new(SOURCE, docs, state=state, commit=commit)


def ingest(*, raw_bucket: str | None = None, s3: Any = None, state: Any = None) -> list[str]:
    """Collect new tenant-private docs and write them into the tenant's own raw
    bucket via `raw_writer.write_raw_documents` — `ONCA_RAW_BUCKET`, the same
    env var every other ingester already writes through. In a tenant deployment
    this resolves to `infra/tenant_stack.py`'s `OncaTenantRawBucket`, never the
    vendor's bucket — by config, not by a mode flag (Decision 1's locality
    argument, applied to this source). Returns the S3 keys written.
    """
    raw_bucket = raw_bucket or os.environ.get("ONCA_RAW_BUCKET")
    if not raw_bucket:
        print("Warning: ONCA_RAW_BUCKET not configured — tenant_s3 lens skipped")
        return []
    if s3 is None:
        import boto3

        s3 = boto3.client("s3")
    new_docs = collect(s3=s3, state=state)
    if not new_docs:
        return []
    from src.ingest import raw_writer

    return raw_writer.write_raw_documents(raw_bucket, new_docs)
