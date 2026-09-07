"""Ingest the SPA/MF authorized fixed-odds betting operators list — betting new-entrant feed (#78, E3).

Betting / iGaming (SPA/MF, Leis 13.756/14.790) had no entrant detection. The Secretaria de Prêmios
e Apostas publishes the authorized-operators spreadsheet on gov.br — and for this sector the
**authorized list IS the entrant feed** (a small, high-interest set: ~85 companies / ~188 brands as
of 2026-09). A new company (new CNPJ / Portaria) appearing = "a new betting operator cleared
authorization". Same Job-1 shape as `bcb_autorizacoes`.

Live source (verified 2026-09-07):
  https://www.gov.br/fazenda/pt-br/composicao/orgaos/secretaria-de-premios-e-apostas/
    transparencia-ativa-processos-de-autorizacao-de-apostas-de-quota-fixa/planilha-de-autorizacoes.xlsx
  HTTP 200, ~77 KB, .xlsx. Sheet1 header: PORTARIA DE AUTORIZAÇÃO | DENOMINAÇÃO SOCIAL DA EMPRESA |
  CNPJ | MARCAS | DOMÍNIOS | NÚMERO E ANO DO REQUERIMENTO. A company with several brands spans
  multiple rows (company fields only on its first row; following rows carry brand/domain only).
  gov.br requires a browser User-Agent (default UA → 404 stub).

The entity is the authorized COMPANY (empresa + CNPJ); its brands/domains are collected as
products/aliases. XLSX is parsed with the stdlib (zipfile + ElementTree) — no openpyxl dependency
added to the Lambda bundle. Lambda port: handler wraps fetch_authorized(); JsonState → delta.
"""
from __future__ import annotations

import io
import re
import zipfile
from typing import Any
from xml.etree import ElementTree as ET

import requests

PLANILHA_XLSX = (
    "https://www.gov.br/fazenda/pt-br/composicao/orgaos/secretaria-de-premios-e-apostas/"
    "transparencia-ativa-processos-de-autorizacao-de-apostas-de-quota-fixa/planilha-de-autorizacoes.xlsx"
)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/126.0 Safari/537.36")
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_COL_RE = re.compile(r"^([A-Z]+)")


def _col_letter(ref: str) -> str:
    """Column letters from a cell ref like 'C12' -> 'C' (positional; blank cells are omitted)."""
    m = _COL_RE.match(ref or "")
    return m.group(1) if m else ""


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in si.iter(f"{_NS}t")) for si in root.findall(f"{_NS}si")]


def _rows(z: zipfile.ZipFile, ss: list[str], sheet: str = "xl/worksheets/sheet1.xml"):
    """Yield each row as a {column-letter: value} dict (only non-empty cells present)."""
    root = ET.fromstring(z.read(sheet))
    for r in root.findall(f".//{_NS}row"):
        cells: dict[str, str] = {}
        for c in r.findall(f"{_NS}c"):
            v = c.find(f"{_NS}v")
            if v is None or v.text is None:
                continue
            val = ss[int(v.text)] if c.get("t") == "s" else v.text
            cells[_col_letter(c.get("r", ""))] = (val or "").strip()
        yield cells


def _digits(v: Any) -> str:
    return "".join(ch for ch in str(v or "") if ch.isdigit())


def parse_authorized(content: bytes) -> list[dict[str, Any]]:
    """Parse the SPA authorizations XLSX into one record per authorized COMPANY, collecting its
    brands + domains. Company fields (portaria/empresa/CNPJ) carry forward across brand-only rows."""
    z = zipfile.ZipFile(io.BytesIO(content))
    ss = _shared_strings(z)
    out: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    cur: dict[str, Any] | None = None
    started = False
    for cells in _rows(z, ss):
        # Header row establishes the layout; data starts after it. Detect by the CNPJ header.
        if not started:
            if any(c.upper() == "CNPJ" for c in cells.values()):
                started = True
            continue
        empresa = cells.get("C", "")
        cnpj = _digits(cells.get("D", ""))
        brand = cells.get("E", "")
        domain = cells.get("F", "")
        if empresa and cnpj:  # a new company row
            key = cnpj or empresa
            if key in by_key:
                cur = by_key[key]
            else:
                cur = {
                    "id": f"spa-bet:{key}",
                    "source": "SPA-Apostas",
                    "kind": "competitor",
                    "cnpj": cnpj or None,
                    "name": empresa,
                    "portaria": cells.get("B") or None,
                    "requerimento": cells.get("G") or None,
                    "license_class": "Casa de apostas (SPA/MF)",
                    "is_fintech": False,
                    "legal_nature": None,
                    "situation": "autorizada",
                    "registry": "SPA/planilha-de-autorizacoes",
                    "brands": [],
                    "domains": [],
                }
                by_key[key] = cur
                out.append(cur)
        if cur is not None:  # attach brand/domain to the current company (incl. brand-only rows)
            if brand and brand not in cur["brands"]:
                cur["brands"].append(brand)
            if domain and domain not in cur["domains"]:
                cur["domains"].append(domain)
    return out


def fetch_authorized(url: str | None = None, *, timeout: int = 90) -> list[dict[str, Any]]:
    """Fetch + parse the current SPA/MF authorized-operators spreadsheet."""
    resp = requests.get(url or PLANILHA_XLSX, timeout=timeout,
                        headers={"User-Agent": _UA, "Accept-Language": "pt-BR"})
    resp.raise_for_status()
    return parse_authorized(resp.content)


def inspect() -> None:  # pragma: no cover - manual live check
    rows = fetch_authorized()
    print(f"{len(rows)} authorized betting companies")
    for r in rows[:12]:
        print(f"  {(r['name'] or '')[:38]:38} {r['cnpj'] or '':16} brands={r['brands'][:3]}")


if __name__ == "__main__":  # pragma: no cover
    inspect()
