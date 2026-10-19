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





def test_introspect_schema_runs_a_fixed_query_not_arbitrary_sql():
    fake_client = FakeBigQueryClient(
        [{"table_name": "estabelecimentos", "column_name": "cnpj_basico", "data_type": "STRING"}]
    )
    out = rbq.introspect_schema(fake_client)
    assert out == [{"table_name": "estabelecimentos", "column_name": "cnpj_basico", "data_type": "STRING"}]
    sql = fake_client.queries[0][0]
    assert "INFORMATION_SCHEMA" in sql
    assert rbq.DATASET in sql


def test_estimate_runs_the_discover_query_with_the_known_roots_exclusion():
    fake_client = FakeBigQueryClient([])
    rbq.estimate_bytes(fake_client, known_roots=["11111111", "22222222"])
    sql, job_config = fake_client.queries[-1]
    assert "NOT IN UNNEST" in sql
    assert any(p[0] == "known_roots" for p in job_config["query_parameters"])
    assert job_config["use_query_cache"] is False


def test_query_omits_exclusion_when_not_asked():
    sql = rbq._build_query(exclude_known=False, limit=rbq.DEFAULT_FETCH_LIMIT)
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


def test_estimate_passes_each_tables_own_snapshot_as_params():
    client = FakeBigQueryClient([])
    rbq.estimate_bytes(client, known_roots=["1"])
    names = {p[0]: p[2] for p in client.queries[-1][1]["query_parameters"]}
    assert names["est_ano"] == 2026 and names["est_mes"] == 8
    assert names["emp_ano"] == 2026 and names["emp_mes"] == 8


def test_every_query_carries_the_byte_cap(monkeypatch):
    monkeypatch.setenv("ONCA_RECEITA_BQ_MAX_BYTES", "12345")
    client = FakeBigQueryClient([])
    rbq.estimate_bytes(client, known_roots=["1"])
    rbq.bigquery_fingerprint(client, (2026, 1))
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




def test_legacy_discover_mode_reads_the_extract_not_bigquery(monkeypatch):
    # #227: the BigQuery discover query is retired; a stale `discover` invoke must
    # not scan BigQuery, so it routes to the S3 path (here: snapshot-gated skip).
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "ingest"))
    import receita_bigquery_handler as h
    monkeypatch.setenv("ONCA_RECEITA_EXTRACT_BUCKET", "extract-bucket")
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda svc: FakeS3()))
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", None)  # importing it would fail
    monkeypatch.setattr(h, "_read_marker", lambda: "est=2026-01;emp=2026-01")
    out = h.handler({"mode": "discover"}, None)
    assert out["mode"] == "discover_s3" and out["skipped"] == "snapshot_unchanged"


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


# --- #227: Anteater's shared extract in S3 -------------------------------------

import gzip as _gzip  # noqa: E402
import io as _io  # noqa: E402
import json as _json  # noqa: E402

_EXTRACT_HEADER = "cnpj_basico,razao_social,nome_fantasia,cnae,sigla_uf,porte,sector,products,ano,mes\n"
_EXTRACT_ROWS = [
    "00508114,I.C. HOLDINGS LTDA,,6461100,SC,1,,onca,2026,1",
    "00141155,COOP CRED LTDA,COOPCRED,6424703,RJ,5,,onca+anteater,2026,1",
    "00302225,OSMAR LTDA,ELOGRAN,4744005,PR,5,retail,anteater,2026,1",   # not Onça
    "11111111,KNOWN BANCO SA,KNOWN,6422100,SP,5,,onca,2026,1",           # already registered
    "22222222,SEGUROS SA,,6511101,SP,5,,onca,2026,1",
]


@pytest.fixture(autouse=True)
def _extract_bucket(monkeypatch):
    monkeypatch.setenv("ONCA_RECEITA_EXTRACT_BUCKET", "extract-bucket")


def test_extract_bucket_must_come_from_the_stack(monkeypatch):
    monkeypatch.delenv("ONCA_RECEITA_EXTRACT_BUCKET")
    with pytest.raises(ValueError):
        rbq.extract_marker(FakeS3())


