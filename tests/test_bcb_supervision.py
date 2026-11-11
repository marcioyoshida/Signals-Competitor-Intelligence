"""#378 part 2: BCB supervision on the CRO board — PAS sanctions, Pix data incidents, consultations.

All institutions below are INVENTED (public repo)."""
import datetime as dt
import io
import json

from src.dashboard import feed_builder
from src.ingest import bcb_supervision as b
from src.synth import executive

TODAY = dt.date(2026, 11, 10)


def _pas(**kw):
    row = {"PAS": "900001", "Nome": "BANCO EXEMPLO ALFA S.A.", "CPF_CNPJ": "11222333000144",
           "Data_da_citacao": "2026-09-01", "Numero_decisao_1_instancia": "COPAS - 2026 / 0101",
           "Data_da_decisao_1_instancia": "2026-11-09", "Tipo_penalidade_1_instancia": "MULTA",
           "Duracao_da_pena_1_instancia": None, "Valor_da_multa_1_instancia": 1500000.0,
           "Apresentou_recurso": "SIM", "Data_da_decisao_2_instancia": None,
           "Tipo_penalidade_2_instancia": None,
           "Situacao": "AGUARDANDO JULGAMENTO DE RECURSO PELA 2ª INSTÂNCIA"}
    row.update(kw)
    return row


# --- PAS -----------------------------------------------------------------------------------
def test_pas_url_uses_percent20_and_bare_date():
    u = b.pas_url("2025-11-10")
    assert "+" not in u and "%20ge%202025-11-10" in u and "'" not in u
    assert "$orderby=Data_da_decisao_1_instancia%20desc" in u


def test_pas_drops_individuals_lgpd():
    assert b.normalize_pas(_pas(CPF_CNPJ="***.123.456-**")) is None       # masked CPF
    assert b.normalize_pas(_pas(CPF_CNPJ="12345678901")) is None          # bare CPF


def test_pas_keeps_copas_and_sfn_drops_capital_register_noise():
    r = b.normalize_pas(_pas())
    assert r["committee"] == "COPAS" and r["fine"] == 1500000.0 and r["appealed"] is True
    assert r["cnpj_root"] == "11222333" and r["industries"] == ["banking"]
    # DERAD capital-register fine on a non-financial firm: noise for a bank CRO
    assert b.normalize_pas(_pas(Nome="EXEMPLO PARTICIPACOES LTDA",
                                Numero_decisao_1_instancia="DERADGEPAGGTSUL - 2026 / 9")) is None
    # …but the same committee against a BCB-supervised institution stays
    assert b.normalize_pas(_pas(Nome="COOPERATIVA DE CREDITO EXEMPLO LTDA",
                                Numero_decisao_1_instancia="DERADGEPAGGTSUL - 2026 / 9")) is not None
    # funds / ETFs / reinsurance brokers are not the BCB's supervised institutions
    assert b.normalize_pas(_pas(Nome="EXEMPLO ETF CLASSE DE INDICE",
                                Numero_decisao_1_instancia="DERADGEPAGGTSUL - 2026 / 9")) is None


def test_pas_uninformative_copas_name_goes_to_all_bcb_modules():
    r = b.normalize_pas(_pas(Nome="EXEMPLO LTDA"))
    assert r["industries"] == ["banking", "fintech", "acquiring"]


def test_industry_inference():
    assert b.infer_industries("ADMINISTRADORA DE CONSÓRCIO EXEMPLO") == ["consorcio"]
    assert b.infer_industries("EXEMPLO SOCIEDADE DE CRÉDITO DIRETO S.A.") == ["fintech"]
    assert b.infer_industries("EXEMPLO DTVM LTDA") == ["investment-banking"]
    assert b.infer_industries("EXEMPLO INSTITUIÇÃO DE PAGAMENTO S.A.") == ["acquiring", "fintech"]


