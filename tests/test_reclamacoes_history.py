"""#335 BCB complaints-ranking history: three methodologies, dated attribution."""
import datetime as dt

from src.ingest import reclamacoes_history as rh

H2014 = ("Ano;Semestre;Categoria;Tipo;CNPJ IF;Instituição financeira;Índice;"
         "Quantidade de reclamações reguladas procedentes;Quantidade de reclamações reguladas - outras;"
         "Quantidade de reclamações não reguladas;Quantidade total de reclamações;"
         "Quantidade total de clientes  CCS e SCR;Quantidade de clientes  CCS;Quantidade de clientes  SCR;")
H2024 = ("Ano;Trimestre;Categoria;Tipo;CNPJ IF;Instituição financeira;Índice;"
         "Quantidade total de reclamações respondidas;Quantidade de reclamações procedentes;"
         "Quantidade de reclamações procedentes extrapoladas;Quantidade total de reclamações analisadas;"
         "Quantidade total de clientes  CCS e SCR;Quantidade de clientes  CCS;Quantidade de clientes  SCR;")
H2024Q2 = ("Ano;Trimestre;Categoria;Tipo;CNPJ IF;Instituição financeira;Índice;"
           "Quantidade total de reclamações respondidas;Quantidade de reclamações reguladas procedentes extrapoladas;"
           "Quantidade total de clientes  CCS e SCR;Quantidade de clientes  CCS;Quantidade de clientes  SCR;")
S15 = {"ano": 2015, "periodicidade": "SEMESTRAL", "periodo": 2}
T24 = {"ano": 2024, "periodicidade": "TRIMESTRAL", "periodo": 4}

EMAP = {"quarters": [201403, 202606], "roots": {
    "01701201": [{"from": 201403, "to": 201606, "group_entity_id": "hsbc", "entity_id": "hsbc"},
                 {"from": 201609, "to": 202606, "group_entity_id": "bradesco", "entity_id": "hsbc-bank"}],
    "60746948": [{"from": 201403, "to": 202606, "group_entity_id": "bradesco", "entity_id": "bradesco"}],
    "13660104": [{"from": 201403, "to": 202606, "group_entity_id": None, "entity_id": None}]}}
SNAP = [{"CodInst": "C0010045", "NomeInstituicao": "BRADESCO", "CnpjInstituicaoLider": "60746948"},
        {"CodInst": "C0080099", "NomeInstituicao": "HSBC", "CnpjInstituicaoLider": "01701201"},
        {"CodInst": "00360305", "NomeInstituicao": "CAIXA ECONOMICA FEDERAL", "CnpjInstituicaoLider": None},
        {"CodInst": "60746948", "NomeInstituicao": "BRADESCO", "CnpjInstituicaoLider": "60746948"}]


def csv(header, *rows):
    return ("\n".join([header, *rows]) + "\n").encode("latin-1")


def test_periods_labels_and_ends():
    ps = rh.periods(dt.date(2017, 7, 1))
    assert [rh.label(p) for p in ps] == ["2014-S2", "2015-S1", "2015-S2", "2016-S1", "2016-S2",
                                         "2017-T1", "2017-T2"]
    assert rh.period_end(S15) == "2015-12-31" and rh.period_end(T24) == "2024-12-31"
    assert rh.period_end({"ano": 2016, "periodicidade": "SEMESTRAL", "periodo": 1}) == "2016-06-30"


def test_methodology_is_read_from_the_header():
    assert rh.method_of(H2014.split(";")) == "regulated_2014"
    assert rh.method_of(H2024Q2.split(";")) == "transition_2024"
    assert rh.method_of(H2024.split(";")) == "sampled_2024"


