"""#335 BCB bank-fee tables as dated snapshots."""
from src.ingest import tarifas_history as th

EMAP = {"quarters": [202403, 202606], "roots": {
    "60701190": [{"from": 202403, "to": 202606, "group_entity_id": "itau", "entity_id": "itau"}]}}


def fake_get(url):
    if "ListaInstituicoesDeGrupoConsolidado" in url:
        return {"value": [{"Cnpj": "60701190", "Nome": "ITAÚ UNIBANCO S.A."}]}
    if "ListaTarifasPorInstituicaoFinanceira" in url:
        if "%27F%27" in url:
            return {"value": [{"CodigoServico": "1101", "Servico": "Cadastro", "Unidade": "Por cliente",
                               "DataVigencia": "2012-08-01", "ValorMaximo": 30.0, "TipoValor": "Real",
                               "Periodicidade": "Por evento"}]}
        raise RuntimeError("boom")
    if "ListaValoresServicoBancario" in url:
        return {"value": [{"CodigoServico": "1101", "NomeServico": "Cadastro", "ValorMinimo": 0.0,
                           "ValorMedio": 2915.73, "ValorMaximo": 99999.99}]}
    raise AssertionError(url)


def test_url_quotes_odata_parameters():
    u = th._url(th.BASE, "ListaTarifasPorInstituicaoFinanceira", {"PessoaFisicaOuJuridica": "F", "CNPJ": "60701190"})
    assert "(PessoaFisicaOuJuridica=@PessoaFisicaOuJuridica,CNPJ=@CNPJ)" in u
    assert "@CNPJ=%2760701190%27" in u and u.endswith("$format=json&$top=100000")


def test_snapshot_attributes_records_failures_and_flags_placeholder_maximums():
    s = th.snapshot("2026-11-16", EMAP, groups=["03"], get=fake_get, pause=0)
    inst = s["institutions"]["60701190"]
    assert inst["entity_id"] == "itau" and inst["groups"] == ["03"]
    assert inst["F"][0] == {"code": "1101", "service": "Cadastro", "unit": "Por cliente", "since": "2012-08-01",
                            "max": 30.0, "value_type": "Real", "periodicity": "Por evento"}
    assert inst["J"] is None and s["failed"] == 1
    assert s["market"]["03"]["F"][0]["placeholder_max"] is True
    assert th.is_placeholder(99999.99) and not th.is_placeholder(30.0) and not th.is_placeholder(None)


def test_changes_between_snapshots():
    def snap(fees):
        return {"institutions": {"60701190": {"entity_id": "itau", "F": fees, "J": []}}}
    a = snap([{"code": "1101", "service": "Cadastro", "max": 30.0, "since": "2012-08-01"},
              {"code": "1201", "service": "2a via", "max": 9.4, "since": "2024-03-01"}])
    b = snap([{"code": "1101", "service": "Cadastro", "max": 35.0, "since": "2026-11-01"},
              {"code": "1301", "service": "Saque", "max": 2.5, "since": "2026-11-01"}])
    ch = {c["code"]: c for c in th.changes(a, b)}
    assert ch["1101"]["kind"] == "changed" and (ch["1101"]["from"], ch["1101"]["to"]) == (30.0, 35.0)
    assert ch["1201"]["kind"] == "withdrawn" and ch["1301"]["kind"] == "new"
    assert th.changes(a, a) == []
