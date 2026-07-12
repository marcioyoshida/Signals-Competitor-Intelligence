import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import bcb_normativos

# Real row shape captured from a live browser Network-tab inspection of
# /api/search/app/normativos/buscanormativos on 2026-07-12.
LIVE_SHAPED_ROW = {
    "title": "Comunicado N° 45.546",
    "RefinableString01": "string;#10/7/2026 18:30",
    "AssuntoNormativoOWSMTXT": (
        '<div class="ExternalClassC73D4FC6E8394059895BDC7B95C9382A">Divulga as condições de '
        "oferta pública para a realização de operações de swap para fins de rolagem do "
        "vencimento de 03/08/2026</div>"
    ),
    "ResponsavelOWSText": "DEPIN",
    "listItemId": "135940",
    "TipodoNormativoOWSCHCS": "Comunicado",
    "NumeroOWSNMBR": "45546.0000000000",
    "RevogadoOWSBOOL": "0",
    "HitHighlightedSummary": "Divulga as condições de oferta pública ...",
    "CanceladoOWSBOOL": "0",
    "data": "2026-07-10T21:30:12Z",
    "RefinableString03": "string;#Comunicado",
    "RefinableString05": "string;#",
    "RowNumber": 0,
}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_fetch_recent_returns_empty_on_request_error(monkeypatch, capsys):
    def fake_get(*args, **kwargs):
        raise requests.HTTPError("bad request")

    monkeypatch.setattr(bcb_normativos.requests, "get", fake_get)

    result = bcb_normativos.fetch_recent(days=7)

    assert result == []
    assert "Warning: BCB normativos fetch failed" in capsys.readouterr().out


def test_fetch_recent_parses_live_shaped_response(monkeypatch):
    payload = {"TotalRows": 1, "RowCount": 1, "Rows": [LIVE_SHAPED_ROW]}
    monkeypatch.setattr(bcb_normativos.requests, "get", lambda *args, **kwargs: FakeResponse(payload))

    result = bcb_normativos.fetch_recent(days=7)

    assert result == [
        {
            "id": "bcb:Comunicado:45546",
            "source": "BCB",
            "kind": "regulatory",
            "doc_type": "Comunicado",
            "number": "45546",
            "date": "2026-07-10",
            "subject": "Divulga as condições de oferta pública para a realização de operações "
            "de swap para fins de rolagem do vencimento de 03/08/2026",
            "url": "https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?tipo=Comunicado&numero=45546",
        }
    ]


def test_fetch_recent_builds_date_range_query(monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=None, headers=None):
        captured["params"] = params
        return FakeResponse({"TotalRows": 0, "RowCount": 0, "Rows": []})

    monkeypatch.setattr(bcb_normativos.requests, "get", fake_get)

    bcb_normativos.fetch_recent(days=7)

    assert captured["params"]["querytext"] == "ContentType:normativo AND contentSource:normativos"
    assert captured["params"]["refinementfilters"].startswith("Data:range(datetime(")


def test_fetch_recent_filters_by_types(monkeypatch):
    comunicado = dict(LIVE_SHAPED_ROW)
    resolucao = dict(LIVE_SHAPED_ROW, TipodoNormativoOWSCHCS="Resolução CMN", NumeroOWSNMBR="5328.0")
    payload = {"TotalRows": 2, "RowCount": 2, "Rows": [comunicado, resolucao]}
    monkeypatch.setattr(bcb_normativos.requests, "get", lambda *args, **kwargs: FakeResponse(payload))

    result = bcb_normativos.fetch_recent(days=7, types=["Resolução CMN"])

    assert [d["doc_type"] for d in result] == ["Resolução CMN"]


def test_fetch_recent_paginates_until_total_rows_covered(monkeypatch):
    page_size = bcb_normativos.ROWS_PER_PAGE
    row_one = dict(LIVE_SHAPED_ROW, NumeroOWSNMBR="1.0")
    row_two = dict(LIVE_SHAPED_ROW, NumeroOWSNMBR="2.0")
    pages = [
        {"TotalRows": page_size + 1, "RowCount": page_size, "Rows": [row_one] * page_size},
        {"TotalRows": page_size + 1, "RowCount": 1, "Rows": [row_two]},
    ]
    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append(params["startrow"])
        return FakeResponse(pages[len(calls) - 1])

    monkeypatch.setattr(bcb_normativos.requests, "get", fake_get)

    result = bcb_normativos.fetch_recent(days=7)

    assert calls == [0, page_size]
    assert len(result) == page_size + 1
