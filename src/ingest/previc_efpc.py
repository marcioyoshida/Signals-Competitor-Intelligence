"""Ingest the PREVIC EFPC base-cadastral registry — closed-pension new-entrant feed (#79, E2).

Closed-pension (EFPC, PREVIC) — one of the two industries added recently — had no entrant detection.
PREVIC publishes the "Base Cadastral de Entidades" as a direct XLSX download (NOT behind the dead
dados.gov.br CKAN token); a new EFPC row (new CNPJ / Código) = a closed-pension entity newly
registered. Same Job-1 shape as `bcb_autorizacoes`.

Live source (verified 2026-09-07):
  https://www.gov.br/previc/pt-br/dados-abertos/demonstrativos-contabeis/
    base-cadastral-de-entidades/base-cadastral-de-entidades.xlsx/@@download/file
  HTTP 200, ~104 KB, .xlsx, ~454 EFPCs. A title row precedes the header; the header row carries
  `Código da EFPC | Razão Social | Sigla EFPC | CNPJ | Situação Detalhada EFPC | Situacao | UF |
  Município | ...`. gov.br needs a browser User-Agent. XLSX parsed with the stdlib (zipfile +
  ElementTree) — no openpyxl dependency. Columns are located BY HEADER NAME (robust to the title
  offset / column reordering). Lambda port: handler wraps fetch_entities(); JsonState → delta.
"""
from __future__ import annotations

import io
import re
import zipfile
from typing import Any
from xml.etree import ElementTree as ET

import requests

BASE_CADASTRAL_XLSX = (
    "https://www.gov.br/previc/pt-br/dados-abertos/demonstrativos-contabeis/"
    "base-cadastral-de-entidades/base-cadastral-de-entidades.xlsx/@@download/file"
)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/126.0 Safari/537.36")
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_COL_RE = re.compile(r"^([A-Z]+)")


def _col(ref: str) -> str:
    m = _COL_RE.match(ref or "")
    return m.group(1) if m else ""


def _digits(v: Any) -> str:
    return "".join(ch for ch in str(v or "") if ch.isdigit())


def _sheet_rows(content: bytes) -> list[dict[str, str]]:
    """Every row of sheet1 as a {column-letter: value} dict (blank cells omitted)."""
    z = zipfile.ZipFile(io.BytesIO(content))
    ss: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        ss = ["".join(t.text or "" for t in si.iter(f"{_NS}t")) for si in root.findall(f"{_NS}si")]
    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    out: list[dict[str, str]] = []
    for r in sheet.findall(f".//{_NS}row"):
        cells: dict[str, str] = {}
        for c in r.findall(f"{_NS}c"):
            v = c.find(f"{_NS}v")
            if v is None or v.text is None:
                continue
            val = ss[int(v.text)] if c.get("t") == "s" else v.text
            cells[_col(c.get("r", ""))] = (val or "").strip()
        out.append(cells)
    return out


# Header label (normalized: lowercased, single-spaced) -> our field name.
_FIELDS = {
    "codigo da efpc": "codigo_efpc",
    "razao social": "name",
    "sigla efpc": "sigla",
    "cnpj": "cnpj",
    "situacao detalhada efpc": "situacao_detalhada",
    "situacao": "situacao",
    "uf": "uf",
    "municipio": "municipio",
}


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    for a, b in (("ç", "c"), ("ã", "a"), ("õ", "o"), ("á", "a"), ("é", "e"), ("í", "i"),
                 ("ó", "o"), ("ú", "u"), ("â", "a"), ("ê", "e")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s)


def parse_entities(content: bytes) -> list[dict[str, Any]]:
    """Parse the PREVIC base-cadastral XLSX into EFPC entrant records. Locates the header row by the
    presence of a CNPJ column, then maps the needed columns BY NAME (robust to the title offset)."""
    rows = _sheet_rows(content)
    colmap: dict[str, str] = {}  # column-letter -> field
    data_start = None
    for i, cells in enumerate(rows):
        norm = {col: _norm(v) for col, v in cells.items()}
        if any(v == "cnpj" for v in norm.values()):
            for col, v in norm.items():
                if v in _FIELDS:
                    colmap[col] = _FIELDS[v]
            data_start = i + 1
            break
    if data_start is None:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cells in rows[data_start:]:
        rec = {field: cells.get(col, "") for col, field in colmap.items()}
        name = rec.get("name")
        if not name:
            continue
        cnpj = _digits(rec.get("cnpj"))
        codigo = rec.get("codigo_efpc")
        ident = cnpj or codigo or name
        if ident in seen:
            continue
        seen.add(ident)
        out.append({
            "id": f"previc-efpc:{ident}",
            "source": "PREVIC-EFPC",
            "kind": "competitor",
            "cnpj": cnpj or None,
            "name": name,
            "sigla": rec.get("sigla") or None,
            "codigo_efpc": codigo or None,
            "license_class": "EFPC (previdência complementar fechada)",
            "is_fintech": False,
            "legal_nature": None,
            "situation": rec.get("situacao") or rec.get("situacao_detalhada") or "cadastrada",
            "uf": rec.get("uf") or None,
            "municipio": rec.get("municipio") or None,
            "registry": "PREVIC/base-cadastral-entidades",
        })
    return out


def fetch_entities(url: str | None = None, *, timeout: int = 90) -> list[dict[str, Any]]:
    """Fetch + parse the current PREVIC EFPC base-cadastral registry."""
    resp = requests.get(url or BASE_CADASTRAL_XLSX, timeout=timeout,
                        headers={"User-Agent": _UA, "Accept-Language": "pt-BR"})
    resp.raise_for_status()
    return parse_entities(resp.content)


def inspect() -> None:  # pragma: no cover - manual live check
    rows = fetch_entities()
    print(f"{len(rows)} EFPCs")
    for r in rows[:12]:
        print(f"  {(r['sigla'] or '')[:12]:12} {(r['name'] or '')[:40]:40} {r['cnpj'] or ''} {r['situation'][:20]}")


if __name__ == "__main__":  # pragma: no cover
    inspect()
