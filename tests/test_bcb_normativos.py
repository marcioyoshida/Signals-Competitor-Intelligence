import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import bcb_normativos


def test_fetch_recent_returns_empty_on_request_error(monkeypatch, capsys):
    def fake_get(*args, **kwargs):
        raise requests.HTTPError("bad request")

    monkeypatch.setattr(bcb_normativos.requests, "get", fake_get)

    result = bcb_normativos.fetch_recent(days=7)

    assert result == []
    assert "Warning: BCB normativos fetch failed" in capsys.readouterr().out


def test_fetch_recent_accepts_accented_query(monkeypatch):
    class FakeResponse:
        def __init__(self):
            self.text = "<li><a href='/estabilidadefinanceira/exibenormativo?tipo=Instrução%20Normativa%20BCB&numero=760'>Instrução Normativa BCB n° 760</a> Data/Hora Documento: 9/7/2026 13:46 Assunto: Divulga a versão 9.0 do Manual de Experiência do Cliente no Open Finance. Responsável: DENOR</li>"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(bcb_normativos.requests, "get", lambda *args, **kwargs: FakeResponse())

    result = bcb_normativos.fetch_recent(days=7, query="cartão de crédito")

    assert result
    assert result[0]["subject"] == "Divulga a versão 9.0 do Manual de Experiência do Cliente no Open Finance."
