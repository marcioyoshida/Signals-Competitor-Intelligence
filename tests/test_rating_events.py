"""#332 rating-action events: subjects of the banks' own CVM filings + link-only agency pointers."""
import json
from pathlib import Path

import pytest

from src.ingest import rating_events as r

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("subject, agency, action, ratings, outlook", [
    # real subjects from the 2014–2026 IPE history (probed 2026-11-16)
    ("Fitch Ratings atribui ratings ao Banco Sofisa S/A", "fitch", "assign", [], None),
    ("Standard and Poor's reafirma os ratings do Banco Pine", "sp", "affirm", [], None),
    ("S&ampP eleva os ratings do Banco Pine", "sp", "upgrade", [], None),
    ("Elevação de Rating - S&ampP", "sp", "upgrade", [], None),
    ("ELEVAÇÃO DE RATING PELA S&ampP PARA 'brA+'", "sp", "upgrade", ["brA+"], None),
    ("ELEVAÇÃO DE RATING PELA MOODY`S PARA 'A.br'", "moodys", "upgrade", ["A.br"], None),
    ("ELEVAÇÃO DE RATING PELA MOODY'S PARA  A+.br", "moodys", "upgrade", ["A+.br"], None),
    ("Fitch confirma ratings do Banrisul em BB- e AA+(bra), com perspectiva estável",
     "fitch", "affirm", ["BB-", "AA+(bra)"], "stable"),
    ("Fitch Afirma Ratings 'AAA(bra)' da Cielo Perspectiva Estável", "fitch", "affirm", ["AAA(bra)"], "stable"),
    ("ELEVAÇÃO DA PERSPECTIVA DO RATING NACIONAL DE LONGO PRAZO", None, "outlook", [], None),
    ("Alterac¸a~o Perspectiva Rating Moodys", "moodys", "outlook", [], None),
    ("Revisão Perspectiva Rating Moodys Local", "moodys", "outlook", [], None),
    ("Fato Relevante - Atribuição de Rating Moody's BR", "moodys", "assign", [], None),   # not "eleva"
    ("Comunicado ao Mercado - Retirada de Rating de Crédito na escala global - S&ampP", "sp", "withdraw", [], None),
    ("Revisão dos IDRs pela Fitch Ratings", "fitch", "review", [], None),
    ("Relatório de Monitoramento de Rating do 2º Trimestre de 2014", None, "report", [], None),
    ("Moodys Elevou o ratings do Banco da Amazônia", "moodys", "upgrade", [], None),
])
def test_parse_subject(subject, agency, action, ratings, outlook):
    p = r.parse_subject(subject)
    assert (p["agency"], p["action"], p["ratings_named"], p["outlook"]) == (agency, action, ratings, outlook)


@pytest.mark.parametrize("subject", ["Live - Fitch Ratings", "Abertura de nova agência",
                                     "Fato Relevante - Aquisição de participação", "Eleição de Diretor(es)"])
def test_not_rating_actions(subject):
    assert r.parse_subject(subject) is None


def test_scale():
    assert [r.scale_of(x) for x in ("brA+", "AA+(bra)", "A.br", "BB-", "Ba2", None)] == [
        "national", "national", "national", "global", "global", None]


ROWS = [
    {"doc_class": "comunicado", "subject": "ELEVAÇÃO DE RATING PELA S&ampP PARA 'brA'", "protocol": "1",
     "reference_date": "2024-10-15", "date": "2024-10-16", "entity_id": "banco_pine", "cnpj_root": "62144175",
     "company": "BANCO PINE", "url": "https://www.rad.cvm.gov.br/x?p=1"},
    {"doc_class": "comunicado", "subject": "Fitch Ratings eleva ratings do Banco Inter", "protocol": "2",
     "reference_date": "2018-11-16", "date": "2018-11-16", "entity_id": "inter", "url": "u2"},
    {"doc_class": "ata_rca", "subject": "Elevação do Rating", "protocol": "3", "date": "2020-01-01"},
    {"doc_class": "comunicado", "subject": "Fitch Afirma Ratings 'AAA(bra)' da Cielo Perspectiva Estável",
     "protocol": "4", "date": "2026-09-09", "entity_id": "cielo", "url": "u4"},
]


def test_from_ipe_uses_only_triggers_and_flags_what_needs_the_filing():
    ev = {e["protocol"]: e for e in r.from_ipe(ROWS)}
    assert set(ev) == {"1", "2", "4"}                                  # board minutes are not events
    assert ev["1"]["new_rating"] == "brA" and ev["1"]["scale"] == "national" and ev["1"]["date"] == "2024-10-15"
    assert ev["1"]["needs_document"] is True                          # outlook unknown
    assert ev["2"]["needs_document"] is True and ev["2"]["new_rating"] is None
    assert ev["4"]["needs_document"] is False                         # rating + outlook in the subject
    assert ev["2"]["source"] == "cvm-ipe" and ev["2"]["id"] == "rating:cvm:2"


GOOD = {"entity_id": "bradesco", "agency": "sp", "date": "2015-09-10", "action": "downgrade",
        "old_rating": "BBB-", "new_rating": "BB+", "outlook": "negative",
        "url": "https://disclosure.spglobal.com/ratings/en/regulatory/article/-/view/sourceId/1"}


def test_pointer_validation_keeps_them_link_only():
    assert r.validate_pointer(GOOD) == []
    assert any("own domain" in e for e in r.validate_pointer({**GOOD, "url": "https://news.example.com/x"}))
    assert any("own domain" in e for e in r.validate_pointer({**GOOD, "url": "https://spglobal.com.evil.io/x"}))
    assert any("no agency text" in e for e in r.validate_pointer({**GOOD, "text": "release body"}))
    assert any("agency" in e for e in r.validate_pointer({**GOOD, "agency": "austin"}))
    assert any("date" in e for e in r.validate_pointer({**GOOD, "date": "10/09/2015"}))
    with pytest.raises(ValueError):
        r.from_pointers([{**GOOD, "url": "https://x.org"}])


def test_build_merges_sorts_and_counts_and_replay_reads_by_entity():
    store = r.build(ROWS, [GOOD], "2026-11-16T00:00:00+00:00")
    assert store["schema"] == r.SCHEMA and store["count"] == 4
    assert [e["date"] for e in store["events"]] == sorted(e["date"] for e in store["events"])
    assert store["by_action"] == {"upgrade": 2, "affirm": 1, "downgrade": 1}
    p = r.ratings_for(store, "bradesco", "2015-01-01", "2015-12-31")
    assert len(p) == 1 and p[0]["source"] == "agency-pointer" and p[0]["scale"] == "global"


def test_the_committed_pointer_file_is_valid():
    doc = json.loads((ROOT / "config" / "rating_pointers.json").read_text(encoding="utf-8"))
    r.from_pointers(doc["pointers"])                                    # raises on any bad row