class FakeS3:
    def __init__(self, marker=None, rows=_EXTRACT_ROWS):
        self.marker = marker or {"snapshots": {"estabelecimentos": [2026, 1], "empresas": [2026, 1]},
                                 "prefix": "receita/2026-01/", "rows": len(rows)}
        self.csv = _gzip.compress((_EXTRACT_HEADER + "\n".join(rows) + "\n").encode())
        self.keys = []

    def get_object(self, Bucket, Key):
        self.keys.append((Bucket, Key))
        if Key == "receita/_snapshot.json":
            return {"Body": _io.BytesIO(_json.dumps(self.marker).encode())}
        assert Key == self.marker["prefix"] + "estabelecimentos.csv.gz"
        return {"Body": _io.BytesIO(self.csv)}


def test_extract_candidates_keep_onca_fs_rows_minus_known_roots():
    s3 = FakeS3()
    marker = rbq.extract_marker(s3)
    out = list(rbq.candidates_from_extract(rbq.iter_extract_rows(s3, marker), ["11111111"]))
    assert [c["cnpj"] for c in out] == ["00508114", "00141155", "22222222"]
    first = out[0]
    assert first["name"] == "I.C. HOLDINGS LTDA"            # razão social when no fantasia
    assert out[1]["name"] == "COOPCRED" and out[1]["uf"] == "RJ"
    assert first["discovery_source"] == "receita_extract" and first["registry"] == "receita_extract_s3"
    # Same shape as the discover query's rows, so propose_candidates is unchanged.
    bq_shape = rbq._row_to_candidate({"cnpj_basico": "1", "cnae": "6461100", "uf": "SC"})
    assert set(first) == set(bq_shape)


def test_extract_snapshot_key_matches_the_bigquery_marker_format():
    marker = rbq.extract_marker(FakeS3())
    assert rbq.snapshot_key(rbq.extract_snapshots(marker)) == "est=2026-01;emp=2026-01"


def test_extract_marker_rejects_a_prefix_outside_receita():
    s3 = FakeS3(marker={"snapshots": {"estabelecimentos": [2026, 1]}, "prefix": "digests/"})
    with pytest.raises(ValueError):
        rbq.extract_marker(s3)


def test_extract_fingerprint_matches_the_bigquery_shape():
    s3 = FakeS3()
    fp = rbq.extract_fingerprint(rbq.iter_extract_rows(s3, rbq.extract_marker(s3)))
    assert fp == {"64": [3, 508114 + 141155 + 11111111, 508114 ^ 141155 ^ 11111111],
                  "65": [1, 22222222, 22222222]}
    client = FakeBigQueryClient([{"division": "64", "n": 3, "root_sum": 1, "root_xor": 2}])
    assert rbq.bigquery_fingerprint(client, (2026, 1)) == {"64": [3, 1, 2]}
    sql, cfg = client.queries[-1]
    assert "LIMIT" not in sql and "known_roots" not in sql  # the FULL universe
    names = {p[0]: p[2] for p in cfg["query_parameters"]}
    assert (names["est_ano"], names["est_mes"]) == (2026, 1)


def test_discover_s3_proposes_from_the_extract_without_bigquery(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "ingest"))
    import receita_bigquery_handler as h
    from src.synth import entity_registry as er

    s3, written, proposed = FakeS3(), [], []
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda svc: s3))
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", None)  # importing it would fail
    monkeypatch.setattr(h, "_read_marker", lambda: None)
    monkeypatch.setattr(h, "_write_marker", written.append)
    monkeypatch.setattr(er, "load_cnpj_root_map", lambda **kw: {"11111111": "ent-1"})
    monkeypatch.setattr(er, "propose_review", lambda **kw: proposed.append(kw["key"]) or kw["key"])
    monkeypatch.setenv("ONCA_RECEITA_BQ_MAX_PROPOSE", "2")
    out = h.handler({"mode": "discover_s3"}, None)
    assert out["ok"] and out["candidates"] == 3 and out["proposed"] == 2 and out["bytes_billed"] == 0
    assert proposed == ["receita:00508114", "receita:00141155"]
    assert written == ["est=2026-01;emp=2026-01"]


def test_discover_s3_skips_when_the_snapshot_was_already_processed(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "ingest"))
    import receita_bigquery_handler as h

    s3 = FakeS3()
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda svc: s3))
    monkeypatch.setattr(h, "_read_marker", lambda: "est=2026-01;emp=2026-01")
    out = h.handler({"mode": "discover_s3"}, None)
    assert out["skipped"] == "snapshot_unchanged"
    assert [k for _, k in s3.keys] == ["receita/_snapshot.json"]  # marker only, no CSV read
