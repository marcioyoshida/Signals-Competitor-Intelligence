"""ANS IGR — consumer reputation for saúde suplementar (#140)."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import ans_igr


def _csv(header: str, rows: list[str]) -> bytes:
    return ("\n".join([header, *rows])).encode("latin-1")


IGR = _csv(
    "REGISTRO_OPERADORA;RAZAO_SOCIAL;COBERTURA;IGR;QTD_RECLAMACOES;QTD_BENEFICIARIOS;"
    "PORTE_OPERADORA;COMPETENCIA;COMPETENCIA_BENEFICIARIO;DT_ATUALIZACAO",
    [
        # two coberturas for the same operadora, newest competência
        '"000582";"PORTO SEGURO - SEGURO SAÚDE S/A";"Assistência médica";71,80;620;863538;'
        '"Grande";"2026-08";"2026-07";"2026-09-08"',
        '"000582";"PORTO SEGURO - SEGURO SAÚDE S/A";"Exclusivamente odontológica";1,13;14;1239187;'
        '"Grande";"2026-08";"2026-07";"2026-09-08"',
        # an older competência that must be ignored
        '"000582";"PORTO SEGURO - SEGURO SAÚDE S/A";"Assistência médica";99,00;900;800000;'
        '"Grande";"2026-07";"2026-06";"2026-08-08"',
        # an operadora that is NOT in the registry
        '"999999";"OPERADORA DESCONHECIDA LTDA";"Assistência médica";500,00;300;1000;'
        '"Pequeno";"2026-08";"2026-07";"2026-09-08"',
    ],
)

CADOP = _csv(
    "REGISTRO_OPERADORA;CNPJ;RAZAO_SOCIAL;NOME_FANTASIA;MODALIDADE",
    [
        '"000582";"61198164000160";"PORTO SEGURO - SEGURO SAÚDE S/A";"PORTO SEGURO";"Seguradora"',
        '"999999";"11111111000199";"OPERADORA DESCONHECIDA LTDA";"";"Medicina de Grupo"',
    ],
)


def _resolver(root: str) -> str | None:
    return "porto_seguro" if root == "61198164" else None


def test_to_float_ans_decimal_comma():
    assert ans_igr._to_float("83,07") == 83.07
    assert ans_igr._to_float("1.779,79") == 1779.79
    assert ans_igr._to_float("") is None
    assert ans_igr._to_float("n/a") is None


def test_parse_igr_takes_only_the_newest_competencia():
    rows = ans_igr.parse_igr(IGR)
    assert {r["competencia"] for r in rows} == {"2026-08"}   # 2026-07 dropped
    assert len(rows) == 3
    porto = [r for r in rows if r["registro"] == "000582"]
    assert len(porto) == 2                                    # both coberturas kept here
    assert porto[0]["igr"] == 71.80 and porto[0]["complaints"] == 620


def test_parse_cadop_builds_the_cnpj_bridge():
    cadop = ans_igr.parse_cadop(CADOP)
    assert cadop["000582"]["cnpj_root"] == "61198164"
    assert cadop["000582"]["modalidade"] == "Seguradora"


def test_map_to_entities_folds_coberturas_into_one_record():
    recs = ans_igr.map_to_entities(
        ans_igr.parse_igr(IGR), ans_igr.parse_cadop(CADOP),
        resolver=_resolver, today=dt.date(2026, 9, 19))
    assert len(recs) == 1
    r = recs[0]
    assert r["entity"] == "porto_seguro" and r["source"] == "ANS"
    assert r["complaints"] == 634          # 620 + 14 summed
    assert r["beneficiaries"] == 2102725   # 863538 + 1239187 summed
    # IGR is a RATE — take the dominant book's, never an average of the two
    assert r["index"] == 71.80
    assert r["cobertura"] == "Assistência médica"
    assert r["period"] == "2026-08" and r["date"] == "2026-09-19"
    assert "_top_complaints" not in r      # internal key stripped


def test_unresolved_operadoras_are_dropped_never_name_matched():
    # #140's core rule: the razão social must never be used to attribute. The unknown
    # operadora resolves to nothing and must simply vanish.
    recs = ans_igr.map_to_entities(
        ans_igr.parse_igr(IGR), ans_igr.parse_cadop(CADOP), resolver=_resolver)
    assert [r["entity"] for r in recs] == ["porto_seguro"]
    assert not any("DESCONHECIDA" in str(r.get("company", "")) for r in recs)


def test_a_registro_missing_from_cadop_is_dropped():
    # No bridge row => no CNPJ => no attribution, even though the IGR row is well-formed.
    recs = ans_igr.map_to_entities(
        ans_igr.parse_igr(IGR), {}, resolver=lambda root: "should_never_be_called")
    assert recs == []


def test_resolver_failure_never_breaks_the_run():
    def boom(root):
        raise RuntimeError("registry down")
    recs = ans_igr.map_to_entities(
        ans_igr.parse_igr(IGR), ans_igr.parse_cadop(CADOP), resolver=boom)
    assert recs == []


def test_summarize_ranks_worst_by_index():
    recs = [
        {"entity": "a", "index": 50.0, "complaints": 10, "period": "2026-08"},
        {"entity": "b", "index": 500.0, "complaints": 5, "period": "2026-08"},
        {"entity": "c", "index": None, "complaints": 1, "period": "2026-08"},
    ]
    s = ans_igr.summarize(recs)
    assert s["kind"] == "ans_complaints_index" and s["source"] == "ANS"
    assert s["total"] == 3 and s["period"] == "2026-08"
    assert [w["entity"] for w in s["worst"]] == ["b", "a"]   # higher IGR = worse
    assert s["worst"][0]["complaints"] == 5


def test_merge_is_keyed_by_entity_and_upserts():
    first = ans_igr.merge(None, [{"entity": "porto_seguro", "index": 71.8}],
                          today=dt.date(2026, 9, 19))
    assert first["count"] == 1 and first["as_of"] == "2026-09-19"
    second = ans_igr.merge(first, [{"entity": "porto_seguro", "index": 80.0},
                                   {"entity": "bradesco", "index": 83.0}],
                           today=dt.date(2026, 10, 19))
    assert second["count"] == 2
    assert second["records"]["porto_seguro"]["index"] == 80.0   # upserted, not duplicated
    assert {r["entity"] for r in ans_igr.list_records(second)} == {"porto_seguro", "bradesco"}


def test_run_is_a_noop_when_the_source_is_unavailable():
    out = ans_igr.run(bucket=None, downloader=lambda url: None, resolver=_resolver)
    assert out["status"] == "noop"


def test_run_end_to_end_without_a_bucket():
    blobs = {ans_igr.IGR_URL: IGR, ans_igr.CADOP_URL: CADOP}
    out = ans_igr.run(bucket=None, downloader=lambda url: blobs[url],
                      resolver=_resolver, today=dt.date(2026, 9, 19))
    assert out["status"] == "ok"
    assert out["operadoras"] == 2 and out["resolved"] == 1
    assert out["worst"][0]["entity"] == "porto_seguro"
