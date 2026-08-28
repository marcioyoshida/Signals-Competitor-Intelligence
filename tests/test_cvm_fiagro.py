"""Tests for CVM FIAGRO informe ingest + ticker-from-ISIN."""
from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock, patch

from src.ingest.cvm_fiagro import (
    _ticker_from_isin,
    fetch_fiagro,
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