def test_parse_both_layouts_with_br_numbers():
    a = rh.parse(csv(H2014, "2015;2º;Mais de dois milhões de clientes;Conglomerado; ;HSBC (conglomerado);"
                           "1,23;100;50;10;160;81.300.000;80000000;5000000;",
                     "2015;2º;Menos de dois milhões de clientes;Banco/financeira;13660104;AGIPLAN; ;11;46;97;154;99272; ;99272;"), S15)
    assert a[0] == {**a[0], "period": "2015-S2", "as_of": "2015-12-31", "method": "regulated_2014",
                    "kind": "conglomerado", "cnpj8": None, "indice": 1.23, "reguladas_procedentes": 100.0,
                    "reguladas_outras": 50.0, "nao_reguladas": 10.0, "total": 160.0, "clientes": 81300000.0}
    assert a[1]["kind"] == "instituicao" and a[1]["cnpj8"] == "13660104" and a[1]["indice"] is None
    b = rh.parse(csv(H2024, "2024;4º;Top 15;Conglomerado; ;BRADESCO (conglomerado);20,5;5000;400;1200;900;70000000;1;1;"), T24)
    assert b[0]["method"] == "sampled_2024" and b[0]["procedentes"] == 400.0
    assert b[0]["procedentes_extrapoladas"] == 1200.0 and b[0]["analisadas"] == 900.0 and "total" not in b[0]


def test_conglomerates_resolve_through_the_register_by_date_and_caixa_by_institution_name():
    leaders = rh.conglomerate_leaders(SNAP)
    assert leaders["bradesco"] == "60746948" and leaders["hsbc"] == "01701201"
    assert leaders["caixa economica federal"] == "00360305"
    assert rh.name_key("CAIXA ECONÔMICA FEDERAL (conglomerado)") == "caixa economica federal"
    rows = rh.parse(csv(H2014, "2015;2º;X;Conglomerado; ;HSBC (conglomerado);1;1;1;1;1;1;1;1;",
                             "2015;2º;X;Banco/financeira;13660104;AGIPLAN; ;1;1;1;1;1;1;1;",
                             "2015;2º;X;Conglomerado; ;NOVO (conglomerado);1;1;1;1;1;1;1;1;"), S15)
    out = rh.attribute(rows, EMAP, leaders, resolve_name=lambda n: ["novo_bank"] if n == "novo" else [])
    assert (out[0]["entity_id"], out[0]["attribution"], out[0]["leader_cnpj8"]) == ("hsbc", "dated", "01701201")
    assert out[1]["entity_id"] is None and out[1]["attribution"] is None      # in the map, no registry entity
    assert (out[2]["entity_id"], out[2]["attribution"]) == ("novo_bank", "name")


def test_series_keeps_the_conglomerate_headline_per_period():
    rows = [{"period": "2015-S2", "as_of": "2015-12-31", "kind": "instituicao", "cnpj8": "60746948",
             "entity_id": "bradesco", "method": "regulated_2014", "indice": 9.0, "name": "BCO BRADESCO"},
            {"period": "2015-S2", "as_of": "2015-12-31", "kind": "conglomerado", "cnpj8": None,
             "entity_id": "bradesco", "method": "regulated_2014", "indice": 7.0, "name": "BRADESCO (conglomerado)"},
            {"period": "2024-T4", "as_of": "2024-12-31", "kind": "conglomerado", "cnpj8": None,
             "entity_id": "bradesco", "method": "sampled_2024", "indice": 20.5, "name": "BRADESCO (conglomerado)"},
            {"period": "2024-T4", "as_of": "2024-12-31", "kind": "conglomerado", "cnpj8": None,
             "entity_id": None, "method": "sampled_2024", "indice": 1.0, "name": "NOVO (conglomerado)"}]
    s = rh.build_series(rows, "t", ["2015-S2", "2024-T4"], ["2022-T2"])
    assert [p["indice"] for p in rh.series_for(s, "bradesco")] == [7.0, 20.5]
    assert len(rh.series_for(s, "bradesco", conglomerate_only=False)) == 3
    assert [p["period"] for p in rh.series_for(s, "bradesco", method="sampled_2024")] == ["2024-T4"]
    assert s["unattributed"]["NOVO (conglomerado)"]["rows"] == 1 and s["not_published"] == ["2022-T2"]


def test_fetch_treats_204_as_not_published():
    class R:
        def __init__(self, code, content):
            self.status_code, self.content = code, content

        def raise_for_status(self):
            pass
    assert rh.fetch(T24, get=lambda *a, **k: R(204, b"")) is None
    assert rh.fetch(T24, get=lambda *a, **k: R(200, b"x")) == b"x"