# --- Pix -----------------------------------------------------------------------------------
def test_parse_period_forms():
    assert b.parse_period("26 a 28 de abril de 2026") == ("2026-04-26", "2026-04-28")
    assert b.parse_period("30 de agosto de 2025 a 27 de fevereiro de 2026") == ("2025-08-30", "2026-02-27")
    assert b.parse_period("1º de março de 2026") == ("2026-03-01", "2026-03-01")
    assert b.parse_period("data não informada") == (None, None)


def test_pix_dict_only_keys_and_invisible_chars():
    row = {"BancoDados": "DICT​", "Periodo_incidente": "26 a 28 de abril de 2026",
           "Instituicao_incidente": "Exemplo Sociedade de Crédito Direto S.A.",
           "NaturezaDados": "<p>Dados cadastrais vinculados a 28.203 chaves​ Pix&#58; nome, CPF</p>"}
    r = b.normalize_pix(row)
    assert r["keys"] == 28203 and r["date"] == "2026-04-28" and r["industries"] == ["fintech"]
    assert b.normalize_pix({**row, "BancoDados": "Pesquisa de hábitos"}) is None
    public = b.normalize_pix({**row, "Instituicao_incidente": "Órgão Público Exemplo"})
    assert public["industries"] == ["banking", "fintech", "acquiring"]        # Pix ecosystem


# --- consultations -------------------------------------------------------------------------
PAGE = """<html><body><div>10/11/2026</div><span>Consultas e outras participações ativas</span>
<div>131/2026 | <a href="x">Detalhar consulta</a></div><div>Data final da consulta: 24/01/2027</div>
<p>Submete à consulta pública minuta de resolução sobre captação financeira. Rodapé do site</p>
<div>132/2026 | <a href="y">Detalhar consulta</a></div><div>Data final da consulta: 05/12/2026</div>
<p>Submete à consulta pública proposta sobre consórcios.</p></body></html>"""


def test_parse_consultas_skips_header_date_and_reads_deadlines():
    rows = b.parse_consultas(PAGE, today=TODAY)
    assert [r["number"] for r in rows] == ["131/2026", "132/2026"]            # never "11/2026"
    assert rows[0]["deadline"] == "2027-01-24" and rows[0]["days_to_deadline"] == 75
    assert rows[0]["subject"].endswith("captação financeira.")              # footer cut
    assert rows[1]["industries"] == ["consorcio"]


# --- state ---------------------------------------------------------------------------------
class _S3:
    def __init__(self, objs=None):
        self.objs = dict(objs or {})

    def get_object(self, Bucket, Key):
        if Key not in self.objs:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objs[Key])}

    def put_object(self, Bucket, Key, Body, **_):
        self.objs[Key] = Body


def test_update_state_keeps_previous_rows_when_a_source_fails():
    prev = {"pix_incidents": [{"id": "bcb-pix:old", "name": "X", "date": "2026-01-01"}],
            "pix_incidents_as_of": "2026-11-09"}
    s3 = _S3({b.STATE_KEY: json.dumps(prev).encode()})

    def get(url):
        if "Gepad" in url:
            return {"value": [_pas()]}
        raise RuntimeError("502")

    out = b.update_state("bkt", s3=s3, get=get, consultas_fn=lambda today: [],
                         cnpj_to_entity={"11222333": "banco_alfa"}, today=TODAY)
    doc = json.loads(s3.objs[b.STATE_KEY])
    assert out["pas"] == 1 and out["matched"] == 1 and out["errors"] == ["pix_incidents"]
    assert doc["pas"][0]["entity"] == "banco_alfa"
    assert doc["pix_incidents"] == prev["pix_incidents"] and doc["pix_incidents_as_of"] == "2026-11-09"


