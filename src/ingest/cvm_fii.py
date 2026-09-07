"""Ingest CVM FII Informe Mensal — structured real-estate-fund universe.

FIIs (Fundos de Investimento Imobiliário) publish a monthly informe in their own
CVM open-data package. This is the FII sibling of ``cvm_fiagro`` (#14 Stage 1 /
#102): the **high-precision structured registry sync** for industry
``real-estate-funds``. Every row carries a CNPJ (and, for listed funds, a B3
ticker derived from the ISIN) — a strong identity safe for entity auto-create /
enrichment via the shared, industry-parametric ``entity_discovery.discover_fiagro``
engine (this module only fetches + normalizes; no bespoke discovery logic).

Source package (verified live 2026-09-07):
  https://dados.cvm.gov.br/dados/FII/DOC/INF_MENSAL/DADOS/inf_mensal_fii_{YEAR}.zip
  Members: inf_mensal_fii_geral_{YEAR}.csv (identity),
           inf_mensal_fii_ativo_passivo_{YEAR}.csv (Total_Investido — size proxy),
           inf_mensal_fii_complemento_{YEAR}.csv (unused here).

The file is keyed by YEAR with one row per fund per monthly Data_Referencia (and a
Versao); we keep the **latest (Data_Referencia, Versao)** row per CNPJ.

Scope: **discovery only.** Unlike ``cvm_fiagro`` this module deliberately ships no
PL-move narrative helpers — ``ativo_passivo.Total_Investido`` is an investment-total
*proxy*, NOT a labelled patrimônio líquido (CVM publishes no clean PL column here),
so it is used only as an internal size floor for auto-create, never surfaced as a
metric. Name matching is NOT used to resolve (it mis-attributes across managers —
see the FII plan doc); CNPJ + ISIN-ticker are the only join keys.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import zipfile
from typing import Any

import requests

from src.ingest.cvm_fiagro import _digits, _parse_float, _ticker_from_isin

BASE = "https://dados.cvm.gov.br/dados/FII/DOC/INF_MENSAL/DADOS"
ZIP_TMPL = f"{BASE}/inf_mensal_fii_{{year}}.zip"
DATASET_URL = "https://dados.cvm.gov.br/dataset/fii-doc-inf_mensal"


def latest_year() -> int:
    return dt.date.today().year


def _find_available_year(max_lookback: int = 2) -> int | None:
    """Newest year whose informe ZIP exists (HEAD). Early January the current
    year's file may not be published yet → fall back to the prior year."""
    for back in range(max_lookback + 1):
        year = latest_year() - back
        try:
            r = requests.head(ZIP_TMPL.format(year=year), timeout=20, allow_redirects=True)
            if r.status_code == 200:
                return year
        except requests.RequestException:
            continue
    return None


def _rank(row: dict[str, str]) -> tuple[str, int]:
    """Sort key to pick the most recent record for a CNPJ: latest Data_Referencia,
    then highest Versao."""
    try:
        ver = int(row.get("Versao") or 0)
    except ValueError:
        ver = 0
    return ((row.get("Data_Referencia") or "")[:10], ver)


def _latest_by_cnpj(zf: zipfile.ZipFile, member_hint: str) -> dict[str, dict[str, str]]:
    """Read a member CSV → {cnpj14: latest row} keyed by CNPJ_Fundo_Classe."""
    name = next((n for n in zf.namelist() if member_hint in n and n.endswith(".csv")), None)
    best: dict[str, dict[str, str]] = {}
    if not name:
        return best
    with zf.open(name) as fh:
        reader = csv.DictReader(io.TextIOWrapper(fh, encoding="latin-1", newline=""), delimiter=";")
        for row in reader:
            cnpj = _digits(row.get("CNPJ_Fundo_Classe"))
            if len(cnpj) < 14:
                continue
            cur = best.get(cnpj)
            if cur is None or _rank(row) >= _rank(cur):
                best[cnpj] = row
    return best


