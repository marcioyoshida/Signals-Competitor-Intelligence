"""Ingest BCB normativos (resolutions, circulars, instructions).

The historical API endpoint is returning 400 for current requests, so the
handler falls back to the public Busca de Normas page and parses the rendered
results HTML.
"""
from __future__ import annotations

import datetime as dt
import html
import re
from typing import Any

import requests

SEARCH_URL = "https://www.bcb.gov.br/estabilidadefinanceira/buscanormas"

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
        "conteudo": "",
        "dataInicioBusca": dt.date.today() - dt.timedelta(days=days),
        "dataFimBusca": dt.date.today(),
        "tipoDocumento": "Todos",
    }

    try:
        resp = requests.get(SEARCH_URL, params=params, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"Warning: BCB normativos fetch failed: {exc}")
        return []

    html_text = resp.text
    docs: list[dict[str, Any]] = []
    for match in re.finditer(r"<li[^>]*>(.*?)</li>", html_text, flags=re.S):
        block = match.group(1)
        if "Instrução Normativa BCB" in block or "Resolução" in block or "Comunicado" in block:
            title_match = re.search(r"<a[^>]*>(.*?)</a>", block, flags=re.S)
            if not title_match:
                continue
            title = html.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip()
            if not title:
                continue
            subject_match = re.search(r"Assunto:\s*(.*?)<", block, flags=re.S)
            date_match = re.search(r"Data/Hora Documento:\s*(.*?)<", block, flags=re.S)
            rel_match = re.search(r"Responsável:\s*(.*?)<", block, flags=re.S)
            url_match = re.search(r'href="([^"]+)"', title_match.group(0))
            docs.append(
                {
                    "id": f"bcb:{title}",
                    "source": "BCB",
                    "kind": "regulatory",
                    "doc_type": title.split(" n°", 1)[0].strip() if " n°" in title else title,
                    "number": re.search(r"n°\s*([0-9]+)", title),
                    "date": html.unescape(date_match.group(1)).strip() if date_match else None,
                    "subject": html.unescape(subject_match.group(1)).strip() if subject_match else None,
                    "url": f"https://www.bcb.gov.br{url_match.group(1)}" if url_match else None,
                }
            )
    return docs


if __name__ == "__main__":
    for d in fetch_recent(days=14):
        print(f"{d['date']}  {d['doc_type']} {d['number']}: {d['subject'][:90]}")