def test_update_state_carries_previous_bindings_when_registry_scan_fails():
    """2026-11-11 06:50 run: the registry scan timed out, the maps came in empty, and every row
    lost its entity. An empty map now reuses the previous run's bindings."""
    prev = {"pas": [b.normalize_pas(_pas()) | {"entity": "banco_alfa"}],
            "pix_incidents": [{"id": "bcb-pix:1", "name": "Exemplo Pagamentos S.A.",
                               "entity": "exemplo_pag", "date": "2026-05-01"}]}
    s3 = _S3({b.STATE_KEY: json.dumps(prev).encode()})

    def get(url):
        if "Gepad" in url:
            return {"value": [_pas()]}
        return {"conteudo": [{"BancoDados": "DICT", "Periodo_incidente": "1 a 2 de maio de 2026",
                           "Instituicao_incidente": "Exemplo Pagamentos S.A.",
                           "NaturezaDados": "46 chaves Pix"}]}

    out = b.update_state("bkt", s3=s3, get=get, consultas_fn=lambda today: [],
                         cnpj_to_entity={}, name_to_entity={}, today=TODAY)
    doc = json.loads(s3.objs[b.STATE_KEY])
    assert out["matched"] == 2 and doc["entity_map"] == "carried"
    assert doc["pas"][0]["entity"] == "banco_alfa"
    assert doc["pix_incidents"][0]["entity"] == "exemplo_pag"


def test_update_state_fresh_map_wins_over_carried():
    prev = {"pas": [b.normalize_pas(_pas()) | {"entity": "old_id"}]}
    s3 = _S3({b.STATE_KEY: json.dumps(prev).encode()})
    out = b.update_state("bkt", s3=s3, get=lambda url: {"value": [_pas()]} if "Gepad" in url else {"value": []},
                         consultas_fn=lambda today: [], cnpj_to_entity={"11222333": "banco_alfa"},
                         today=TODAY)
    doc = json.loads(s3.objs[b.STATE_KEY])
    assert out["matched"] == 1 and doc["pas"][0]["entity"] == "banco_alfa" and "entity_map" not in doc


def test_supervision_runs_on_its_own_budget_not_in_the_entrants_hook():
    """It shared the BCB autorizações budget inside _post_entrants; a slow register fetch
    exhausted it and killed the registry scan."""
    src = open("src/ingest/lambda_port.py", encoding="utf-8").read()
    hook = src[src.index("def _post_entrants("):src.index("def _post_fatos(")]
    assert "bcb_supervision" not in hook
    assert '_source_budget("BCB supervision"' in src


# --- feed projection + scoping -------------------------------------------------------------
DOC = {"as_of": "2026-11-10",
       "pas": [b.normalize_pas(_pas()) | {"entity": "banco_alfa"},
               b.normalize_pas(_pas(PAS="900002", Nome="EXEMPLO DTVM LTDA", CPF_CNPJ="99888777000166",
                                    Valor_da_multa_1_instancia=50000.0)),
               b.normalize_pas(_pas(PAS="900003", Data_da_decisao_1_instancia="2025-12-01"))],
       "pix_incidents": [b.normalize_pix({"BancoDados": "DICT", "Periodo_incidente": "1 a 2 de maio de 2026",
                                          "Instituicao_incidente": "Exemplo Pagamentos S.A.",
                                          "NaturezaDados": "46 chaves Pix"})],
       "consultas": [{"id": "bcb-cp:131/2026", "number": "131/2026", "deadline": "2027-01-24",
                      "subject": "Captação", "industries": ["banking"]},
                     {"id": "bcb-cp:1/2026", "number": "1/2026", "deadline": "2026-11-01",
                      "subject": "Encerrada", "industries": ["banking"]}],
       "errors": {"pix_incidents": "boom"}}
ATTRS = {"banco_alfa": {"label": "Banco Alfa", "industries": ["banking"]}}


def test_for_feed_binds_roster_windows_and_recounts():
    out = b.for_feed(DOC, entity_attrs=ATTRS, today=TODAY)
    assert "errors" not in out
    assert [r.get("label") for r in out["pas"]][0] == "Banco Alfa"
    assert len(out["pas"]) == 2                                   # 2025-12-01 is past 180 days
    assert all("cnpj_root" not in r for r in out["pas"])
    assert [c["number"] for c in out["consultas"]] == ["131/2026"]   # closed one dropped
    assert out["consultas"][0]["days_to_deadline"] == 75
    unknown = b.for_feed({"pas": [{**DOC["pas"][0], "entity": "not_in_roster"}]}, entity_attrs=ATTRS, today=TODAY)
    assert "entity" not in unknown["pas"][0]


