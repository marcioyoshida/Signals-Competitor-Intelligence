import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import receita_bigquery as rbq  # noqa: E402


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class FakeBigQueryClient:
    """Captures the SQL/params it's called with and returns pre-seeded rows —
    enough surface to test `receita_bigquery.py`'s shaping logic entirely
    offline, no real GCP credentials or network call involved."""

    def __init__(self, rows):
        self._rows = rows
        self.queries: list[tuple[str, object]] = []

    def query(self, sql, job_config=None, **_kw):
        self.queries.append((sql, job_config))
        if "GROUP BY ano, mes" in sql:  # latest_snapshot probe
            return _FakeResult([{"ano": 2026, "mes": 8}])
        return _FakeResult(self._rows)


import types  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fake_bigquery(monkeypatch):
    """Every query in the module goes through `_run`, which builds a QueryJobConfig —
    so the fake module is needed by every test, not only the fetch ones."""
    mod = types.SimpleNamespace(
        QueryJobConfig=lambda **kw: kw,
        ScalarQueryParameter=lambda *a: a,
        ArrayQueryParameter=lambda *a: a,
    )
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", mod)
    monkeypatch.setitem(sys.modules, "google.cloud", types.SimpleNamespace(bigquery=mod))
    return mod


# --- _row_to_candidate --------------------------------------------------------

def test_row_prefers_nome_fantasia_over_razao_social():
    row = {"cnpj_basico": "12345678", "nome_fantasia": "Acme Bank",
           "razao_social": "ACME BANCO SA", "cnae": "6422800", "uf": "SP"}
    out = rbq._row_to_candidate(row)
    assert out["name"] == "Acme Bank"


def test_row_falls_back_to_razao_social_when_nome_fantasia_is_blank():
    # The fix over the old shard-streaming path (#123): nome_fantasia is
    # commonly empty; razao_social is not, so a candidate shouldn't be lost.
    row = {"cnpj_basico": "12345678", "nome_fantasia": "", "razao_social": "ACME BANCO SA",
           "cnae": "6422800", "uf": "SP"}
    out = rbq._row_to_candidate(row)
    assert out["name"] == "ACME BANCO SA"


def test_row_with_no_name_at_all_still_shapes_but_with_none():
    row = {"cnpj_basico": "12345678", "nome_fantasia": "", "razao_social": "",
           "cnae": "6422800", "uf": "SP"}
    out = rbq._row_to_candidate(row)
    assert out["name"] is None


def test_row_maps_cnae_to_industry_via_receita_bulk_mapping():
    row = {"cnpj_basico": "12345678", "nome_fantasia": "X", "razao_social": "",
           "cnae": "6422800", "uf": "SP"}
    out = rbq._row_to_candidate(row)
    assert out["industry"] == "banking"


def test_row_shape_matches_receita_bulk_parse_estabelecimentos():
    # propose_candidates is the SAME consumer either way — the shape contract
    # must match exactly.
    row = {"cnpj_basico": "12345678", "nome_fantasia": "X", "razao_social": "",
           "cnae": "6422800", "uf": "SP"}
    out = rbq._row_to_candidate(row)
    assert set(out) == {
        "id", "source", "kind", "cnpj", "name", "cnae", "industry", "uf",
        "registry", "discovery_source",
    }
    assert out["id"] == "receita:12345678"
    assert out["source"] == "Receita-CNPJ-bulk"
    assert out["kind"] == "competitor"


def test_row_with_empty_cnpj_basico_is_dropped():
    row = {"cnpj_basico": "", "nome_fantasia": "X", "razao_social": "", "cnae": "6422800"}
    assert rbq._row_to_candidate(row) is None


def test_row_with_non_fs_cnae_is_dropped():
    row = {"cnpj_basico": "12345678", "nome_fantasia": "X", "razao_social": "",
           "cnae": "4711301"}  # supermercados — not FS
    assert rbq._row_to_candidate(row) is None


# --- fetch_fs_candidates --------------------------------------------------------

