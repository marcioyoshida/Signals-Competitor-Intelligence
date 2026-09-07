"""#76 / R5 — real per-INGESTER run telemetry (reliability), the blind spot the feed-derived
lens-freshness proxy (`product_intel.source_health`) cannot see.

The lens proxy measures how fresh each lens's NARRATIVES are — but a source that runs and errors,
or fetches zero relevant docs, produces no narratives and is therefore INVISIBLE to it: a silently
broken ingester looks identical to a quiet-but-healthy one. This module captures, at ingest time
and independent of whether a source produced any narrative, whether each source RAN and SUCCEEDED
(`last_ok`, `last_error`, `runs`), so the CPO data-quality panel can flag "stale / erroring / never
ran" per source.

Flow: `_source_budget` records into the in-process `_LEDGER` for the current run; at the end of the
ingest handler `merge_and_publish` folds this run's ledger over the durable
``source_health/index.json`` store (so a source that DIDN'T run this cycle keeps its prior `last_ok`
and simply grows staler). `feed_builder` loads it into ``feed.source_runs``.
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

INDEX_KEY = "source_health/index.json"

# In-process ledger for the current run (reset at the start of each ingest handler invocation).
_LEDGER: dict[str, dict[str, Any]] = {}


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def reset() -> None:
    """Clear the per-run ledger (called at the top of the ingest handler)."""
    _LEDGER.clear()


def record(source: str, *, ok: bool, docs: int | None = None, error: str | None = None) -> None:
    """Record one source's run outcome. ``ok`` = it completed without raising; ``error`` is the
    stringified failure (budget/timeout/network) when not ok; ``docs`` is optional fetched count."""
    src = (source or "").strip() or "unknown"
    e = _LEDGER.setdefault(src, {"source": src, "runs": 0})
    e["runs"] = int(e.get("runs") or 0) + 1
    e["last_run"] = _now_iso()
    if ok:
        e["last_ok"] = e["last_run"]
        e["last_error"] = None
    else:
        e["last_error"] = (error or "erro")[:300]
    if docs is not None:
        e["docs"] = int(docs)


def ledger() -> dict[str, dict[str, Any]]:
    """A copy of the current run's ledger."""
    return {k: dict(v) for k, v in _LEDGER.items()}


def _staleness_days(last_ok: str | None, *, now: _dt.datetime | None = None) -> int | None:
    if not last_ok:
        return None
    try:
        t = _dt.datetime.fromisoformat(str(last_ok))
        n = now or _dt.datetime.now(_dt.timezone.utc)
        if t.tzinfo is None:
            t = t.replace(tzinfo=_dt.timezone.utc)
        return max(0, (n - t).days)
    except Exception:
        return None


def _band(rec: dict[str, Any], *, now: _dt.datetime | None = None) -> str:
    """Reliability band: erroring > never-ok > stale/warn/ok by days since last success."""
    if rec.get("last_error"):
        return "error"
    s = _staleness_days(rec.get("last_ok"), now=now)
    if s is None:
        return "never_ok"
    return "ok" if s <= 2 else "warn" if s <= 7 else "stale"


def merge(stored: dict[str, Any], run: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Fold this run's ledger over the durable store. A source absent from ``run`` keeps its prior
    record untouched (staleness then grows on read). Returns the merged records-by-source map."""
    out = {k: dict(v) for k, v in (stored.get("records") or {}).items()}
    for src, rec in run.items():
        prior = out.get(src) or {}
        merged = {**prior, **rec}
        merged["runs"] = int(prior.get("runs") or 0) + int(rec.get("runs") or 0)
        # never lose a real last_ok: if this run errored, keep the prior success timestamp
        if not rec.get("last_ok") and prior.get("last_ok"):
            merged["last_ok"] = prior["last_ok"]
        out[src] = merged
    return {"records": out, "updated_at": _now_iso()}


def as_rows(index: dict[str, Any], *, now: _dt.datetime | None = None) -> list[dict[str, Any]]:
    """Records → sorted rows with computed staleness + band (worst first) for the CPO panel."""
    rows = []
    for rec in (index.get("records") or {}).values():
        r = dict(rec)
        r["staleness_days"] = _staleness_days(rec.get("last_ok"), now=now)
        r["band"] = _band(rec, now=now)
        rows.append(r)
    order = {"error": 0, "never_ok": 1, "stale": 2, "warn": 3, "ok": 4}
    rows.sort(key=lambda r: (order.get(r["band"], 9), -(r.get("staleness_days") or 0)))
    return rows


def load_index(bucket: str, *, s3: Any | None = None) -> dict[str, Any]:
    import boto3

    s3 = s3 or boto3.client("s3")
    try:
        body = s3.get_object(Bucket=bucket, Key=INDEX_KEY)["Body"].read()
        data = json.loads(body)
        return data if isinstance(data, dict) else {}
    except Exception:  # pragma: no cover - first run
        return {}


def merge_and_publish(bucket: str, *, s3: Any | None = None) -> str | None:
    """Fold the current run's ledger over the stored index and persist it. Best-effort; returns the
    S3 URI or None. Reads the in-process ledger, so call once at the end of the ingest run."""
    if not bucket:
        return None
    import boto3

    s3 = s3 or boto3.client("s3")
    try:
        stored = load_index(bucket, s3=s3)
        merged = merge(stored, ledger())
        s3.put_object(
            Bucket=bucket, Key=INDEX_KEY,
            Body=json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        return f"s3://{bucket}/{INDEX_KEY}"
    except Exception as exc:  # pragma: no cover - never blocks the ingest return
        print(f"Warning: source_health publish failed: {exc}")
        return None