def test_scope_fail_closed_and_entity_follows_row_ok():
    block = b.for_feed(DOC, entity_attrs=ATTRS, today=TODAY)
    insurance = b.scope(block, ["insurance"], lambda r: False)
    assert insurance["pas"] == [] and insurance["pix_incidents"] == [] and insurance["consultas"] == []
    ib = b.scope(block, ["investment-banking"], lambda r: False)
    assert [r["name"] for r in ib["pas"]] == ["EXEMPLO DTVM LTDA"]    # bound row needs row_ok
    bank = b.scope(block, ["banking"], lambda r: r.get("entity") == "banco_alfa")
    assert {r.get("entity") for r in bank["pas"]} == {"banco_alfa"}
    assert b.scope(block, [], lambda r: True)["pix_incidents"] == []


def test_every_feed_projection_scopes_or_empties_the_block():
    src = open(feed_builder.__file__, encoding="utf-8").read()
    assert src.count('"bcb_supervision": _scope_bcb_supervision(') == 2   # tenant + entry
    assert '"seven_s", "groups", "product_radar", "bcb_supervision"):' in src  # sample tier empties it


# --- executive CRO -------------------------------------------------------------------------
def test_cro_groups_sanctions_and_adds_consultation_deadlines():
    block = b.for_feed(DOC, entity_attrs=ATTRS, today=TODAY)
    twin = {**block["pas"][0], "id": "bcb-pas:900001:2", "fine": 500000.0, "status": "MULTA PAGA"}
    block["pas"].append(twin)
    rows = executive._bcb_sanction_rows(block)
    alfa = next(r for r in rows if r["label"] == "Banco Alfa")
    assert alfa["n_decisions"] == 2 and alfa["fine_total"] == 2000000.0 and alfa["appealed"]
    assert len(alfa["statuses"]) == 2
    dl = executive._consulta_deadline_rows(block)
    assert dl[0]["deadline_source"] == "consulta" and dl[0]["days_to_deadline"] == 75
    assert dl[0]["domain"] == "Consulta pública BCB 131/2026"


def test_build_executive_carries_bcb_panels():
    feed = {"dates": ["2026-11-10"], "industry_options": [{"slug": "banking", "label": "Banking"}],
            "entity_attrs": ATTRS, "feed": [], "bcb_supervision": b.for_feed(DOC, entity_attrs=ATTRS, today=TODAY)}
    cro = executive.build_executive(feed)["cro"]
    P = cro["panels"]
    assert P["bcb_sanctions"] and P["pix_incidents"]
    assert any(r.get("deadline_source") == "consulta" for r in P["deadlines"])
    agg = cro["by_industry"]["banking"]
    assert agg["n_bcb_sanctions"] >= 1 and agg["n_deadlines"] >= 1 and agg["bcb_fines_total"] >= 1500000


def test_name_key_conservative_matching():
    assert b.name_key("Banco Exemplo S.A.") == b.name_key("Exemplo") == "EXEMPLO"
    assert b.name_key("Exemplo S.A. – Crédito, Financiamento e Investimento") == "EXEMPLO"
    rows = [{"name": "Banco Exemplo S.A."}, {"name": "Outra Coisa Ltda"}, {"name": "Duplo S.A."}]
    b.match_entities(rows, {}, {"EXEMPLO": "exemplo", "DUPLO": "duplo_a", "BANCO DUPLO": "duplo_b"})
    assert rows[0]["entity"] == "exemplo" and "entity" not in rows[1]
    assert "entity" not in rows[2]                      # ambiguous key binds nothing


def test_parenthesised_short_name_matches():
    rows = [{"name": "Banco Exemplo do Estado S.A. (Exemplão)"}]
    b.match_entities(rows, {}, {b._fold("Exemplão"): "exemplao"})
    assert rows[0]["entity"] == "exemplao"