def test_fetch_fs_candidates_filters_and_shapes_query_results(monkeypatch):
    rows = [
        {"cnpj_basico": "11111111", "nome_fantasia": "Banco X", "razao_social": "",
         "cnae": "6422800", "uf": "SP"},
        {"cnpj_basico": "22222222", "nome_fantasia": "Mercado Y", "razao_social": "",
         "cnae": "4711301", "uf": "RJ"},  # non-FS — must be excluded even if the WHERE clause missed it
    ]
    fake_client = FakeBigQueryClient(rows)

    import types
    fake_bigquery_module = types.SimpleNamespace(
        QueryJobConfig=lambda **kw: kw,
        ScalarQueryParameter=lambda *a: a,
        ArrayQueryParameter=lambda *a: a,
    )
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake_bigquery_module)
    monkeypatch.setitem(sys.modules, "google.cloud", types.SimpleNamespace(bigquery=fake_bigquery_module))

    out = rbq.fetch_fs_candidates(fake_client)

    assert len(out) == 1
    assert out[0]["cnpj"] == "11111111"
    assert fake_client.queries  # the query was actually issued


def test_introspect_schema_runs_a_fixed_query_not_arbitrary_sql():
    fake_client = FakeBigQueryClient(
        [{"table_name": "estabelecimentos", "column_name": "cnpj_basico", "data_type": "STRING"}]
    )
    out = rbq.introspect_schema(fake_client)
    assert out == [{"table_name": "estabelecimentos", "column_name": "cnpj_basico", "data_type": "STRING"}]
    sql = fake_client.queries[0][0]
    assert "INFORMATION_SCHEMA" in sql
    assert rbq.DATASET in sql


def test_fetch_fs_candidates_pushes_known_roots_exclusion_into_the_sql(monkeypatch):
    fake_client = FakeBigQueryClient([])
    import types
    fake_bigquery_module = types.SimpleNamespace(
        QueryJobConfig=lambda **kw: kw,
        ScalarQueryParameter=lambda *a: a,
        ArrayQueryParameter=lambda *a: a,
    )
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake_bigquery_module)
    monkeypatch.setitem(sys.modules, "google.cloud", types.SimpleNamespace(bigquery=fake_bigquery_module))

    rbq.fetch_fs_candidates(fake_client, known_roots=["11111111", "22222222"])

    sql, job_config = fake_client.queries[-1]
    assert "NOT IN UNNEST" in sql
    assert any(p[0] == "known_roots" for p in job_config["query_parameters"])


def test_fetch_fs_candidates_omits_exclusion_when_no_known_roots(monkeypatch):
    fake_client = FakeBigQueryClient([])
    import types
    fake_bigquery_module = types.SimpleNamespace(
        QueryJobConfig=lambda **kw: kw,
        ScalarQueryParameter=lambda *a: a,
        ArrayQueryParameter=lambda *a: a,
    )
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake_bigquery_module)
    monkeypatch.setitem(sys.modules, "google.cloud", types.SimpleNamespace(bigquery=fake_bigquery_module))

    rbq.fetch_fs_candidates(fake_client)

    sql, job_config = fake_client.queries[-1]
    assert "NOT IN UNNEST" not in sql
    assert f"LIMIT {rbq.DEFAULT_FETCH_LIMIT}" in sql


def test_query_pins_both_tables_by_parameter_not_subquery():
    # #156: BigQuery prunes only on a direct column-vs-parameter comparison — no
    # `MAX(...)` subquery and no expression wrapped around ano/mes.
    sql = rbq._build_query(exclude_known=True, limit=10)
    assert "MAX(" not in sql and "ano * 100" not in sql
    assert "ano = @est_ano AND mes = @est_mes" in sql
    assert "ano = @emp_ano AND mes = @emp_mes" in sql
    assert "SELECT *" not in sql  # bytes are billed per column read
    assert "GROUP BY" in sql  # empresas collapsed to one row per root
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY est.cnpj_basico) = 1" in sql
    assert sql.index("QUALIFY") < sql.index("LIMIT")


def test_fetch_passes_each_tables_own_snapshot_as_params():
    client = FakeBigQueryClient([])
    rbq.fetch_fs_candidates(client, known_roots=["1"])
    names = {p[0]: p[2] for p in client.queries[-1][1]["query_parameters"]}
    assert names["est_ano"] == 2026 and names["est_mes"] == 8
    assert names["emp_ano"] == 2026 and names["emp_mes"] == 8