def fetch_fii(
    year: int | None = None,
    *,
    min_pl: float = 0.0,
    zip_url: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch + normalize the FII universe for a year (latest month per fund).

    Returns records in the same shape ``entity_discovery._profile_from_fiagro``
    reads (``fund_name``/``cnpj``/``isin``/``ticker``/``admin``/``pl``/…), tagged
    ``industry="real-estate-funds"``. ``pl`` is the ativo_passivo Total_Investido
    size proxy (see module docstring); ``min_pl`` drops micro/illiquid vehicles.
    """
    if zip_url is None:
        year = year or _find_available_year() or latest_year()
        zip_url = ZIP_TMPL.format(year=year)
    resp = requests.get(zip_url, timeout=180)
    resp.raise_for_status()

    out: list[dict[str, Any]] = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        geral = _latest_by_cnpj(zf, "inf_mensal_fii_geral")
        ap = _latest_by_cnpj(zf, "inf_mensal_fii_ativo_passivo")
        for cnpj, row in geral.items():
            name = (row.get("Nome_Fundo_Classe") or "").strip()
            isin = (row.get("Codigo_ISIN") or "").strip() or None
            ticker = _ticker_from_isin(isin)
            size = _parse_float((ap.get(cnpj) or {}).get("Total_Investido")) or 0.0
            if size < min_pl:
                continue
            as_of = (row.get("Data_Referencia") or "")[:10] or f"{year}-01-01"
            out.append(
                {
                    "id": f"cvm:fii:{cnpj}",
                    "source": "CVM",
                    "kind": "competitor",
                    "fund_name": name,
                    "fund_class": "FII",
                    "cnpj": cnpj,
                    "isin": isin,
                    "ticker": ticker,
                    "admin": (row.get("Nome_Administrador") or "").strip() or None,
                    "admin_cnpj": _digits(row.get("CNPJ_Administrador")) or None,
                    "manager": None,  # FII geral carries no gestor column
                    "pl": size,  # Total_Investido proxy — discovery floor only, never a metric
                    "segmento": (row.get("Segmento_Atuacao") or "").strip() or None,
                    "tipo_gestao": (row.get("Tipo_Gestao") or "").strip() or None,
                    "listed": (row.get("Mercado_Negociacao_Bolsa") or "").strip().upper() == "S",
                    "registered": (row.get("Data_Funcionamento") or "")[:10] or None,
                    "publico_alvo": (row.get("Publico_Alvo") or "").strip() or None,
                    "site": (row.get("Site") or "").strip() or None,
                    "url": DATASET_URL,
                    "as_of": as_of,
                    "industry": "real-estate-funds",
                    "registry": "fii_inf_mensal",
                    "discovery_source": "cvm_fii",
                }
            )
    out.sort(key=lambda r: r.get("pl") or 0.0, reverse=True)
    return out


def inspect(top: int = 15, *, min_pl: float = 0.0) -> None:  # pragma: no cover - manual
    rows = fetch_fii(min_pl=min_pl)
    print(f"FII: {len(rows)} funds (min_pl={min_pl:.0f})")
    for r in rows[:top]:
        print(
            f"  {r.get('ticker') or '----':6}  {(r.get('cnpj') or '')[:14]:14}  "
            f"~R${(r.get('pl') or 0) / 1e6:8.1f} mi  {(r.get('segmento') or '-'):18}  "
            f"{(r.get('fund_name') or '')[:44]}"
        )


if __name__ == "__main__":  # pragma: no cover - manual probe
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "inspect":
        inspect(min_pl=float(sys.argv[2]) if len(sys.argv) > 2 else 0.0)
    else:
        sample = fetch_fii()
        print(f"{len(sample)} FII funds")
        for f in sample[:12]:
            print(f"  {f.get('ticker') or '----':6}  {(f.get('fund_name') or '')[:55]}")
