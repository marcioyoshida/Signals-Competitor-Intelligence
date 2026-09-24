"""#104 (#14 Stage 2) — Receita CNPJ candidate discovery via a public BigQuery mirror,
replacing the live-shard-streaming approach that turned out not viable on a shared
Lambda (`receita_bulk.fetch_shard`: measured 885s against a 900s ceiling live). With
`ONCA_INGEST_RECEITA_BULK=true` already deployed at a 240s per-source budget, every
run was almost certainly hitting the deadline mid-download and returning `[]` —
"enabled" but silently producing zero candidates, not a hypothetical risk.

Base dos Dados (basedosdados.org, an established Brazilian open-data NGO) mirrors the
Receita Federal CNPJ "Estabelecimentos"/"Empresas" dumps into PUBLIC BigQuery tables.
One CNAE-filtered SQL query returns exactly the candidate rows Onça needs in seconds,
at zero query cost under BigQuery's 1TB/month free tier — no streaming or decompressing
a 300MB+ zip inside a time-budgeted Lambda at all.

Reuses the SAME cross-cloud credential bridge O1 (#136) already built and live-verified
for GDELT (`src/ingest/gdelt_bridge.py`, `infra/gdelt_bridge.py`'s `OncaGdeltBridgeRole`)
— a public dataset in a DIFFERENT GCP project needs only `roles/bigquery.jobUser` on
OUR OWN billing project (already granted for GDELT), never a new GCP-side trust/grant.
This module is pure query/shaping logic (a `client` is always passed in, so tests run
offline against a fake); `receita_bigquery_handler.py` is the thin Lambda wiring that
needs the real bridge.

**Column names are the one thing this module cannot get right without a live query**
— `basedosdados.br_me_cnpj`'s exact schema wasn't independently verifiable before this
was deployed (only fragments were confirmable via search). `introspect_schema()` exists
for exactly that: run it once against the live bridge (`receita_bigquery_handler.py`'s
`mode="introspect"`) to confirm/correct `_ESTABELECIMENTOS_COLUMNS`/`_EMPRESAS_COLUMNS`
below before trusting `fetch_fs_candidates`'s output.

Output rows are shaped IDENTICALLY to `receita_bulk.parse_estabelecimentos`'s —
`{id, source, kind, cnpj, name, cnae, industry, uf, registry, discovery_source}` — so
`receita_bulk.propose_candidates` (registry dedup + propose_review, unchanged) is the
SAME consumer either way; only how the FS-CNAE rows are obtained changed.

**Bonus fix over the old shard-streaming path** (closes #123's "no razão-social
fallback" gap as a side effect, for free): this query LEFT JOINs `empresas` for razão
social, so a CNPJ whose `nome_fantasia` is empty (the common case — `nome_fantasia` is
optional in the Estabelecimentos layout, `razao_social` is not) still gets a usable
name instead of being silently dropped by `propose_candidates`'s `no_name` skip.
"""
from __future__ import annotations

import os
from typing import Any

from src.ingest.receita_bulk import FS_CNAE_DIVISIONS, cnae_to_industry, is_fs_cnae

DATASET = "basedosdados.br_me_cnpj"

# Best-effort column names as of 2026-09-23, assembled from public examples/issues —
# NOT independently confirmed against a live schema dump before first deploy. Correct
# these against `introspect_schema()`'s real output before trusting `fetch_fs_candidates`.
_ESTABELECIMENTOS_COLUMNS = {
    "cnpj_basico": "cnpj_basico",
    "cnae": "cnae_fiscal_principal",
    "situacao": "situacao_cadastral",
    "matriz_filial": "identificador_matriz_filial",
    "nome_fantasia": "nome_fantasia",
    "uf": "sigla_uf",
}
_EMPRESAS_COLUMNS = {
    "cnpj_basico": "cnpj_basico",
    "razao_social": "razao_social",
}