def test_every_query_carries_the_byte_cap(monkeypatch):
    monkeypatch.setenv("ONCA_RECEITA_BQ_MAX_BYTES", "12345")
    client = FakeBigQueryClient([])
    rbq.fetch_fs_candidates(client, known_roots=["1"])
    rbq.introspect_schema(client)
    rbq.sample_rows(client, table="empresas", limit=2)
    rbq.recheck_roots(client, ["1"])
    assert client.queries and all(cfg["maximum_bytes_billed"] == 12345 for _, cfg in client.queries)


def test_sample_rows_is_pinned_to_the_latest_snapshot():
    client = FakeBigQueryClient([])
    rbq.sample_rows(client, table="estabelecimentos", limit=2)
    assert "ano = @s_ano AND mes = @s_mes" in client.queries[-1][0]


def test_byte_cap_hit_raises_a_distinct_error():
    class CapClient(FakeBigQueryClient):
        def query(self, sql, job_config=None, **_kw):
            raise RuntimeError("400 Query exceeded limit for bytes billed: 12345. bytesBilledLimitExceeded")
    with pytest.raises(rbq.ByteCapExceeded):
        rbq.introspect_schema(CapClient([]))


def test_stale_proposals_flags_closed_missing_and_non_fs():
    pending = [{"review_id": f"r{i}", "proposed": f"N{i}", "payload": {"cnpj": root}}
               for i, root in enumerate(["11111111", "22222222", "33333333", "44444444"])]
    current = {"11111111": {"situacao": "2", "cnae": "6422800"},   # still fine
               "22222222": {"situacao": "8", "cnae": "6422800"},   # baixada
               "44444444": {"situacao": "2", "cnae": "4711301"}}   # left FS
    out = {s["cnpj"]: s["reason"] for s in rbq.stale_proposals(pending, current)}
    assert out == {"22222222": "not_active", "33333333": "no_head_office_in_latest",
                   "44444444": "cnae_not_fs"}


def test_active_situacao_matches_the_live_leading_zero_stripped_encoding():
    # Confirmed live 2026-09-23 via sample_rows against the real BigQuery table:
    # basedosdados stores situacao_cadastral as '2', not the raw dump's '02' —
    # an unverified '02' guess would have silently matched zero rows forever.
    assert rbq._ACTIVE_SITUACAO == "2"


def test_sample_rows_targets_the_requested_table():
    fake_client = FakeBigQueryClient([{"cnpj_basico": "1"}])
    rbq.sample_rows(fake_client, table="empresas", limit=3)
    sql = fake_client.queries[-1][0]
    assert f"{rbq.DATASET}.empresas" in sql
    assert "LIMIT 3" in sql


def test_snapshot_key_is_stable_and_readable():
    assert rbq.snapshot_key({"empresas": (2026, 1), "estabelecimentos": (2026, 8)}) == "est=2026-08;emp=2026-01"


def test_fetch_reuses_given_snapshots_without_probing():
    client = FakeBigQueryClient([])
    rbq.fetch_fs_candidates(client, known_roots=["1"],
                            snapshots={"estabelecimentos": (2025, 12), "empresas": (2025, 11)})
    assert len(client.queries) == 1
    names = {p[0]: p[2] for p in client.queries[-1][1]["query_parameters"]}
    assert (names["est_mes"], names["emp_mes"]) == (12, 11)


def test_discover_skips_when_snapshot_unchanged(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "ingest"))
    import receita_bigquery_handler as h
    client = FakeBigQueryClient([])
    monkeypatch.setattr(h, "_read_marker", lambda: "est=2026-08;emp=2026-08")
    out = h._dispatch("discover", {}, client)
    assert out["skipped"] == "snapshot_unchanged"
    assert all("GROUP BY ano, mes" in q for q, _ in client.queries)  # probe only


def test_table_review_sets_aside_and_untable_restores():
    from src.synth import entity_registry as er

    class T:
        def __init__(self): self.d = {"REVIEW#r1": {"pk": "REVIEW#r1", "status": "pending"}}
        def get_item(self, Key): return {"Item": dict(self.d[Key["pk"]])} if Key["pk"] in self.d else {}
        def put_item(self, Item): self.d[Item["pk"]] = Item
    t = T()
    assert er.table_review("r1", "receita recheck not_active", table=t)["status"] == "tabled"
    assert er.table_review("r1", "again", table=t) is None  # only from pending
    assert er.untable_review("r1", table=t)["status"] == "pending"
