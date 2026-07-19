import io
import zipfile
from datetime import date, timedelta

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import cvm_ofertas


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text.encode("latin-1"))
    return buf.getvalue()


def test_normalize_resolucao_160():
    row = {
        "Numero_Requerimento": "999",
        "Numero_Processo": "SRE/0001/2026",
        "Data_Registro": "2026-07-01",
        "Data_requerimento": "2026-06-30",
        "Status_Requerimento": "Oferta Registrada",
        "Valor_Mobiliario": "Debêntures",
        "Tipo_Oferta": "PRIMARIA",
        "Rito_Requerimento": "Automático",
        "Tipo_requerimento": "OPD",
        "Nome_Emissor": "EMPRESA X S.A.",
        "CNPJ_Emissor": "12.345.678/0001-90",
        "Nome_Lider": "BTG PACTUAL",
        "CNPJ_Lider": "30.306.294/0001-45",
        "Administrador": "",
        "Gestor": "",
        "Valor_Total_Registrado": "1000000.00",
        "Qtde_Total_Registrada": "1000",
    }
    rec = cvm_ofertas._normalize_resolucao_160(row)
    assert rec["id"] == "cvm-oferta:r160:999"
    assert rec["source"] == "CVM-Ofertas"
    assert rec["issuer"] == "EMPRESA X S.A."
    assert rec["issuer_cnpj"] == "12345678000190"
    assert rec["leader"] == "BTG PACTUAL"
    assert rec["amount"] == 1000000.0
    assert rec["event_date"] == "2026-07-01"
    assert rec["security"] == "Debêntures"


def test_fetch_recent_filters_lookback_and_watchlist(monkeypatch):
    today = date.today()
    recent = (today - timedelta(days=5)).isoformat()
    old = (today - timedelta(days=90)).isoformat()
    header = (
        "Numero_Requerimento;Rito_Requerimento;Numero_Processo;Data_requerimento;"
        "Data_Registro;Data_Encerramento;Status_Requerimento;Valor_Mobiliario;"
        "Tipo_requerimento;Bookbuilding;CNPJ_Emissor;Nome_Emissor;CNPJ_Lider;"
        "Nome_Lider;Grupo_Coordenador;Tipo_Oferta;Emissao;Qtde_Total_Registrada;"
        "Valor_Total_Registrado;Oferta_inicial;Oferta_vasos_comunicantes;Publico_alvo;"
        "Reabertura_serie;Titulo_classificado_como_sustentavel;Titulo_padronizado;"
        "Destinacao_recursos;Data_deliberacao_aprovou_oferta;Mercado_negociacao;"
        "Tipo_lastro;Regime_fiduciario;Ativos_alvo;Descricao_garantias;Descricao_lastro;"
        "Identificacao_devedores_coobrigados;Possibilidade_revolvencia;FIDC_nao_padronizado;"
        "Titulo_incentivado;Regime_distribuicao;Tipo_societario;Administrador;Gestor;"
        "Agente_fiduciario;Escriturador;Custodiante;Avaliador_Risco;Processo_SEI;"
        "Endereco_emissor_rede_mundial_computadores\n"
    )
    # Minimal rows: only fields we need; DictReader tolerates short rows poorly,
    # so build full-enough lines with empties.
    def row(req, dreg, issuer, leader, amount):
        # 47 columns roughly — pad with empties
        cols = [""] * 47
        cols[0] = req
        cols[2] = f"SRE/{req}"
        cols[3] = dreg
        cols[4] = dreg
        cols[6] = "Oferta Registrada"
        cols[7] = "Debêntures"
        cols[11] = issuer
        cols[13] = leader
        cols[15] = "PRIMARIA"
        cols[18] = amount
        return ";".join(cols) + "\n"

    csv_body = (
        header
        + row("1", recent, "ACME S.A.", "BTG PACTUAL SERVIÇOS", "500000.00")
        + row("2", recent, "OTHER S.A.", "BANCO XP", "100.00")
        + row("3", old, "BTG OLD", "BTG PACTUAL", "999.00")
    )
    payload = _zip_bytes({cvm_ofertas.FILE_RESOLUCAO_160: csv_body})

    class FakeResp:
        content = payload

        def raise_for_status(self):
            return None

    monkeypatch.setattr(cvm_ofertas.requests, "get", lambda *a, **k: FakeResp())

    all_recent = cvm_ofertas.fetch_recent(lookback_days=30, watchlist=None, include_legacy=False)
    assert {r["id"] for r in all_recent} == {"cvm-oferta:r160:1", "cvm-oferta:r160:2"}

    btg_only = cvm_ofertas.fetch_recent(
        lookback_days=30, watchlist=["BTG"], include_legacy=False
    )
    assert len(btg_only) == 1
    assert btg_only[0]["issuer"] == "ACME S.A."
    assert btg_only[0]["amount"] == 500000.0


def test_parse_date_formats():
    assert cvm_ofertas._parse_date("2026-07-01") == date(2026, 7, 1)
    assert cvm_ofertas._parse_date("01/07/2026") == date(2026, 7, 1)
    assert cvm_ofertas._parse_date("") is None
