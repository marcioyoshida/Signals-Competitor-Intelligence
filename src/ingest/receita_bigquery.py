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
    return [dict(row) for row in client.query(query).result()]


def sample_rows(client: Any, *, table: str = "estabelecimentos", limit: int = 5) -> list[dict[str, Any]]:
    """A FIXED `SELECT * LIMIT n` probe (never arbitrary SQL) — schema introspection
    tells you column NAMES/types, not whether `situacao_cadastral` is coded `'02'` or
    already translated to `'Ativa'` by basedosdados' own "traduzido" convention. Run
    this before trusting `_ACTIVE_SITUACAO`/`_MATRIZ_FLAG`'s literal values."""
    query = f"SELECT * FROM `{DATASET}.{table}` LIMIT {int(limit)}"
    return [dict(row) for row in client.query(query).result()]


def _build_query(*, exclude_known: bool, limit: int) -> str:
    e = _ESTABELECIMENTOS_COLUMNS
    m = _EMPRESAS_COLUMNS
    exclusion = (
        f"AND est.{e['cnpj_basico']} NOT IN UNNEST(@known_roots)\n          " if exclude_known else ""
    )
    return f"""
        SELECT
          est.{e['cnpj_basico']} AS cnpj_basico,
          est.{e['nome_fantasia']} AS nome_fantasia,
          emp.{m['razao_social']} AS razao_social,
          est.{e['cnae']} AS cnae,
          est.{e['uf']} AS uf
        FROM `{DATASET}.estabelecimentos` AS est
        LEFT JOIN `{DATASET}.empresas` AS emp
          ON est.{e['cnpj_basico']} = emp.{m['cnpj_basico']}
        WHERE est.{e['situacao']} = @active_situacao
          AND est.{e['matriz_filial']} = @matriz_flag
          AND SUBSTR(CAST(est.{e['cnae']} AS STRING), 1, 2) IN UNNEST(@cnae_divisions)
          {exclusion}LIMIT {int(limit)}
    """


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
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    rows = client.query(_build_query(exclude_known=exclude_known, limit=limit), job_config=job_config).result()
    out: list[dict[str, Any]] = []
    for row in rows:
        candidate = _row_to_candidate(dict(row))
        if candidate is not None:
            out.append(candidate)
    return out
