"""Tests for CVM FIAGRO informe ingest + ticker-from-ISIN."""
from __future__ import annotations

import datetime as dt
import io
import zipfile
from unittest.mock import MagicMock, patch

from src.ingest.cvm_fiagro import (
    _ticker_from_isin,
    fetch_fiagro,
    for_cotista_moves,
    for_new_registrations,
    for_pl_moves,
    latest_yyyymm,
)


def test_ticker_from_isin():
    assert _ticker_from_isin("BRKNCACTF014") == "KNCA11"
    # Live ISIN BRXPAGCTF005 encodes root XPAG (market often shows XPCA11;
    # the derived form is still a distinctive alias — curator can set ticker).
    assert _ticker_from_isin("BRXPAGCTF005") == "XPAG11"
    assert _ticker_from_isin("BRVGIACTF004") == "VGIA11"
    assert _ticker_from_isin("BRRURAR01M16") == "RURA11"
    assert _ticker_from_isin("") is None
    assert _ticker_from_isin(None) is None
    assert _ticker_from_isin("US0378331005") is None  # not BR


def test_latest_yyyymm_shape():
    y = latest_yyyymm()
    assert len(y) == 6 and y.isdigit()


def _fixture_zip_bytes() -> bytes:
    csv_body = (
        "CNPJ_Classe;Nome_Classe;Data_Referencia;Data_Entrega;Versao;Classe_Unica;"
        "CNPJ_Administrador;Nome_Administrador;Data_Registro;Publico_Alvo;Codigo_ISIN;"
        "Cotistas_Vinculo_Familiar;Regra_Anexo;Classificacao_Autorregulada;Prazo_Duracao;"
        "Encerramento_Exercicio_Social;Mercado_Negociacao;Entidade_Administradora;"
        "Email_Administrador;Servico_Atendimento_Cotista;Site;Nome_Gestor;CNPJ_Gestor;"
        "Numero_Cotistas;Valor_Ativo;Patrimonio_Liquido;Cotas_Emitidas;Valor_Patrimonial_Cotas\n"
        "41745701000137;KINEA CREDITO AGRO FIAGRO-IMOBILIARIO;2026-03-01;2026-04-10;1;S;"
        "60701190000104;INTRAG DTVM;2021-01-15;INVESTIDORES EM GERAL;BRKNCACTF014;"
        "N;;;0 DIA/DIAS;31/12;BOLSA;;a@b.com;a@b.com;www.x.com;KINEA;60701190000104;"
        "50000;2200000000;2185411394.09;100000000;21.85\n"
        "41081088000109;VALORA CRA FIAGRO;2026-03-01;2026-04-10;1;S;"
        "07559989000117;VALORA;2021-08-17;INVESTIDORES EM GERAL;BRVGIACTF004;"
        "N;;;0 DIA/DIAS;31/12;BOLSA;;a@b.com;a@b.com;www.x.com;VALORA;07559989000117;"
        "170000;1100000000;1024520468.78;106008140;9.69\n"
        # tiny vehicle — dropped by min_pl
        "99999999000100;TINY TEST FIAGRO;2026-03-01;2026-04-10;1;S;"
        "111;ADMIN;2020-01-01;QUALIFICADO;;"
        "N;;;0;31/12;BALCAO;;a@b.com;a@b.com;www.x.com;G;111;"
        "1;1000;500.0;10;50\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("inf_mensal_fiagro_202603.csv", csv_body.encode("latin-1"))
    return buf.getvalue()


def test_fetch_fiagro_parses_fixture():
    mock_resp = MagicMock()
    mock_resp.content = _fixture_zip_bytes()
    mock_resp.raise_for_status = MagicMock()

    with patch("src.ingest.cvm_fiagro.requests.get", return_value=mock_resp):
        rows = fetch_fiagro(yyyymm="202603", min_pl=1_000_000.0)

    assert len(rows) == 2
    by_ticker = {r["ticker"]: r for r in rows}
    assert "KNCA11" in by_ticker
    assert "VGIA11" in by_ticker
    assert by_ticker["KNCA11"]["cnpj"] == "41745701000137"
    assert by_ticker["KNCA11"]["industry"] == "agri-funds"
    assert by_ticker["KNCA11"]["pl"] > 2e9
    assert by_ticker["VGIA11"]["fund_name"].startswith("VALORA")
    # tiny dropped
    assert all(r["cnpj"] != "99999999000100" for r in rows)


# ── Move detection (task b) ─────────────────────────────────────────────────

def _fund(cnpj, name, pl, cotistas, registered="2020-01-01", **extra):
    return {
        "id": f"cvm:fiagro:{cnpj}",
        "cnpj": cnpj,
        "fund_name": name,
        "ticker": extra.pop("ticker", None),
        "admin": "ADMIN X",
        "manager": "GESTORA Y",
        "pl": pl,
        "cotistas": cotistas,
        "registered": registered,
        "url": "https://dados.cvm.gov.br/dataset/fiagro-doc-inf_mensal",
        "yyyymm": extra.pop("yyyymm", "202607"),
        "as_of": "2026-07-01",
        "industry": "agri-funds",
        **extra,
    }


def test_for_pl_moves_and_cotista_moves_filter_shape():
    rows = [
        _fund("1", "A", pl=100.0, cotistas=50),
        _fund("2", "B", pl=None, cotistas=10),  # no PL -> dropped from PL series
        _fund("3", "C", pl=200.0, cotistas=None),  # no cotistas -> dropped from that series
        _fund("4", "D", pl=300.0, cotistas=5),  # below COTISTA_MIN_BASE(20) -> dropped
    ]
    pl_series = for_pl_moves(rows)
    assert {r["cnpj"] for r in pl_series} == {"1", "3", "4"}
    assert all(r["event"] == "pl_move" for r in pl_series)

    cot_series = for_cotista_moves(rows)
    assert {r["cnpj"] for r in cot_series} == {"1"}
    assert "pl" not in cot_series[0]  # stripped so narrative can't mix up the metric
    assert cot_series[0]["event"] == "cotista_move"


def test_month_over_month_pl_move_two_fixture(tmp_path, monkeypatch):
    """Two competency months → detect_moves flags the fund whose PL jumped."""
    from src.diff import engine

    monkeypatch.setattr(engine, "STATE_DIR", tmp_path)
    from src.diff.engine import detect_moves

    prev_month = [
        _fund("11111111000100", "STEADY FIAGRO", pl=100_000_000.0, cotistas=50, yyyymm="202606"),
        _fund("22222222000100", "GROWER FIAGRO", pl=100_000_000.0, cotistas=50, yyyymm="202606"),
    ]
    curr_month = [
        _fund("11111111000100", "STEADY FIAGRO", pl=104_000_000.0, cotistas=50, yyyymm="202607"),
        _fund("22222222000100", "GROWER FIAGRO", pl=140_000_000.0, cotistas=50, yyyymm="202607"),
    ]

    # First run (prev month) only seeds the baseline — no prior value to diff.
    seeded = detect_moves(
        "fiagro_pl_test", for_pl_moves(prev_month),
        key_field="cnpj", value_field="pl", min_pct=15.0,
    )
    assert seeded == []

    moves = detect_moves(
        "fiagro_pl_test", for_pl_moves(curr_month),
        key_field="cnpj", value_field="pl", min_pct=15.0,
    )
    assert len(moves) == 1
    assert moves[0]["cnpj"] == "22222222000100"
    assert moves[0]["fund_name"] == "GROWER FIAGRO"
    assert moves[0]["prev_value"] == 100_000_000.0
    assert moves[0]["pct_change"] == 40.0
    assert moves[0]["url"].startswith("https://dados.cvm.gov.br")
    # STEADY (+4%) never crosses the 15% threshold.
    assert all(m["cnpj"] != "11111111000100" for m in moves)


def test_new_registrations_lookback_window():
    today = dt.date(2026, 8, 29)
    rows = [
        _fund("1", "OLD FUND", pl=60e6, cotistas=50, registered="2021-01-01"),
        _fund("2", "FRESH FUND", pl=60e6, cotistas=50, registered="2026-07-08"),
        _fund("3", "NO REG DATE", pl=60e6, cotistas=50, registered=None),
    ]
    fresh = for_new_registrations(rows, as_of=today, lookback_days=60)
    assert [r["cnpj"] for r in fresh] == ["2"]
    assert fresh[0]["id"] == "fiagro:newreg:2"
    assert fresh[0]["event"] == "new_registration"


def test_new_registration_dedupes_via_detect_new_state(tmp_path, monkeypatch):
    """A fund alerted once as 'new' must not resurface every month it still
    falls inside the lookback window — detect_new's seen-set (keyed by the
    cnpj-scoped id) is what prevents that, not the window itself. (The
    additional first-run seed suppression is layered on by the ingest
    handler's ``_new_since_last_run(..., seed_if_empty=True)``, not by
    ``detect_new`` itself.)"""
    from src.diff import engine

    monkeypatch.setattr(engine, "STATE_DIR", tmp_path)
    from src.diff.engine import detect_new

    today = dt.date(2026, 8, 29)
    rows = [_fund("42", "NEW CLASS", pl=60e6, cotistas=50, registered="2026-07-08")]
    candidates = for_new_registrations(rows, as_of=today, lookback_days=60)

    first_run = detect_new("fiagro_newreg_test", candidates)
    assert [c["cnpj"] for c in first_run] == ["42"]

    # Next run: the fund is still inside the 60-day window (same input), but
    # it was already seen — must not repeat as "new".
    second_run = detect_new("fiagro_newreg_test", candidates)
    assert second_run == []
