import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.diff.engine import ValueState, detect_moves
from src.ingest import bcb_juros


def test_normalize_daily_maps_live_fields():
    row = {
        "InicioPeriodo": "2026-06-29",
        "FimPeriodo": "2026-07-03",
        "Segmento": "PESSOA FÍSICA",
        "Modalidade": "Cartão de crédito - rotativo total - Prefixado",
        "Posicao": 1,
        "InstituicaoFinanceira": "BANCO EXEMPLO",
        "TaxaJurosAoMes": 10.0,
        "TaxaJurosAoAno": 213.84,
        "cnpj8": "12345678",
    }
    rec = bcb_juros._normalize_daily(row)
    assert rec["cnpj8"] == "12345678"
    assert rec["institution"] == "BANCO EXEMPLO"
    assert rec["rate_year"] == 213.84
    assert rec["move_key"] == "12345678|PESSOA FÍSICA|Cartão de crédito - rotativo total - Prefixado"
    assert rec["id"].startswith("juros-d:2026-06-29:12345678:")
    assert rec["source"] == "BCB-Juros"
    assert rec["series"] == "daily"


def test_filter_rates_by_institution_and_modality():
    rows = [
        {
            "institution": "BANCO ITAU",
            "modality": "Cartão de crédito - rotativo total - Prefixado",
            "rate_year": 100.0,
            "move_key": "1|rot",
        },
        {
            "institution": "BANCO XP",
            "modality": "Cheque especial - Prefixado",
            "rate_year": 50.0,
            "move_key": "2|cheq",
        },
        {
            "institution": "BANCO ITAU",
            "modality": "Capital de giro com prazo até 365 dias - Prefixado",
            "rate_year": 20.0,
            "move_key": "1|giro",
        },
    ]
    filtered = bcb_juros.filter_rates(
        rows, institutions=["ITAU"], modalities=["Cartão de crédito"]
    )
    assert len(filtered) == 1
    assert filtered[0]["move_key"] == "1|rot"


def test_for_moves_drops_missing_rate():
    rows = [
        {"move_key": "a", "rate_year": 10.0},
        {"move_key": "b", "rate_year": None},
        {"move_key": None, "rate_year": 5.0},
    ]
    assert bcb_juros.for_moves(rows) == [{"move_key": "a", "rate_year": 10.0}]


def test_detect_moves_on_rate_year(tmp_path, monkeypatch):
    monkeypatch.setattr(bcb_juros, "BASE", "unused")  # not used here
    # Use temp state dir via engine ValueState path monkeypatch
    from src.diff import engine

    monkeypatch.setattr(engine, "STATE_DIR", tmp_path)

    v1 = [
        {
            "move_key": "111|Cartão rotativo",
            "rate_year": 100.0,
            "institution": "Bank A",
            "modality": "Cartão rotativo",
        }
    ]
    assert detect_moves("bcb_juros", v1, "move_key", "rate_year", min_pct=10) == []

    v2 = [
        {
            "move_key": "111|Cartão rotativo",
            "rate_year": 130.0,
            "institution": "Bank A",
            "modality": "Cartão rotativo",
        }
    ]
    moves = detect_moves("bcb_juros", v2, "move_key", "rate_year", min_pct=10)
    assert len(moves) == 1
    assert moves[0]["pct_change"] == 30.0
    assert moves[0]["prev_value"] == 100.0


def test_fetch_daily_uses_filter_and_normalizes(monkeypatch):
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "value": [
                    {
                        "InicioPeriodo": "2026-06-29",
                        "FimPeriodo": "2026-07-03",
                        "Segmento": "PESSOA FÍSICA",
                        "Modalidade": "Cheque especial - Prefixado",
                        "Posicao": 3,
                        "InstituicaoFinanceira": "BANCO X",
                        "TaxaJurosAoMes": 5.0,
                        "TaxaJurosAoAno": 80.0,
                        "cnpj8": "99999999",
                    }
                ]
            }

    def fake_get(url, timeout=120):
        captured["url"] = url
        return FakeResp()

    monkeypatch.setattr(bcb_juros.requests, "get", fake_get)
    rows = bcb_juros.fetch_daily("2026-06-29")
    assert "InicioPeriodo eq '2026-06-29'" in captured["url"]
    assert len(rows) == 1
    assert rows[0]["cnpj8"] == "99999999"
    assert rows[0]["rate_year"] == 80.0


def test_latest_daily_period(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "value": [
                    {
                        "inicioPeriodo": "2026-06-29",
                        "fimPeriodo": "2026-07-03",
                        "tipoModalidade": "D",
                    }
                ]
            }

    monkeypatch.setattr(bcb_juros.requests, "get", lambda *a, **k: FakeResp())
    period = bcb_juros.latest_daily_period()
    assert period == {"inicio_periodo": "2026-06-29", "fim_periodo": "2026-07-03"}
