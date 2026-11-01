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


def _NO_BCB(tipo, numero):
    """BCB's API doesn't know the norm: sync falls back to the DOU page."""
    return {"text": "", "versao": None, "amendments": [], "revoked": False, "url": None}


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
    rep = RD.sync_documents(targets, "raw", s3=s3, fetch=fetch, fetch_bcb_fn=_NO_BCB,
                            min_chars=100, pause_s=0)
    assert set(rep["stored"]) == {"res-cmn-5336", "res-cmn-5339"}
    assert rep["skipped"] == 2
    # index persisted + reloadable; a second run is all-unchanged
    idx = RD.load_index("raw", s3=s3)
    assert "res-cmn-5336" in idx and idx["res-cmn-5336"]["versions"][0]["chars"] > 0
    rep2 = RD.sync_documents(targets, "raw", s3=s3, fetch=fetch, fetch_bcb_fn=_NO_BCB,
                             min_chars=100, pause_s=0)
    assert rep2["stored"] == [] and rep2["unchanged"] == 2


def test_store_second_version_computes_and_persists_a_diff():
    s3 = _FakeS3(); index = {}
    v1 = "Art. 1º O limite é de R$ 100,00.\nArt. 2º Vigência imediata."
    v2 = "Art. 1º O limite é de R$ 250,00.\nArt. 2º Vigência imediata.\nArt. 3º Nova regra."
    RD.store_document("raw", "res-cmn-5336", v1, url="u1", index=index, s3=s3)
    r2 = RD.store_document("raw", "res-cmn-5336", v2, url="u2", index=index, s3=s3)
    # the new version carries a diff vs the prior, persisted as a .diff.json
    assert r2["stored"] and "diff" in r2
    assert r2["diff"]["modified"] == 1 and r2["diff"]["added"] == 1
    assert "modifica Art. 1" in r2["diff"]["summary"]
    assert any(k.endswith(".diff.json") for k in s3.store)
    assert index["res-cmn-5336"]["versions"][1]["diff"]["vs"] == index["res-cmn-5336"]["versions"][0]["hash"]


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
    # #294: BCB's API serves Res. BCB 585, so it is still a target, with NO DOU url
    assert regulatory.regdoc_targets(narrs, as_of="2026-08-23") == [
        {"instrument_key": "res-bcb-585", "label": "Resolução BCB 585", "url": None}]
    # a regulator BCB's API doesn't serve, without its own DOU page: nothing
    narrs = [_narr("A Resolução CNSP 470 dispõe sobre X.", cites=[
        {"url": "https://www.in.gov.br/web/dou/-/portaria-susep-n-8.186-de-21-728000000"}])]
    assert regulatory.regdoc_targets(narrs, as_of="2026-08-23") == []


# --- #294: BCB's consolidated text -------------------------------------------------------

BCB_TEXTO = (
    '<div class="ExternalClassX"><p class="Epgrafe"><span>\u200bRESOLUÇÃO CMN Nº 5.330, DE 23 DE '
    'JULHO DE 2026</span></p><p><span>Dispõe sobre a contratação&#58; crédito rural.</span></p>'
    '<p><span>Art. 1º Fica autorizada a contratação.</span></p><p><span>§ 9º A linha pode manter '
    'lastro. (Redação dada pela Resolução CMN nº 5.334, de 24/8/2026.)</span></p>'
    '<p>Art. 2º Esta Resolução entra em vigor<br>na data de sua publicação.</p></div>')
ATUALIZACOES = (
    "Resolução BCB nº 155/2021 - Alteração, a partir de 1º/3/2022, produzindo efeitos a partir de "
    "1º/10/2022 - Revogação: art. 25, inciso I. ;#Resolução BCB nº 494/2025 - Nova redação: art. 9º, "
    "caput. Inclusão: arts. 9º, parágrafo único, e 9º-A.;#")


def test_bcb_target_maps_only_the_types_bcb_serves():
    assert RD.bcb_target("res-cmn-5330") == ("Resolução CMN", "5330")
    assert RD.bcb_target("res-bcb-80") == ("Resolução BCB", "80")
    assert RD.bcb_target("in-bcb-600") == ("Instrução Normativa BCB", "600")
    assert RD.bcb_target("circ-3978") == ("Circular", "3978")
    for k in ("circ-susep-700", "res-cvm-175", "res-cnsp-470", "comunicado-123", "", "res-cmn-x"):
        assert RD.bcb_target(k) is None, k


def test_html_to_text_keeps_one_block_per_line_for_the_diff():
    t = RD.html_to_text(BCB_TEXTO)
    lines = t.split("\n")
    assert lines[0] == "RESOLUÇÃO CMN Nº 5.330, DE 23 DE JULHO DE 2026"      # zero-width space gone
    assert "Dispõe sobre a contratação: crédito rural." in lines        # &#58; unescaped
    assert "Art. 1º Fica autorizada a contratação." in lines
    assert "Art. 2º Esta Resolução entra em vigor" in lines             # <br> breaks the line
    from src.synth import reg_diff
    assert [u["key"] for u in reg_diff.segment(t)] == ["preamble", "art-1", "art-2"]


def test_parse_atualizacoes_reads_act_detail_and_effective_date():
    a = RD.parse_atualizacoes(ATUALIZACOES)
    assert a == [
        {"act": "Resolução BCB nº 155/2021", "detail": "Revogação: art. 25, inciso I",
         "effective": "2022-03-01"},
        {"act": "Resolução BCB nº 494/2025",
         "detail": "Nova redação: art. 9º, caput. Inclusão: arts. 9º, parágrafo único, e 9º-A",
         "effective": None},
    ]
    assert RD.parse_atualizacoes(None) == [] and RD.parse_atualizacoes("") == []


