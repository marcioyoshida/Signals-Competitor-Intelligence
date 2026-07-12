"""Ingest BCB normativos (resolutions, circulars, instructions).

The public 'Busca de Normas' page (estabilidadefinanceira/buscanormas) is a
client-rendered Angular app — there's no server HTML to scrape, and the old
/api/search/app/normativos/buscanormas REST endpoint this used to call is
decommissioned (400 regardless of params). The real backing API, found by
inspecting the page's network requests, is the SharePoint Search REST
endpoint below (confirmed against a live browser capture on 2026-07-12).
"""
from __future__ import annotations

import datetime as dt
import html
import re
from typing import Any
from urllib.parse import urlencode

import requests

SEARCH_URL = "https://www.bcb.gov.br/api/search/app/normativos/buscanormativos"
DOCUMENT_URL = "https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo"
REFERER = "https://www.bcb.gov.br/estabilidadefinanceira/buscanormas"

# Document types most relevant to payments/fintech strategy.
# Full list includes Resolução CMN, Resolução BCB, Instrução Normativa BCB,
# Circular, Carta Circular, Comunicado.
DEFAULT_TYPES = [
    "Resolução BCB",
    "Resolução CMN",
    "Instrução Normativa BCB",
    "Comunicado",
]

ROWS_PER_PAGE = 50
MAX_ROWS = 500


def _clean_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


def _to_doc(row: dict[str, Any]) -> dict[str, Any]:
    doc_type = row.get("TipodoNormativoOWSCHCS") or ""
    number = None
    raw_number = row.get("NumeroOWSNMBR")
    if raw_number:
        try:
            number = str(int(float(raw_number)))
        except ValueError:
            number = str(raw_number)
    return {
        "id": f"bcb:{doc_type}:{number}",
        "source": "BCB",
        "kind": "regulatory",
        "doc_type": doc_type,
        "number": number,
        "date": (row.get("data") or "")[:10] or None,
        "subject": _clean_html(row.get("AssuntoNormativoOWSMTXT", "")),
        "url": f"{DOCUMENT_URL}?{urlencode({'tipo': doc_type, 'numero': number})}" if number else None,
    }


def fetch_recent(days: int = 7, types: list[str] | None = None, query: str | None = None) -> list[dict[str, Any]]:
    """Fetch normativos published in the last `days` days."""
    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    end = dt.date.today().isoformat()

    querytext = "ContentType:normativo AND contentSource:normativos"
    if query:
        querytext += f" AND {query}"

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Referer": REFERER,
    }

    rows: list[dict[str, Any]] = []
    startrow = 0
    while True:
        params = {
            "querytext": querytext,
            "rowlimit": ROWS_PER_PAGE,
            "startrow": startrow,
            "sortlist": "Data1OWSDATE:descending",
            "refinementfilters": f"Data:range(datetime({start}),datetime({end}T23:59:59))",
        }
        try:
            resp = requests.get(SEARCH_URL, params=params, timeout=30, headers=headers)
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"Warning: BCB normativos fetch failed: {exc}")
            return [_to_doc(r) for r in rows]

        payload = resp.json()
        page_rows = payload.get("Rows", [])
        rows.extend(page_rows)

        startrow += ROWS_PER_PAGE
        total = payload.get("TotalRows", len(rows))
        if startrow >= total or startrow >= MAX_ROWS or not page_rows:
            break

    docs = [_to_doc(r) for r in rows]
    if types:
        docs = [d for d in docs if d["doc_type"] in types]
    return docs


if __name__ == "__main__":
    for d in fetch_recent(days=14):
        print(f"{d['date']}  {d['doc_type']} {d['number']}: {(d['subject'] or '')[:90]}")
        print(f"   {d['url']}")
