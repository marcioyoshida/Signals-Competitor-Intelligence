"""BCB Financial Stability Report (REF) into the KB (#337)."""
from __future__ import annotations

import json

from src.ingest import ref_history as rh


def _body(n: int, words: str = "texto corrido sobre o sistema financeiro. ") -> str:
    return f"Outubro 2015  |  Relatório de Estabilidade Financeira  |  {n}\n" + words * 12


def _edition_2015() -> list[str]:
    """A 2015-style PDF: cover, credits, two contents pages, then printed page n at index n."""
    toc1 = ("Sumário\nPrefácio 5\nSumário executivo 6\n1 Ambiente macroeconômico e financeiro 8\n"
            "1.1 Introdução  ______________  8\n1.2\tMercados\tfinanceiros  ______  10\n2 Sistema financeiro 12\n"
            "2.1 Introdução  ____________  12\n2.2 Liquidez  ____________  13\n"
            "2.3\tTestes\tde\testresse\tde\tcapital  ________  15\n"
            "\t 2.3.1\t Análise\tde\tcenário  ________  15\n2.4 Shadow banking  ________  18\n")
    toc2 = ("Boxes\nÍndice de Liquidez Estrutural __________ 14\nApêndice 19\n"
            "Conceitos e Metodologias – Estresse de capital ______ 20\nAnexo 21\nOutros 21\nMais 21\n")
    pages = ["Relatório de \nEstabilidade Financeira\nOutubro 2015\nVolume 14  |  Número 2",
             "Publicação semestral", toc1, toc2]
    while len(pages) < 23:
        n = len(pages)
        text = _body(n)
        if n == 15:
            text = _body(n, "os testes de estresse mostram capital suficiente. ")
        pages.append(text)
    return pages


def test_printed_number_layouts():
    assert rh.printed_number("12  |  Relatório de Estabilidade Financeira  |  Novembro 2008\nx") == 12
    assert rh.printed_number("Março 2012  |  Relatório de Estabilidade Financeira  |  30\nx") == 30
    assert rh.printed_number("Anexo estatístico13\nparticular") == 13
    assert rh.printed_number("Temas Selecionados, 59\n2.1 Atuação") is None
    assert rh.printed_number("Relatório de \nEstabilidade") is None


def test_page_offset_majority():
    pages = ["capa", "x"] + [f"{i + 1} | Relatório de Estabilidade Financeira | Maio 2009\nt" for i in range(2, 10)]
    assert rh.page_offset(pages) == -1
    assert rh.page_offset(["a", "b"]) is None


def test_clean_ligatures_tabs_hyphens():
    assert rh.clean("ﬁ nanceiro\tdo sis-\ntema") == "financeiro do sistema"


def test_toc_entries_wrapped_and_comma_layout():
    ents = rh.toc_entries(["Sumário\nPrefácio, 4\nAvaliação dos Sistemas Financeiros \nNacional e Internacional, 10 \n"
                           "1.1 Sistema Financeiro Internacional, 11\n 1.2.5 Testes de estresse, 47\n"
                           "Boxe 4 – Descrição dos cenários utilizados no teste \nde estresse macroeconômico, 48\n"])
    assert {"num": None, "title": "Avaliação dos Sistemas Financeiros Nacional e Internacional", "page": 10} in ents
    assert {"num": "1.2.5", "title": "Testes de estresse", "page": 47} in ents
    assert any(e["title"].startswith("Boxe 4") and e["page"] == 48 for e in ents)


def test_toc_header_line_does_not_glue_onto_numbered_entry():
    ents = rh.toc_entries(["Boxes\n2.1 Introdução ____ 12\n"])
    assert ents == [{"num": "2.1", "title": "Introdução", "page": 12}]


def test_edition_sections_from_toc():
    ed = rh.edition("201510", rh.url("201510", "refPub"), _edition_2015())
    assert ed["parse_mode"] == "toc"
    assert ed["edition"] == "2015-10" and ed["date"] == "2015-10-31" and ed["volume"] == "v. 14, n. 2"
    by = {s["label"]: s for s in ed["sections"]}
    stress = by["2 Sistema financeiro › 2.3 Testes de estresse de capital"]
    assert stress["stress"] and stress["pdf_pages"] == [16, 18]          # printed 15–17, unsplit by 2.3.1
    assert "1 Ambiente macroeconômico e financeiro › 1.1 Introdução" in by    # same-page chapter merged
    assert "Índice de Liquidez Estrutural" not in by                           # box inside the span dropped
    assert by["Conceitos e Metodologias – Estresse de capital"]["stress"]
    assert not by["2 Sistema financeiro › 2.4 Shadow banking"]["stress"]
    keys = [s["key"] for s in ed["sections"]]
    assert len(set(keys)) == len(keys) and all(k.startswith("BCB-REF/2015-10/") for k in keys)


def test_depth3_stress_and_box_bounded_by_next_entry():
    toc = ("Sumário\nPrefácio 3\n1.1 Liquidez 4\n1.2 Sistema Financeiro Nacional 6\n 1.2.4 Solvência 7\n"
           " 1.2.5 Testes de estresse 9\n 1.2.6 Outros 11\n1.3 Pesquisa 13\nBoxe – Clima 8\nConceitos 15\n")
    pages = ["capa", toc] + [f"Anexo estatístico{i + 1}\n" + "texto " * 60 for i in range(1, 16)]
    secs, mode = rh.sections(pages)
    assert mode == "toc"
    labels = {s["label"]: (s["start"], s["end"]) for s in secs}
    assert labels["1.2.5 Testes de estresse"] == (9, 10)                      # printed 9–10, stops at 1.2.6
    assert labels["1.2 Sistema Financeiro Nacional (continuação)"] == (11, 12)
    assert "Boxe – Clima" not in labels                                      # not a stress box


