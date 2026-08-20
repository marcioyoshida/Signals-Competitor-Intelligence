import csv
import datetime as dt
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import cvm_ipe

COLS = [
    "CNPJ_Companhia", "Nome_Companhia", "Codigo_CVM", "Data_Referencia",
    "Categoria", "Tipo", "Especie", "Assunto", "Data_Entrega",
    "Tipo_Apresentacao", "Protocolo_Entrega", "Versao", "Link_Download",
]


def _zip(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLS, delimiter=";")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in COLS})
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("ipe_cia_aberta_2026.csv", buf.getvalue().encode("latin-1"))
    return z.getvalue()


def _row(company, categoria, assunto, data, proto, link="https://rad.cvm.gov.br/x"):
    return {
        "CNPJ_Companhia": "60.872.504/0001-23", "Nome_Companhia": company,
        "Categoria": categoria, "Assunto": assunto, "Data_Entrega": data,
        "Protocolo_Entrega": proto, "Link_Download": link, "Especie": "",
    }


def test_fetch_filters_category_date_and_watchlist():
    today = dt.date(2026, 8, 16)
    rows = [
        _row("ITAU UNIBANCO HOLDING S.A.", "Fato Relevante", "Aquisição", "2026-08-10", "P1"),
        _row("ITAU UNIBANCO HOLDING S.A.", "Assembleia", "AGO", "2026-08-11", "P2"),          # noise category
        _row("ITAU UNIBANCO HOLDING S.A.", "Comunicado ao Mercado", "Resultado", "2026-01-01", "P3"),  # too old
        _row("PETROBRAS S.A.", "Fato Relevante", "Dividendos", "2026-08-12", "P4"),           # off-watchlist
    ]
    facts = cvm_ipe.fetch_material_facts(
        lookback_days=30, watchlist=["ITAU UNIBANCO"],
        today=today, fetcher=lambda y: _zip(rows) if y == 2026 else None,
    )
    ids = {f["id"] for f in facts}
    assert ids == {"cvm-fato:P1"}                        # only the on-watchlist, in-window, strategic row
    f = facts[0]
    assert f["source"] == "CVM-FatoRelevante"
    assert f["category"] == "Fato Relevante"
    assert f["subject"] == "Aquisição"
    assert f["company"].startswith("ITAU UNIBANCO")
    assert f["url"] == "https://rad.cvm.gov.br/x"


def test_empty_watchlist_keeps_all_strategic():
    today = dt.date(2026, 8, 16)
    rows = [
        _row("A S.A.", "Fato Relevante", "x", "2026-08-10", "P1"),
        _row("B S.A.", "Comunicado ao Mercado", "y", "2026-08-11", "P2"),
        _row("C S.A.", "Reunião da Administração", "z", "2026-08-11", "P3"),  # noise
    ]
    facts = cvm_ipe.fetch_material_facts(
        lookback_days=30, watchlist=None, today=today,
        fetcher=lambda y: _zip(rows) if y == 2026 else None,
    )
    assert {f["id"] for f in facts} == {"cvm-fato:P1", "cvm-fato:P2"}


def test_crosses_year_boundary_fetches_prior_year():
    today = dt.date(2026, 1, 10)
    calls = []
    cvm_ipe.fetch_material_facts(
        lookback_days=30, today=today,
        fetcher=lambda y: calls.append(y) or None,
    )
    assert set(calls) == {2025, 2026}  # 30-day window from Jan 10 spans 2025


def test_is_governance_classifier():
    g = cvm_ipe.is_governance
    # governance events (control / board / executive / statute / auditor)
    assert g("Alteração de Controle Acionário")
    assert g("Eleição de membros do Conselho de Administração")
    assert g("Renúncia do Diretor Presidente")
    assert g("Celebração de Acordo de Acionistas")
    assert g("Substituição dos Auditores Independentes")
    assert g("Reforma do Estatuto Social")
    # non-governance material facts
    assert not g("Aquisição de carteira de crédito")
    assert not g("Distribuição de dividendos")
    assert not g("Lançamento de novo produto de pagamentos")
    assert not g("Remuneração Complementar aos Acionistas 2T26")  # dividends, not governance
    assert not g("Remuneração dos Acionistas — Juros sobre Capital Próprio")


def test_normalize_tags_governance_records():
    row = {
        "Nome_Companhia": "Banco X S.A.", "Categoria": "Fato Relevante",
        "Assunto": "Renúncia de Diretor", "Protocolo_Entrega": "P9",
        "Data_Entrega": "2026-08-15", "Link_Download": "https://rad.cvm.gov.br/x",
    }
    rec = cvm_ipe._normalize(row)
    assert rec["governance"] is True and rec["topic"] == "governance"
    row2 = {**row, "Assunto": "Emissão de debêntures", "Protocolo_Entrega": "P10"}
    rec2 = cvm_ipe._normalize(row2)
    assert rec2["governance"] is False and rec2["topic"] is None