# situação cadastral 2 = ativa (Receita's own code table; the RAW dump pads this
# '02', but basedosdados strips the leading zero — confirmed live 2026-09-23 via
# `sample_rows`, NOT the value `receita_bulk.parse_estabelecimentos` matches on;
# an unverified '02' guess here would have silently matched zero rows forever,
# which is exactly why `introspect_schema`/`sample_rows` exist as a pre-flight
# step). '1' = matriz (head office, not a branch) — same semantic filter
# `receita_bulk.parse_estabelecimentos` applies, just a different literal encoding.
_ACTIVE_SITUACAO = "2"
_MATRIZ_FLAG = "1"


# #156 — hard ceiling on bytes billed for EVERY job this module issues. Onça and Anteater
# bill to the same GCP project and share one 1 TB/month free tier; a regression (lost
# snapshot pin, schema change) must FAIL the job, not silently spend it. Env-overridable.
DEFAULT_MAX_BYTES = 50 * 1024**3

# Bytes billed by this module's jobs in the current process — the handler reports the
# delta per invocation, so a cost change is visible in every run's result, not inferred.
BYTES_BILLED = {"total": 0}


class ByteCapExceeded(RuntimeError):
    """A job hit `maximum_bytes_billed` — a cost regression, not a generic failure."""


def _max_bytes() -> int:
    return int(os.environ.get("ONCA_RECEITA_BQ_MAX_BYTES") or DEFAULT_MAX_BYTES)


def _run(client: Any, sql: str, params: list[Any] | None = None, *,
         use_cache: bool = True) -> list[dict[str, Any]]:
    from google.cloud import bigquery

    job_config = bigquery.QueryJobConfig(query_parameters=list(params or []),
                                         maximum_bytes_billed=_max_bytes(),
                                         use_query_cache=use_cache)
    try:
        # job_id_prefix forces a REAL job: the jobless fast path BigQuery otherwise picks
        # returns rows with no statistics at all, so bytes billed read as unknown (live).
        job = client.query(sql, job_config=job_config, job_id_prefix="onca_receita_")
        rows = [dict(r) for r in job.result()]
    except Exception as exc:
        if "bytesBilledLimitExceeded" in str(exc) or "bytes billed" in str(exc).lower():
            raise ByteCapExceeded(str(exc)[:500]) from exc
        raise
    # Statistics are not always populated by result() alone (first live run reported 0
    # for every job) — reload once so the per-run cost read is real, not a silent zero.
    if getattr(job, "total_bytes_billed", None) is None and hasattr(job, "reload"):
        try:
            job.reload()
        except Exception as exc:  # pragma: no cover - best-effort telemetry
            print(f"receita_bigquery: job reload failed: {exc}")
    # None = UNKNOWN (a jobless fast-path query carries no statistics — seen live), never 0.
    billed = getattr(job, "total_bytes_billed", None)
    if billed is None:
        BYTES_BILLED["unknown"] = BYTES_BILLED.get("unknown", 0) + 1
    else:
        billed = int(billed)
        BYTES_BILLED["total"] += billed
    BYTES_BILLED.setdefault("jobs", []).append({
        "sql": " ".join(sql.split())[:60], "billed": billed,
        "processed": getattr(job, "total_bytes_processed", None),
        "cache_hit": getattr(job, "cache_hit", None),
    })
    return rows


def latest_snapshot(client: Any, table: str) -> tuple[int, int]:
    """(ano, mes) of the newest snapshot — reads only the two INT64 partition-candidate
    columns. #156: resolved FIRST and then passed as parameters, because BigQuery only
    prunes on a direct column-vs-constant/parameter comparison; the earlier
    `(ano*100+mes) = (SELECT MAX(...))` form wraps the column AND uses a subquery."""
    if table not in ("estabelecimentos", "empresas"):
        raise ValueError(table)
    rows = _run(client, f"SELECT ano, mes FROM `{DATASET}.{table}` "
                        "GROUP BY ano, mes ORDER BY ano DESC, mes DESC LIMIT 1")
    return int(rows[0]["ano"]), int(rows[0]["mes"])


