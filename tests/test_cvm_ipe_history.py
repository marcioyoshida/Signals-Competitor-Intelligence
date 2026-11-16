"""#331 CVM IPE history: material facts + board minutes for financial institutions."""
import csv
import gzip
import io
import json
import zipfile

from src.ingest import cvm_ipe
from src.ingest import cvm_ipe_history as h

COLS = ["CNPJ_Companhia", "Nome_Companhia", "Codigo_CVM", "Data_Referencia", "Categoria", "Tipo",
        "Especie", "Assunto", "Data_Entrega", "Tipo_Apresentacao", "Protocolo_Entrega", "Versao",
        "Link_Download"]

# HSBC Bank Brasil's root: HSBC's until 2016-06, Bradesco's from 2016-09 (as #330 records it)
EMAP = {"quarters": [201403, 202606], "roots": {
    "01701201": [{"from": 201403, "to": 201606, "group_entity_id": "hsbc", "entity_id": "hsbc"},
                 {"from": 201609, "to": 202606, "group_entity_id": "bradesco", "entity_id": "hsbc-bank"}],
    "60746948": [{"from": 201403, "to": 202606, "group_entity_id": "bradesco", "entity_id": "bradesco"}],
}}


def row(cnpj, cat, tipo="", especie="", ref="2015-11-30", entrega="2015-12-02", proto="1", versao="1",
        assunto="Assunto", nome="BANCO X"):
    return {"CNPJ_Companhia": cnpj, "Nome_Companhia": nome, "Codigo_CVM": "1", "Data_Referencia": ref,
            "Categoria": cat, "Tipo": tipo, "Especie": especie, "Assunto": assunto,
            "Data_Entrega": entrega, "Tipo_Apresentacao": "AP", "Protocolo_Entrega": proto,
            "Versao": versao, "Link_Download": f"https://www.rad.cvm.gov.br/x?p={proto}"}


def zipped(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLS, delimiter=";")
    w.writeheader()
    w.writerows(rows)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("ipe_cia_aberta_2015.csv", buf.getvalue().encode("latin-1"))
    return out.getvalue()


HSBC, BRAD, RETAIL = "01.701.201/0001-89", "60.746.948/0001-12", "33.000.167/0001-01"


def test_doc_class_keeps_four_classes_only():
    recs = list(h.year_records(zipped([
        row(BRAD, "Fato Relevante", proto="a"),
        row(BRAD, "Comunicado ao Mercado", proto="b"),
        row(BRAD, "Reunião da Administração", "Conselho de Administração", "Ata", proto="c"),
        row(BRAD, "Reunião da Administração", "Conselho de Administração", "Sumário das Decisões", proto="d"),
        row(BRAD, "Reunião da Administração", "Conselho Fiscal", "Ata", proto="e"),
        row(BRAD, "Reunião da Administração", "Diretoria", "Ata", proto="f"),
        row(BRAD, "Assembleia", "AGO", "Ata", proto="g"),
    ])))
    assert [h.doc_class(r) for r in recs] == ["fato_relevante", "comunicado", "ata_rca", "sumario_rca",
                                             None, None, None]


def test_select_keeps_only_financial_institutions_attributed_at_the_meeting_date():
    rows = h.select(h.year_records(zipped([
        row(HSBC, "Reunião da Administração", "Conselho de Administração", "Ata", ref="2015-11-30", proto="1"),
        row(HSBC, "Fato Relevante", ref="2016-10-03", entrega="2016-10-03", proto="2"),
        row(RETAIL, "Fato Relevante", proto="3", nome="VAREJO SA"),        # not in the map
    ])), EMAP)
    assert [(r["protocol"], r["entity_id"], r["direct_entity_id"]) for r in rows] == [
        ("1", "hsbc", "hsbc"), ("2", "bradesco", "hsbc-bank")]
    assert rows[0]["answer_key"] is True and rows[1]["answer_key"] is False
    assert rows[0]["cnpj_root"] == "01701201" and rows[0]["url"].startswith("https://www.rad.cvm.gov.br/")


def test_one_row_per_protocol_the_latest_version():
    rows = h.select(h.year_records(zipped([
        row(BRAD, "Fato Relevante", proto="9", versao="1", assunto="v1"),
        row(BRAD, "Fato Relevante", proto="9", versao="3", assunto="v3"),
        row(BRAD, "Fato Relevante", proto="9", versao="2", assunto="v2"),
    ])), EMAP)
    assert len(rows) == 1 and rows[0]["subject"] == "v3" and rows[0]["version"] == "3"