class _Resp:
    def __init__(self, body): self._b = body
    def raise_for_status(self): pass
    def json(self): return self._b


def test_fetch_bcb_parses_the_api_reply(monkeypatch):
    seen = {}
    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params, ua=headers["User-Agent"])
        return _Resp({"conteudo": [{"Texto": BCB_TEXTO, "VersaoNormativo": "2.0",
                                    "Atualizacoes": ATUALIZACOES, "Revogado": False, "Cancelado": False}]})
    monkeypatch.setattr(RD.requests, "get", fake_get)
    d = RD.fetch_bcb("Resolução CMN", "5330")
    assert seen["url"] == RD.BCB_API and seen["params"] == {"p1": "Resolução CMN", "p2": "5330"}
    assert seen["ua"].startswith("Onca-CI/")                           # not a spoofed browser
    assert d["versao"] == "2.0" and len(d["amendments"]) == 2 and d["revoked"] is False
    assert "Art. 1º" in d["text"]
    assert d["url"].startswith("https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?")
    monkeypatch.setattr(RD.requests, "get", lambda *a, **k: _Resp({"conteudo": []}))
    assert RD.fetch_bcb("Resolução CMN", "1")["text"] == ""            # unknown norm


def _bcb(versions):
    """A fake BCB API that returns the next text for each call."""
    calls = []
    def f(tipo, numero):
        calls.append((tipo, numero))
        text = versions[min(len(calls), len(versions)) - 1]
        return {"text": text, "versao": f"{len(calls)}.0", "url": "https://www.bcb.gov.br/x",
                "amendments": [{"act": "Resolução CMN nº 5.334/2026", "detail": "Nova redação: art. 1º",
                                "effective": None}], "revoked": False}
    f.calls = calls
    return f


V1 = "Art. 1º O limite é de R$ 100,00 para cada operação contratada.\nArt. 2º Vigência imediata " + "x" * 200
V2 = "Art. 1º O limite é de R$ 250,00 para cada operação contratada.\nArt. 2º Vigência imediata " + "x" * 200
TGT = [{"instrument_key": "res-cmn-5330", "label": "Resolução CMN 5330", "url": None}]


def test_sync_bcb_stores_consolidated_text_and_refreshes_the_bcb_block():
    s3 = _FakeS3()
    f = _bcb([V1, V1])
    rep = RD.sync_documents(TGT, "raw", s3=s3, fetch_bcb_fn=f, pause_s=0, as_of="2026-11-01")
    assert rep["stored"] == ["res-cmn-5330"] and f.calls == [("Resolução CMN", "5330")]
    rec = rep["index"]["res-cmn-5330"]
    assert rec["versions"][0]["source"] == "bcb" and rec["versions"][0]["versao"] == "1.0"
    assert rec["bcb"]["versao"] == "1.0" and rec["bcb"]["amendments"][0]["act"].startswith("Resolução CMN")
    rep2 = RD.sync_documents(TGT, "raw", s3=s3, fetch_bcb_fn=f, pause_s=0, as_of="2026-11-02")
    assert rep2["unchanged"] == 1
    assert rep2["index"]["res-cmn-5330"]["bcb"]["fetched"] == "2026-11-02"   # refreshed anyway


def test_sync_bcb_new_version_diffs_against_the_prior_bcb_version_only():
    s3 = _FakeS3()
    # a legacy DOU version (written before #294) must not be diffed against BCB's text
    idx = {}
    RD.store_document("raw", "res-cmn-5330", "Texto como publicado no DOU " + "y" * 300,
                      url="https://in.gov.br/a", index=idx, s3=s3)
    RD.publish_index(idx, "raw", s3=s3)
    f = _bcb([V1, V2])
    r1 = RD.sync_documents(TGT, "raw", s3=s3, fetch_bcb_fn=f, pause_s=0, as_of="2026-11-01")
    v = r1["index"]["res-cmn-5330"]["versions"]
    assert len(v) == 2 and "diff" not in v[1]                       # first BCB version: no diff
    r2 = RD.sync_documents(TGT, "raw", s3=s3, fetch_bcb_fn=f, pause_s=0, as_of="2026-11-05")
    v = r2["index"]["res-cmn-5330"]["versions"]
    assert len(v) == 3 and v[2]["diff"]["vs"] == v[1]["hash"]       # vs the BCB one, not DOU
    assert "modifica Art. 1" in v[2]["diff"]["summary"]


def test_sync_falls_back_to_dou_when_bcb_does_not_know_the_norm():
    s3 = _FakeS3()
    tgt = [{"instrument_key": "res-cmn-5336", "label": "x", "url": "https://in.gov.br/a"}]
    rep = RD.sync_documents(tgt, "raw", s3=s3, fetch=lambda u: RD.extract_text(DOU_HTML) + " " + "z" * 300,
                            fetch_bcb_fn=_NO_BCB, pause_s=0)
    rec = rep["index"]["res-cmn-5336"]
    assert rep["stored"] == ["res-cmn-5336"] and rec["versions"][0]["source"] == "dou"
    assert "bcb" not in rec


def test_sync_is_time_boxed_and_paced():
    s3 = _FakeS3()
    t = iter([0, 0, 50, 200, 200, 200])                 # start, then one reading per target
    slept = []
    tg = [{"instrument_key": f"res-cmn-{n}", "label": "x", "url": None} for n in (1, 2, 3)]
    rep = RD.sync_documents(tg, "raw", s3=s3, fetch_bcb_fn=_bcb([V1]), budget_s=120,
                            pause_s=0.5, clock=lambda: next(t), sleep=slept.append)
    assert len(rep["stored"]) == 2 and rep["deferred"] == 1     # the third waits for next run
    assert slept == [0.5]                                       # a pause between requests only
