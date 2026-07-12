"""Ingest BCB normativos (resolutions, circulars, instructions).

Uses the Banco Central 'Buscador de Normas' public search API.
No auth required. Returns recently published regulatory documents.

Lambda port note: handler(event, context) wraps fetch_recent();
state moves from local JSON to S3/DynamoDB.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import requests

SEARCH_URL = "https://www.bcb.gov.br/api/search/app/normativos/buscanormas"

# Document types most relevant to payments/fintech strategy.
# Full list includes Resolução CMN, Resolução BCB, Instrução Normativa BCB,
# Circular, Carta Circular, Comunicado.
DEFAULT_TYPES = [
    "Resolução BCB",
    "Resolução CMN",
    "Instrução Normativa BCB",
    "Comunicado",
]


def fetch_recent(days: int = 7, types: list[str] | None = None) -> list[dict[str, Any]]:
    """Fetch normativos published in the last `days` days."""
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    params = {
        "tipodocumento": ",".join(types or DEFAULT_TYPES),
        "dtinicial": since,
        "querytext": "",
        "startrow": 0,
        "rows": 100,
        "sort": "data desc",
    }
    resp = requests.get(SEARCH_URL, params=params, timeout=30)
    resp.raise_for_status()
    rows = resp.json().get("conteudo", [])

    docs = []
    for row in rows:
        docs.append(
            {
                # Stable ID: type + number is unique per normativo
                "id": f"bcb:{row.get('tipodoNormativo', '?')}:{row.get('numero', '?')}",
                "source": "BCB",
                "kind": "regulatory",
                "doc_type": row.get("tipodoNormativo"),
                "number": row.get("numero"),
                "date": row.get("dataDocumento"),
                "subject": (row.get("assunto") or "").strip(),
                "url": f"https://www.bcb.gov.br{row.get('url', '')}",
            }
        )
    return docs


if __name__ == "__main__":
    for d in fetch_recent(days=14):
        print(f"{d['date']}  {d['doc_type']} {d['number']}: {d['subject'][:90]}")
