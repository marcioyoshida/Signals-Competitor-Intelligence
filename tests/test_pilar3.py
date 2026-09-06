"""ADR 022 Phase 6 — Pilar 3 risk-report ingestion (offline)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import pilar3


def test_is_pilar3_requires_both_terms():
    assert pilar3.is_pilar3("Gerenciamento de Riscos e Capital - Pilar 3 (2T26)")
    assert not pilar3.is_pilar3("Decisão de transferência de Produção de Argentina (Pilar) para Brasil")
    assert not pilar3.is_pilar3("Origem Energia Pilar S.A.")
    assert not pilar3.is_pilar3("Gerenciamento de Riscos")   # no 'pilar'


def test_find_filings_filters_noise():
    rows = [
        {"subject": "Gerenciamento de Riscos e Capital - Pilar 3 (2T26)", "company": "ITAU"},
        {"subject": "Inauguração do Pilar Centro Médico", "company": "HOSPITAL"},
    ]
    assert [r["company"] for r in pilar3.find_filings(rows)] == ["ITAU"]


def test_risk_sentences_cleans_rules_and_tables():
    text = (
        "____________________ Relatório Pilar 3 ____________________\n"
        "O gerenciamento de riscos e capital do banco busca manter a solvência e a liquidez "
        "adequadas frente às exposições de crédito ao longo do ciclo econômico.\n"
        "Basileia 15,9 12,4 11,3 6,5 33,6 999 111 222 333 444 555\n"
        "curto.\n"
    )
    sents = pilar3.risk_sentences(text)
    assert len(sents) == 1
    assert "gerenciamento de riscos" in sents[0].lower()
    assert "____" not in sents[0]                       # rule-line stripped
    assert not any(s.strip().startswith("Basileia 15,9") for s in sents)  # table-dense dropped


def test_build_corpus_resolves_and_keeps_latest(monkeypatch):
    rows = [
        {"subject": "Gerenciamento de Riscos - Pilar 3", "company": "ITAU", "date": "2026-08-01",
         "url": "u-new"},
        {"subject": "Gerenciamento de Riscos - Pilar 3", "company": "ITAU", "date": "2026-05-01",
         "url": "u-old"},
    ]
    monkeypatch.setattr(pilar3, "fetch_pdf_text", lambda url, **k: f"O gerenciamento de riscos e capital referente a {url} mantém a solvência adequada frente às exposições de crédito.")
    idx = pilar3.build_corpus(rows, resolver=lambda i: ["itau"])
    assert idx["count"] == 1
    rec = idx["records"]["itau"]
    assert "u-new" in rec["sentences"][0]               # latest filing won
    assert pilar3.corpus_by_entity(idx) == {"itau": rec["sentences"]}