def estimate_bytes(client: Any, *, known_roots: Any = ("00000000",),
                   limit: int | None = None) -> dict[str, Any]:
    """#156 — measure whether the (ano, mes) parameters actually prune, instead of assuming.

    Runs the REAL discover query once with the result cache OFF (bytes billed are the scan
    cost, independent of LIMIT) and sets it against each table's total stored size from
    `__TABLES__` metadata. Pinned bytes ≪ table size ⇒ pruning works. Dry runs were tried
    first and returned no statistics through this client path (live, 2026-09-24).
    Still under the byte cap; read-only, no registry writes."""
    from google.cloud import bigquery

    est, emp = latest_snapshot(client, "estabelecimentos"), latest_snapshot(client, "empresas")
    params = [
        bigquery.ScalarQueryParameter("active_situacao", "STRING", _ACTIVE_SITUACAO),
        bigquery.ScalarQueryParameter("matriz_flag", "STRING", _MATRIZ_FLAG),
        bigquery.ArrayQueryParameter("cnae_divisions", "STRING", list(FS_CNAE_DIVISIONS)),
        bigquery.ArrayQueryParameter("known_roots", "STRING", list(known_roots)),
        *_snapshot_params("est", est), *_snapshot_params("emp", emp),
    ]
    sizes = _run(client, f"SELECT table_id, size_bytes, row_count FROM `{DATASET}.__TABLES__` "
                         "WHERE table_id IN ('estabelecimentos', 'empresas')")
    n_before = len(BYTES_BILLED.get("jobs") or [])
    _run(client, _build_query(exclude_known=True, limit=limit or DEFAULT_FETCH_LIMIT), params,
         use_cache=False)
    discover = (BYTES_BILLED.get("jobs") or [])[n_before:]
    return {
        "snapshot_est": list(est), "snapshot_emp": list(emp),
        "table_sizes": {r["table_id"]: {"size_bytes": r["size_bytes"], "rows": r["row_count"]}
                        for r in sizes},
        "discover_bytes_billed": discover[-1]["billed"] if discover else None,
        "max_bytes_cap": _max_bytes(),
    }


def _snapshot_params(prefix: str, snap: tuple[int, int]) -> list[Any]:
    from google.cloud import bigquery

    return [bigquery.ScalarQueryParameter(f"{prefix}_ano", "INT64", snap[0]),
            bigquery.ScalarQueryParameter(f"{prefix}_mes", "INT64", snap[1])]


def introspect_schema(client: Any) -> list[dict[str, str]]:
    """Run a FIXED schema-introspection query (never arbitrary SQL) against the two
    tables `fetch_fs_candidates` depends on. Returns `[{table_name, column_name,
    data_type}, ...]` — call this once against the live bridge before trusting the
    main query, and whenever basedosdados might have changed the schema."""
    query = f"""
        SELECT table_name, column_name, data_type
        FROM `{DATASET}.INFORMATION_SCHEMA.COLUMNS`
        WHERE table_name IN ('estabelecimentos', 'empresas')
        ORDER BY table_name, ordinal_position
    """
    return _run(client, query)


def sample_rows(client: Any, *, table: str = "estabelecimentos", limit: int = 5) -> list[dict[str, Any]]:
    """A FIXED `SELECT * LIMIT n` probe (never arbitrary SQL) — schema introspection
    tells you column NAMES/types, not whether `situacao_cadastral` is coded `'02'` or
    already translated to `'Ativa'` by basedosdados' own "traduzido" convention. Run
    this before trusting `_ACTIVE_SITUACAO`/`_MATRIZ_FLAG`'s literal values.

    #156: `LIMIT` does not reduce bytes billed — an unpinned `SELECT *` bills every
    column of every month. Pinned to the latest snapshot, and still under the byte cap."""
    snap = latest_snapshot(client, table)
    query = (f"SELECT * FROM `{DATASET}.{table}` "
             f"WHERE ano = @s_ano AND mes = @s_mes LIMIT {int(limit)}")
    return _run(client, query, _snapshot_params("s", snap))