def test_events_for_splits_triggers_from_the_answer_key():
    rows = h.select(h.year_records(zipped([
        row(BRAD, "Fato Relevante", ref="2015-09-10", proto="t"),
        row(BRAD, "Reunião da Administração", "Conselho de Administração", "Ata", ref="2015-09-20", proto="k"),
        row(BRAD, "Reunião da Administração", "Conselho de Administração", "Ata", ref="2016-03-01", proto="late"),
    ])), EMAP)
    assert [r["protocol"] for r in h.events_for(rows, "bradesco", "2015-09-01", "2015-12-31")] == ["t", "k"]
    assert [r["protocol"] for r in h.events_for(rows, "bradesco", "2015-09-01", "2015-12-31",
                                                classes=h.ANSWER_KEY)] == ["k"]


class FakeS3:
    def __init__(self):
        self.objs = {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objs[Key] = (Body, kw)

    def get_object(self, Bucket, Key):
        if Key not in self.objs:
            raise type("NoSuchKey", (Exception,), {})(Key)
        return {"Body": io.BytesIO(self.objs[Key][0])}


def test_store_roundtrip_and_manifest():
    s3 = FakeS3()
    rows = h.select(h.year_records(zipped([row(BRAD, "Fato Relevante", proto="a")])), EMAP)
    meta = h.put_year(s3, "b", 2015, rows, source_bytes=123)
    assert meta["records"] == 1 and meta["by_class"]["fato_relevante"] == 1 and "rows" not in meta
    assert s3.objs[h.year_key(2015)][1]["ContentEncoding"] == "gzip"
    back = h.get_year(s3, "b", 2015)
    assert back["schema"] == h.SCHEMA and back["rows"] == rows
    assert h.get_year(s3, "b", 2099) is None
    h.put_manifest(s3, "b", {2015: meta}, "2026-11-16T00:00:00+00:00")
    man = json.loads(s3.objs[h.MANIFEST_KEY][0])
    assert man["first_year"] == man["last_year"] == 2015 and man["years"]["2015"]["records"] == 1


def test_live_reader_still_ignores_board_minutes():
    content = zipped([row(BRAD, "Reunião da Administração", "Conselho de Administração", "Ata",
                          entrega="2015-12-02")])
    import datetime as dt
    got = cvm_ipe.fetch_material_facts(lookback_days=45, today=dt.date(2015, 12, 10),
                                       fetcher=lambda y: content)
    assert got == []
    assert gzip  # imported for parity with the store format


def test_a_foreign_parents_relayed_sec_forms_are_dropped():
    subjects = ["4", "8-K", "8K", "425", "6-K", "SC 13G/A", "Form 20-F", "FWP",
                "Aquisição de participação", "8-K e resultado do 3T"]
    rows = h.select(h.year_records(zipped([
        row(BRAD, "Comunicado ao Mercado", proto=str(i), assunto=a) for i, a in enumerate(subjects)]
        + [row(BRAD, "Fato Relevante", proto="fr", assunto="8-K")])), EMAP)
    assert sorted(r["subject"] for r in rows) == ["8-K", "8-K e resultado do 3T", "Aquisição de participação"]
    assert {r["doc_class"] for r in rows if r["subject"] == "8-K"} == {"fato_relevante"}


def test_bdr_depositary_paperwork_is_dropped():
    subjects = ["Diversos Documentos", "Docs", "AltTaxaRepasseDivd", "Alt. Taxa. Repasse Divd.",
                "Redução da tarifa sobre distribuições em dinheiro", "144", "S-3ASR", "PRE 14A",
                "Documentos societários do novo banco digital", "Redução de juros do cartão"]
    rows = h.select(h.year_records(zipped([
        row(BRAD, "Comunicado ao Mercado", proto=str(i), assunto=a) for i, a in enumerate(subjects)])), EMAP)
    assert sorted(r["subject"] for r in rows) == ["Documentos societários do novo banco digital",
                                                  "Redução de juros do cartão"]


def test_a_bdr_depositarys_notices_are_dropped_but_its_board_minutes_kept():
    db = "62.331.228/0001-11"
    emap = {**EMAP, "roots": {**EMAP["roots"], "62331228": [
        {"from": 201403, "to": 202606, "group_entity_id": "deutsche", "entity_id": "deutsche"}]}}
    rows = h.select(h.year_records(zipped([
        row(db, "Comunicado ao Mercado", proto="1", assunto="PAGAMENTO DIVIDENDO DEFINITIVO"),
        row(db, "Reunião da Administração", "Conselho de Administração", "Ata", proto="2"),
        row(db, "Fato Relevante", proto="3")])), emap)
    assert sorted(r["doc_class"] for r in rows) == ["ata_rca", "fato_relevante"]
