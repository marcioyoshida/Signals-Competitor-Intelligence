"""#104 — Lambda entry point for the BigQuery-backed Receita CNAE discovery
(`receita_bigquery.py`). Thin wiring only, mirroring `gdelt_macro_handler.py`'s split:
the query/shaping logic is pure and unit-testable offline; this module is the part
that needs the real cross-cloud bridge (O1, `gdelt_bridge.py`) and real AWS access
(DynamoDB registry writes via `receita_bulk.propose_candidates`).

Dispatch is by a FIXED `mode` from the event — never arbitrary SQL or a caller-chosen
table name, even though this Lambda has no public trigger (direct-invoke only, same
IAM-gated model as every other bridge function in this stack):
  - `mode: "introspect"` (default on first deploy, before the real query is trusted) —
    runs `receita_bigquery.introspect_schema` + `sample_rows` against both tables and
    returns the raw result, no registry writes. Use this to confirm/correct
    `_ESTABELECIMENTOS_COLUMNS`/`_ACTIVE_SITUACAO`/`_MATRIZ_FLAG` before switching modes.
  - `mode: "discover"` (the real path) — runs `fetch_fs_candidates` then
    `receita_bulk.propose_candidates`, same propose-only discipline (ADR 011 §4) as
    every other entity-discovery source; never auto-creates.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import receita_bigquery
from receita_bigquery import ByteCapExceeded, fetch_fs_candidates, introspect_schema, sample_rows


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    event = event or {}
    mode = str(event.get("mode") or "introspect")

    from google.cloud import bigquery

    client = bigquery.Client(project=os.environ.get("GCP_PROJECT", ""))
    before = receita_bigquery.BYTES_BILLED["total"]
    receita_bigquery.BYTES_BILLED["jobs"] = []
    try:
        result = _dispatch(mode, event, client)
    except ByteCapExceeded as exc:
        # #156: a cost regression must read as one, not as a generic failure.
        # BigQuery states the exact scan size ("N or higher required") — the one precise
        # cost figure available, since this path returns no job statistics.
        need = re.search(r"(\d+) or higher required", str(exc))
        result = {"ok": False, "mode": mode, "reason": "byte_cap",
                  "max_bytes": receita_bigquery._max_bytes(),
                  "required_bytes": int(need.group(1)) if need else None, "error": str(exc)[:300]}
        print(json.dumps(result))
    unknown = receita_bigquery.BYTES_BILLED.pop("unknown", 0)
    result["bytes_billed"] = None if unknown else receita_bigquery.BYTES_BILLED["total"] - before
    result["jobs"] = receita_bigquery.BYTES_BILLED["jobs"]
    print(json.dumps({"mode": mode, "bytes_billed": result["bytes_billed"]}))
    return result


def _dispatch(mode: str, event: dict[str, Any], client: Any) -> dict[str, Any]:
    if mode == "introspect":
        result = {
            "ok": True,
            "mode": mode,
            "schema": introspect_schema(client),
            "sample_estabelecimentos": sample_rows(client, table="estabelecimentos", limit=3),
            "sample_empresas": sample_rows(client, table="empresas", limit=3),
        }
        # BigQuery rows can carry `datetime.date`/`Decimal` values the Lambda
        # runtime's own response marshaller can't serialize — round-trip through
        # `default=str` (this call already needs to succeed for the print below,
        # so reuse it for the RETURN value too instead of a separate converter).
        safe_result = json.loads(json.dumps(result, default=str))
        print(json.dumps(safe_result)[:4000])
        return safe_result

    if mode == "discover":
        from src.synth import entity_registry as er
        from src.ingest import receita_bulk

        # Loaded ONCE, passed to BOTH the query (as a server-side NOT-IN exclusion
        # — 2026-09-23 live finding: fetching the whole FS-CNAE universe with no
        # exclusion timed out a 300s/512MB Lambda before it could even finish
        # materializing the result set) and propose_candidates (so it doesn't
        # re-scan the registry a second time for the same information).
        known_roots = er.load_cnpj_root_map(force=True)
        fetch_limit = int(os.environ.get("ONCA_RECEITA_BQ_FETCH_LIMIT", "0")) or None
        candidates = fetch_fs_candidates(
            client, known_roots=known_roots.keys(),
            **({"limit": fetch_limit} if fetch_limit else {}),
        )
        report = receita_bulk.propose_candidates(
            candidates,
            max_propose=int(os.environ.get("ONCA_RECEITA_BQ_MAX_PROPOSE", "200")),
            known_roots=known_roots,
        )
        result = {
            "ok": True,
            "mode": mode,
            "candidates": len(candidates),
            "already": report["already"],
            "proposed": len(report["proposed"]),
            "no_name": report["no_name"],
        }
        print(json.dumps(result))
        return result

    if mode == "estimate":
        # #156: read-only cost measurement. `max_bytes` may only LOWER the cap for this one
        # run — a pass under a tight cap is a proven upper bound on the scan, which is the
        # one cost read that works when BigQuery returns no job statistics (seen live).
        # Restored in `finally`: a warm container keeps os.environ, and a leaked low cap
        # would fail the next scheduled discover (seen live — 0.5GB carried over).
        cap, saved = int(event.get("max_bytes") or 0), os.environ.get("ONCA_RECEITA_BQ_MAX_BYTES")
        if 0 < cap < receita_bigquery._max_bytes():
            os.environ["ONCA_RECEITA_BQ_MAX_BYTES"] = str(cap)
        try:
            result = {"ok": True, "mode": mode, **receita_bigquery.estimate_bytes(client)}
        finally:
            if saved is None:
                os.environ.pop("ONCA_RECEITA_BQ_MAX_BYTES", None)
            else:
                os.environ["ONCA_RECEITA_BQ_MAX_BYTES"] = saved
        print(json.dumps(result))
        return result

    if mode == "recheck":
        # Read-only audit: which PENDING Receita proposals no longer qualify against the
        # latest snapshot? Reports only — rejecting stays a curator decision.
        from src.synth import entity_registry as er
        from receita_bigquery import recheck_roots, stale_proposals

        pending = [r for r in er.list_reviews("pending")
                   if r.get("kind") == "discovery" and r.get("reason") == "receita_cnae"]
        roots = sorted({str((r.get("payload") or {}).get("cnpj") or "") for r in pending} - {""})
        stale = stale_proposals(pending, recheck_roots(client, roots)) if roots else []
        result = {"ok": True, "mode": mode, "pending": len(pending), "stale": len(stale),
                  "stale_items": stale}
        print(json.dumps(result, default=str)[:4000])
        return json.loads(json.dumps(result, default=str))

    return {"ok": False, "error": f"unknown mode {mode!r} (expected introspect|discover|recheck|estimate)"}