def _build_query(*, exclude_known: bool, limit: int) -> str:
    e = _ESTABELECIMENTOS_COLUMNS
    m = _EMPRESAS_COLUMNS
    exclusion = (
        f"AND est.{e['cnpj_basico']} NOT IN UNNEST(@known_roots)\n          " if exclude_known else ""
    )
    # Both tables are a MONTHLY HISTORY keyed by (ano, mes), not a current snapshot.
    # Unpinned, a company appears once per month, passes the "ativa" filter on a month
    # it has since left, and the empresas join fans out per month. Each table is pinned
    # to ITS OWN latest snapshot (they can publish on different months), and the output
    # is still deduped to one row per root as a guard against intra-month duplicates.
    # #156: pinned by PARAMETER (resolved by `latest_snapshot` first) so BigQuery can
    # prune; SELECT only the columns used, since bytes billed are per column read.
    return f"""
        WITH est_latest AS (
          SELECT {', '.join(sorted(set(e.values())))}
          FROM `{DATASET}.estabelecimentos`
          WHERE ano = @est_ano AND mes = @est_mes
        ),
        emp_latest AS (
          SELECT {m['cnpj_basico']} AS cnpj_basico, ANY_VALUE({m['razao_social']}) AS razao_social
          FROM `{DATASET}.empresas`
          WHERE ano = @emp_ano AND mes = @emp_mes
          GROUP BY {m['cnpj_basico']}
        )
        SELECT
          est.{e['cnpj_basico']} AS cnpj_basico,
          est.{e['nome_fantasia']} AS nome_fantasia,
          emp.razao_social AS razao_social,
          est.{e['cnae']} AS cnae,
          est.{e['uf']} AS uf
        FROM est_latest AS est
        LEFT JOIN emp_latest AS emp
          ON est.{e['cnpj_basico']} = emp.cnpj_basico
        WHERE est.{e['situacao']} = @active_situacao
          AND est.{e['matriz_filial']} = @matriz_flag
          AND SUBSTR(CAST(est.{e['cnae']} AS STRING), 1, 2) IN UNNEST(@cnae_divisions)
          {exclusion}QUALIFY ROW_NUMBER() OVER (PARTITION BY est.{e['cnpj_basico']}) = 1
        LIMIT {int(limit)}
    """


def recheck_roots(client: Any, roots: list[str]) -> dict[str, dict[str, Any]]:
    """Current state of the given cnpj roots' HEAD OFFICE in the latest snapshot:
    ``{root: {situacao, cnae}}``. A root absent from the result has no head-office row
    in the latest month. Read-only — used to audit proposals queued by the pre-fix,
    snapshot-unpinned query (2026-09-23)."""
    from google.cloud import bigquery

    e = _ESTABELECIMENTOS_COLUMNS
    sql = f"""
        SELECT {e['cnpj_basico']} AS cnpj_basico, ANY_VALUE({e['situacao']}) AS situacao,
               ANY_VALUE({e['cnae']}) AS cnae
        FROM `{DATASET}.estabelecimentos`
        WHERE ano = @est_ano AND mes = @est_mes
          AND {e['matriz_filial']} = @matriz_flag
          AND {e['cnpj_basico']} IN UNNEST(@roots)
        GROUP BY {e['cnpj_basico']}
    """
    params = [
        bigquery.ScalarQueryParameter("matriz_flag", "STRING", _MATRIZ_FLAG),
        bigquery.ArrayQueryParameter("roots", "STRING", list(roots)),
        *_snapshot_params("est", latest_snapshot(client, "estabelecimentos")),
    ]
    return {str(r["cnpj_basico"]): {"situacao": str(r["situacao"] or ""), "cnae": str(r["cnae"] or "")}
            for r in _run(client, sql, params)}


