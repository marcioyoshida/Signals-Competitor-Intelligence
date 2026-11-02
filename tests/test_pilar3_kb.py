"""#299 / ADR 022 Phase 6: Pilar 3 risk-report prose into the Bedrock KB for grounded Q&A."""
import json
from pathlib import Path

from src.ingest import bcb_soundness, pilar3

HDR = ["Gerenciamento de Riscos e Capital – Pilar 3", "_" * 60, "Banco Exemplo"]
PROSE_A = ("O Apetite de Risco articula o conjunto de diretrizes do Conselho de Administração sobre "
           "estratégia e assunção de riscos, definindo a natureza e o nível dos riscos aceitáveis "
           "para a organização e como eles são monitorados.")
PROSE_B = ("A liquidez do banco deverá suportar longos períodos de estresse, e é monitorada por meio "
           "do acompanhamento dos indicadores de curto e de longo prazo definidos pela governança.")


def _pages():
    toc = HDR + ["Risco de Crédito 29", "CRA: Informações qualitativas sobre o risco de crédito 29", "3"]
    p1 = HDR + ["Indicadores prudenciais", *PROSE_A.split(" e ", 1)[:1], "e " + PROSE_A.split(" e ", 1)[1], "4"]
    table = HDR + ["Capital Principal 123.456 7,8% 9,1% 10,2%", "Nível I 98.765 4,3% 2,1%",
                   "CR1: Qualidade creditícia das exposições", "5"]
    p2 = HDR + [PROSE_B, "6"]
    return [toc, p1, table, p2]


def _text_pages():
    return ["\n".join(p) for p in _pages()]


def test_prose_keeps_sentences_and_drops_headers_toc_tables():
    out = pilar3.prose_text(_text_pages())
    paras = out.split("\n\n")
    assert len(paras) == 2
    # A section heading joins the paragraph after it, so each chunk carries its topic.
    assert paras[0] == "Indicadores prudenciais " + PROSE_A and paras[1] == PROSE_B
    for junk in ("Banco Exemplo", "Risco de Crédito 29", "CRA:", "123.456", "CR1:", "____"):
        assert junk not in out


def test_document_has_a_header_and_never_starts_with_from():
    rec = {"entity": "exemplo", "company": "BANCO EXEMPLO S.A.", "date": "2026-08-04",
           "subject": "Gerenciamento de Riscos e Capital - Pilar 3 (2T26)", "url": "https://rad.cvm.gov.br/x"}
    doc = pilar3.kb_document(rec, "From the top: prose.")
    assert doc.startswith("Relatório de Gerenciamento de Riscos e Capital (Pilar 3) — BANCO EXEMPLO S.A.")
    assert "Fonte: https://rad.cvm.gov.br/x" in doc
    assert pilar3.kb_key(rec) == "pilar3/exemplo/2026-08-04.txt"
    assert len(pilar3.kb_document(rec, "x" * 500_000)) < pilar3.KB_MAX_CHARS + 1000