def test_partial_toc_windows_before_first_section():
    toc = ("Temas Selecionados, 30\n2.1 Atuação do BC, 31\n2.2 Risco climático, 33\n2.3 Crises, 35\n"
           "Apêndice, 37\nSiglas, 38\nConceitos e Metodologias, 39\nOutros, 40\n")
    pages = ["capa", "créditos", toc] + [f"Anexo estatístico{i}\n" + "texto " * 60 for i in range(3, 41)]
    secs, mode = rh.sections(pages)
    assert mode == "partial"
    assert secs[0]["label"].startswith("páginas 1–") and secs[0]["num"] is None
    assert any(s["label"] == "2.1 Atuação do BC" for s in secs)


def test_windows_fallback_marks_stress_by_text():
    pages = [("o teste de estresse " * 4) + "x" * 300] + ["sem cabeçalho " * 30] * 9
    ed = rh.edition("200811", "u", pages)
    assert ed["parse_mode"] == "windows"
    assert ed["sections"][0]["stress"] and ed["sections"][0]["stress_basis"] == "text"
    assert ed["edition"] == "2008-11"                                         # folder month fallback


def test_cover_month_beats_folder():
    assert rh.cover_month(["Volume 24 | N. 2 | Novembro 2025"], "202510") == "2025-11"
    assert rh.cover_month(["Brasília v. 7 n° 2 nov. 2008 p. 1–174"], "200811") == "2008-11"
    assert rh.month_end("2024-02") == "2024-02-29"


def test_kb_document_and_metadata():
    ed = rh.edition("201510", rh.url("201510", "refPub"), _edition_2015())
    sec = next(s for s in ed["sections"] if s["stress"])
    doc = rh.kb_document(ed, sec)
    assert doc.startswith("Relatório de Estabilidade Financeira (REF)") and not doc.startswith("From ")
    assert "Seção: 2 Sistema financeiro › 2.3 Testes de estresse de capital" in doc
    assert "| 15" not in doc                                                  # running header stripped
    meta = rh.kb_metadata(ed, sec)
    assert meta["stress"] is True and meta["date"] == "2015-10-31" and meta["source"] == "BCB-REF"
    assert meta["title"].startswith("REF out/2015 (BCB) — ")
    assert "entity" not in meta                                               # system-level, not an entity
    assert len(json.dumps({"metadataAttributes": meta}, ensure_ascii=False).encode()) < 2048
    plain = next(s for s in ed["sections"] if not s["stress"])
    assert "stress" not in rh.kb_metadata(ed, plain)


class _S3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def get_paginator(self, _name):
        s3 = self

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in sorted(s3.objects) if k.startswith(Prefix)]}
        return P()

    def put_object(self, Bucket, Key, Body, **_kw):
        self.objects[Key] = Body

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.objects.pop(o["Key"], None)

    def get_object(self, Bucket, Key):
        import io
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objects[Key])}


def test_write_edition_skip_and_force_replace():
    s3 = _S3()
    ed = rh.edition("201510", "u", _edition_2015())
    n = rh.write_edition(s3, "raw", ed)
    assert n == len(ed["sections"]) and len(s3.objects) == 2 * n
    sidecar = json.loads(s3.objects[ed["sections"][0]["key"] + ".metadata.json"])
    assert sidecar["metadataAttributes"]["edition"] == "2015-10"
    assert rh.write_edition(s3, "raw", ed) == 0                               # seen: skipped
    s3.objects["BCB-REF/2015-10/99-stale.txt"] = b"old cut"
    rh.write_edition(s3, "raw", ed, force=True)
    assert "BCB-REF/2015-10/99-stale.txt" not in s3.objects                   # re-cut leaves nothing stale


def test_manifest_and_point_in_time_reads():
    s3 = _S3()
    eds = [rh.manifest_entry(rh.edition(f, "u", _edition_2015())) for f in ("201510",)]
    eds.append({**eds[0], "folder": "201604", "edition": "2016-04", "date": "2016-04-30"})
    doc = rh.put_manifest(s3, "dig", eds, "2026-11-20T00:00:00+00:00")
    assert all("text" not in s for e in doc["editions"] for s in e["sections"])
    m = rh.load_manifest(s3, "dig")
    assert rh.edition_at(m, "2016-04-29")["edition"] == "2015-10"            # not yet published
    assert rh.edition_at(m, "2016-05-02")["edition"] == "2016-04"
    assert rh.edition_at(m, "2015-01-01") is None
    st = rh.stress_at(m, "2016-01-15")
    assert st["edition"] == "2015-10" and st["sections"] and all(s["stress"] for s in st["sections"])


def test_discover_tries_both_suffixes():
    seen = []

    def head(u):
        seen.append(u)
        return 200 if u.endswith("201004-refP.pdf") else 404
    found = rh.discover(["201004", "201005"], head=head, pause=0)
    assert found == {"201004": rh.url("201004", "refP")}
    assert len(seen) == 4
    assert rh.months("202511", "202602") == ["202511", "202512", "202601", "202602"]
