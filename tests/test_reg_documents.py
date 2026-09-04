"""ADR 009 Phase B — versioned regdocs/ store (fetch/extract/store/index + targets)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import reg_documents as RD
from src.synth import regulatory


class _FakeS3:
    def __init__(self):
        self.store: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.store[Key] = Body if isinstance(Body, bytes) else str(Body).encode()

    def get_object(self, Bucket, Key):
        if Key not in self.store:
            raise Exception("NoSuchKey")
        return {"Body": _Body(self.store[Key])}


class _Body:
    def __init__(self, b): self._b = b
    def read(self): return self._b


DOU_HTML = """<html><body><div class="texto-dou">
<p class="dou-paragraph">O CONSELHO MONETÁRIO NACIONAL resolve:</p>
<p class="dou-paragraph">Art. 1º Esta Resolução altera a Resolução CMN 5.304.</p>
<p class="dou-paragraph">Art. 2º Esta Resolução entra em vigor em 1º de dezembro de 2026.</p>
</div><script>var x=1;</script></body></html>"""


def test_extract_text_from_dou_paragraphs():
    t = RD.extract_text(DOU_HTML)
    assert "conselho monet" in t.lower() and "nacional" in t.lower()
    assert "Art. 1" in t and "Art. 2" in t
    assert "var x=1" not in t                       # script dropped


def test_extract_text_generic_fallback():
    t = RD.extract_text("<html><style>a{}</style><body><h1>Norma</h1><p>Texto do ato.</p></body></html>")
    assert "Norma" in t and "Texto do ato." in t and "a{}" not in t


def test_content_hash_ignores_whitespace_and_case():
    assert RD.content_hash("Art. 1  FOO") == RD.content_hash("art. 1\nfoo")
    assert RD.content_hash("a") != RD.content_hash("b")


def test_store_document_is_content_hash_cached():
    s3 = _FakeS3(); index = {}
    r1 = RD.store_document("raw", "res-cmn-5336", "Art. 1 texto original", url="u", index=index, s3=s3)
    assert r1["stored"] and r1["key"] in s3.store
    # identical content -> no-op
    r2 = RD.store_document("raw", "res-cmn-5336", "Art. 1   texto original", url="u", index=index, s3=s3)
    assert r2["stored"] is False and r2["reason"] == "unchanged"
    # changed content -> a new version
    r3 = RD.store_document("raw", "res-cmn-5336", "Art. 1 texto NOVO", url="u", index=index, s3=s3)
    assert r3["stored"] and len(index["res-cmn-5336"]["versions"]) == 2


def test_sync_documents_fetches_stores_and_indexes():
    s3 = _FakeS3()
    pages = {"https://in.gov.br/a": DOU_HTML, "https://in.gov.br/b": "<p class='dou-paragraph'>"
             + ("texto suficientemente longo " * 20) + "</p>"}
    targets = [
        {"instrument_key": "res-cmn-5336", "label": "Resolução CMN 5336", "url": "https://in.gov.br/a"},
        {"instrument_key": "res-cmn-5339", "label": "Resolução CMN 5339", "url": "https://in.gov.br/b"},
        {"instrument_key": "no-url"},                       # skipped (no url)
        {"instrument_key": "short", "url": "https://in.gov.br/x"},   # skipped (too short)
    ]
    fetch = lambda u: RD.extract_text(pages.get(u, ""))
    rep = RD.sync_documents(targets, "raw", s3=s3, fetch=fetch, min_chars=100)
    assert set(rep["stored"]) == {"res-cmn-5336", "res-cmn-5339"}
    assert rep["skipped"] == 2
    # index persisted + reloadable; a second run is all-unchanged
    idx = RD.load_index("raw", s3=s3)
    assert "res-cmn-5336" in idx and idx["res-cmn-5336"]["versions"][0]["chars"] > 0
    rep2 = RD.sync_documents(targets, "raw", s3=s3, fetch=fetch, min_chars=100)
    assert rep2["stored"] == [] and rep2["unchanged"] == 2


def _narr(text, date="2026-08-20", cites=None):
    return {"entity": None, "narrative": text, "run_date": date, "lenses": ["regulatory"],
            "citations": cites or []}


def test_regdoc_targets_prefers_dou_citation():
    narrs = [_narr(
        "A Resolução CMN 5336 altera dispositivos.", cites=[
            {"url": "https://news.google.com/rss/x"},
            {"url": "https://www.in.gov.br/web/dou/-/resolucao-cmn-n-5.336-729104423"},
            {"url": "https://www.bcb.gov.br/exibenormativo?numero=5336"}])]
    tg = regulatory.regdoc_targets(narrs, as_of="2026-08-23")
    row = next(t for t in tg if t["instrument_key"] == "res-cmn-5336")
    assert "in.gov.br" in row["url"] and "5.336" in row["url"]


def test_regdoc_targets_rejects_wrong_document():
    # the only DOU citation is a DIFFERENT act (a SUSEP portaria) — store nothing, not the
    # wrong document (nexus discipline, cf. #51).
    narrs = [_narr("A Resolução BCB 585 dispõe sobre X.", cites=[
        {"url": "https://www.in.gov.br/web/dou/-/portaria-susep-n-8.186-de-21-728000000"}])]
    assert regulatory.regdoc_targets(narrs, as_of="2026-08-23") == []