class _S3:
    def __init__(self, existing=()):
        self.objects = {k: b"" for k in existing}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise Exception("404")
        return {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body


INDEX = {"records": {
    "exemplo": {"entity": "exemplo", "company": "BANCO EXEMPLO S.A.", "date": "2026-08-04",
                "subject": "Pilar 3 (2T26)", "url": "https://rad.cvm.gov.br/a", "sentences": ["x"]},
    "outro": {"entity": "outro", "date": "2026-05-02", "url": "https://rad.cvm.gov.br/b", "sentences": ["y"]},
    "sem_url": {"entity": "sem_url", "date": "2026-05-02", "sentences": ["z"]},
}}


def test_promote_writes_doc_and_citable_metadata_once():
    s3, fetched = _S3(existing={"pilar3/outro/2026-05-02.txt"}), []
    keys = pilar3.promote_to_kb(INDEX, bucket="raw", s3=s3,
                                fetch_pages=lambda url: fetched.append(url) or _text_pages())
    assert keys == ["pilar3/exemplo/2026-08-04.txt"]
    assert fetched == ["https://rad.cvm.gov.br/a"]               # the seen filing is not re-downloaded
    meta = json.loads(s3.objects["pilar3/exemplo/2026-08-04.txt.metadata.json"])["metadataAttributes"]
    assert meta["url"] == "https://rad.cvm.gov.br/a" and meta["source"] == "CVM-Pilar3"
    assert meta["date"] == "2026-08-04" and meta["name"] == "BANCO EXEMPLO S.A."
    body = s3.objects["pilar3/exemplo/2026-08-04.txt"].decode()
    assert PROSE_B in body
    again = pilar3.promote_to_kb(INDEX, bucket="raw", s3=s3, fetch_pages=lambda url: _text_pages())
    assert again == []


def test_a_filing_without_prose_is_not_written():
    s3 = _S3()
    assert pilar3.promote_to_kb({"records": {"exemplo": INDEX["records"]["exemplo"]}}, bucket="raw",
                                s3=s3, fetch_pages=lambda url: ["\n".join(HDR + ["Tabela 1 2 3"])] * 4) == []
    assert s3.objects == {}


def test_merge_keeps_a_half_yearly_filer_and_takes_the_newer_filing():
    old = {"records": {"a": {"entity": "a", "date": "2026-02-10"}, "b": {"entity": "b", "date": "2026-03-01"}}}
    fresh = {"as_of": "2026-11-02", "records": {"a": {"entity": "a", "date": "2026-08-04"}}}
    m = pilar3.merge(old, fresh)
    assert m["records"]["a"]["date"] == "2026-08-04" and m["records"]["b"]["date"] == "2026-03-01"
    assert m["count"] == 2


def test_run_passes_a_lookback_not_the_year(monkeypatch):
    from src.ingest import cvm_ipe
    seen = {}
    monkeypatch.setattr(cvm_ipe, "fetch_material_facts", lambda lb, **k: seen.setdefault("lb", lb) and [])
    pilar3.run(None)
    assert seen["lb"] == 200


def test_handler_stage_starts_one_ingestion_job_only_when_written(monkeypatch):
    calls = []
    monkeypatch.setenv("ONCA_PILAR3_KB", "true")
    monkeypatch.setenv("ONCA_RAW_BUCKET", "raw")
    monkeypatch.setenv("ONCA_KB_ID", "kb")
    monkeypatch.setenv("ONCA_KB_DATA_SOURCE_ID", "ds")
    monkeypatch.setattr(bcb_soundness, "run", lambda *a, **k: {"status": "noop"})
    for mod in ("bcb_fundamentals", "bcb_inadimplencia", "bcb_resultados", "bcb_km1"):
        m = __import__(f"src.ingest.{mod}", fromlist=["run"])
        monkeypatch.setattr(m, "run", lambda *a, **k: {"status": "ok"})
    runs = []
    monkeypatch.setattr(pilar3, "run", lambda b, kb_bucket=None, **k: runs.append(kb_bucket) or
                        {"status": "ok", "kb_written": len(runs) == 1, "kb_keys": ["pilar3/a/1.txt"]})

    class _Agent:
        def start_ingestion_job(self, **kw):
            calls.append(kw)
            return {"ingestionJob": {"ingestionJobId": "job-1"}}

    import boto3
    monkeypatch.setattr(boto3, "client", lambda name, **k: _Agent())
    body = json.loads(bcb_soundness.lambda_handler({}, None)["body"])
    assert runs == ["raw"] and calls == [{"knowledgeBaseId": "kb", "dataSourceId": "ds"}]
    assert body["pilar3"]["kb_ingestion_job"] == "job-1"
    bcb_soundness.lambda_handler({}, None)                       # nothing new written: no job
    assert len(calls) == 1
    monkeypatch.setenv("ONCA_PILAR3_KB", "false")
    bcb_soundness.lambda_handler({}, None)
    assert runs[-1] is None                                      # flag off: tone corpus only


def test_cdk_wires_the_financials_lambda_to_the_kb():
    src = (Path(__file__).resolve().parents[1] / "infra" / "app.py").read_text()
    block = src[src.index('"OncaFinancials",'):src.index("balancete_fn = lambda_.Function(")]
    assert '"ONCA_PILAR3_KB": "true"' in block and '"ONCA_KB_DATA_SOURCE_ID"' in block
    assert 'raw_bucket.grant_put(financials_fn, "pilar3/*")' in block
    assert "bedrock:StartIngestionJob" in block
    reqs = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text()
    assert "pypdf" in reqs                                        # the Lambda bundle needs it


def test_one_document_per_filing_when_a_bank_resolves_to_two_ids():
    """Live 2026-11-02: itau and itau-unibanco both matched the 2T26 filing -> two KB copies."""
    rec = {"company": "ITAU UNIBANCO HOLDING S.A.", "date": "2026-08-04", "url": "https://rad.cvm.gov.br/i"}
    idx = {"records": {"itau-unibanco": {**rec, "entity": "itau-unibanco"}, "itau": {**rec, "entity": "itau"}}}
    s3 = _S3()
    assert pilar3.promote_to_kb(idx, bucket="raw", s3=s3, fetch_pages=lambda u: _text_pages()) == [
        "pilar3/itau/2026-08-04.txt"]