def stale_proposals(pending: list[dict[str, Any]], current: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Pending Receita proposals that no longer qualify against ``current`` (from
    ``recheck_roots``): head office gone, no longer ``ativa``, or CNAE left FS."""
    out = []
    for p in pending:
        root = str((p.get("payload") or {}).get("cnpj") or "")
        cur = current.get(root)
        why = ("no_head_office_in_latest" if cur is None
               else "not_active" if cur["situacao"] != _ACTIVE_SITUACAO
               else "cnae_not_fs" if not is_fs_cnae(cur["cnae"]) else None)
        if why:
            out.append({"review_id": p.get("review_id"), "cnpj": root, "name": p.get("proposed"),
                        "reason": why, **({"situacao": cur["situacao"]} if cur else {})})
    return out


def _row_to_candidate(row: Any) -> dict[str, Any] | None:
    cnpj_basico = str(row.get("cnpj_basico") or "").strip()
    if not cnpj_basico:
        return None
    cnae = str(row.get("cnae") or "").strip()
    if not is_fs_cnae(cnae):
        return None
    name = (str(row.get("nome_fantasia") or "").strip()
            or str(row.get("razao_social") or "").strip() or None)
    uf = str(row.get("uf") or "").strip() or None
    return {
        "id": f"receita:{cnpj_basico}",
        "source": "Receita-CNPJ-bulk",
        "kind": "competitor",
        # cnpj_basico is the registry's dedup key (propose_candidates keys on the
        # 8-digit root); the full 14-digit CNPJ needs ORDEM/DV, which this query
        # doesn't fetch (propose_candidates never uses them — only the root).
        "cnpj": cnpj_basico,
        "name": name,
        "cnae": cnae,
        "industry": cnae_to_industry(cnae),
        "uf": uf,
        "registry": "receita_estabelecimentos_bigquery",
        "discovery_source": "receita_bigquery",
    }


# A hard cap on rows FETCHED (not just proposed) — live-verified 2026-09-23: an
# unfiltered query against every active FS-CNAE head office in Brazil timed out
# a 300s/512MB Lambda invocation before it could even finish materializing the
# result set (Status: timeout, Max Memory Used: 512MB — memory-bound, not just
# slow). Passing `known_roots` (below) makes a small daily LIMIT converge to
# full coverage over repeated runs instead of re-fetching the same universe
# every time, the same "don't do it all in one run" idea `receita_bulk`'s old
# day-of-year shard rotation used, just driven by the registry's own state
# instead of an arbitrary shard index.
DEFAULT_FETCH_LIMIT = 20_000


def fetch_fs_candidates(
    client: Any, *, cnae_divisions: tuple[str, ...] = FS_CNAE_DIVISIONS,
    known_roots: Any = None, limit: int = DEFAULT_FETCH_LIMIT,
) -> list[dict[str, Any]]:
    """Query the live BigQuery mirror for active, head-office, FS-CNAE establishments
    and shape the results into `receita_bulk.propose_candidates`-compatible rows.

    `known_roots` (any iterable of 8-digit cnpj roots — pass
    `entity_registry.load_cnpj_root_map(...)`'s keys) is pushed into the SQL as a
    `NOT IN UNNEST(...)` exclusion, so a already-registered CNPJ never even crosses
    the wire — `limit` then bounds only the genuinely NEW candidates fetched per
    run, not the whole FS-CNAE universe. Without `known_roots`, `limit` still
    applies (server-side row cap), but the same top-N would repeat every run.
    """
    from google.cloud import bigquery

    params = [
        bigquery.ScalarQueryParameter("active_situacao", "STRING", _ACTIVE_SITUACAO),
        bigquery.ScalarQueryParameter("matriz_flag", "STRING", _MATRIZ_FLAG),
        bigquery.ArrayQueryParameter("cnae_divisions", "STRING", list(cnae_divisions)),
    ]
    exclude_known = known_roots is not None
    if exclude_known:
        params.append(bigquery.ArrayQueryParameter("known_roots", "STRING", list(known_roots)))
    params += _snapshot_params("est", latest_snapshot(client, "estabelecimentos"))
    params += _snapshot_params("emp", latest_snapshot(client, "empresas"))
    rows = _run(client, _build_query(exclude_known=exclude_known, limit=limit), params)
    out: list[dict[str, Any]] = []
    for row in rows:
        candidate = _row_to_candidate(dict(row))
        if candidate is not None:
            out.append(candidate)
    return out
