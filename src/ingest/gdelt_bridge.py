"""O1 (#136): cross-cloud AWS→GCP BigQuery smoke test for the GDELT macro-theme spike.

Onça's pipeline is 100% AWS; GDELT lives in BigQuery (GCP). This module proves the
credential path works — GCP workload identity federation, so an AWS Lambda execution
role can obtain a short-lived GCP token without any long-lived secret ever being
stored in either cloud. The `external_account` JSON config this relies on (bundled
at ``GOOGLE_APPLICATION_CREDENTIALS``) is not itself a secret: it only describes
*how* to fetch a token (which AWS role, which GCP workload identity pool/provider,
which service account to impersonate) — google-auth signs a GetCallerIdentity
request with the Lambda's own AWS credentials to prove the role's identity to GCP.

Scope: this is O1's smoke test only. It runs one ``dry_run`` query (zero cost, no
data movement) to prove the round trip. The real theme-filtered ingestion query is
O2 (#137); this module is not meant to grow into that pipeline in place.
"""
from __future__ import annotations

import json
import os
from typing import Any


def check_bridge(event: dict[str, Any], context: Any) -> dict[str, Any]:
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    project = os.environ.get("GCP_PROJECT", "")
    if not cred_path or not os.path.exists(cred_path):
        return {
            "ok": False,
            "stage": "credentials",
            "error": f"GOOGLE_APPLICATION_CREDENTIALS not set or missing: {cred_path!r}",
        }
    try:
        from google.cloud import bigquery
    except Exception as exc:  # pragma: no cover - import-time packaging error
        return {"ok": False, "stage": "import", "error": str(exc)}

    try:
        client = bigquery.Client(project=project)
        job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        query = (
            "SELECT COUNT(*) AS n FROM `gdelt-bq.gdeltv2.gkg_partitioned` "
            "WHERE _PARTITIONDATE = '2026-09-15' "
            "AND V2Themes LIKE '%ECON_INTEREST_RATES%'"
        )
        job = client.query(query, job_config=job_config)
        return {
            "ok": True,
            "stage": "dry_run",
            "total_bytes_processed": job.total_bytes_processed,
            "project": project,
        }
    except Exception as exc:
        return {"ok": False, "stage": "query", "error": str(exc)}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    result = check_bridge(event, context)
    print(json.dumps(result))
    return result
