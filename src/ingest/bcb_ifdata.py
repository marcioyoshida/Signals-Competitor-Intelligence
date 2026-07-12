"""Ingest BCB IF.data — quarterly institution-level financials.

This is the source for the market-share axis: total assets and credit
portfolio per authorized institution, from BCB's Olinda OData API.

Quarters are published ~60 days after quarter end (90 for Q4), so
always request the latest AVAILABLE base date, not the current quarter.

API docs: https://olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1/swagger-ui2
"""
from __future__ import annotations

from typing import Any

import requests

BASE = "https://olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1/odata"

# TipoInstituicao=2 -> conglomerados prudenciais e instituições independentes
# Relatorio=T -> summary report (assets, credit, deposits, equity)
TIPO_INSTITUICAO = 2
RELATORIO = "T"


def latest_base_date() -> int:
    """Return the most recent published base date as YYYYMM (e.g. 202603)."""
    url = f"{BASE}/ListaDeDatas?$format=json&$orderby=Data desc&$top=1"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return int(resp.json()["value"][0]["Data"])


def fetch_institutions(base_date: int | None = None) -> list[dict[str, Any]]:
    """Fetch summary financials for all institutions at a base date."""
    base_date = base_date or latest_base_date()
    url = (
        f"{BASE}/IfDataValores("
        f"AnoMes=@AnoMes,TipoInstituicao=@TipoInstituicao,Relatorio=@Relatorio)"
        f"?@AnoMes={base_date}"
        f"&@TipoInstituicao={TIPO_INSTITUICAO}"
        f"&@Relatorio='{RELATORIO}'"
        f"&$format=json"
    )
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    return resp.json()["value"]


def market_share(rows: list[dict[str, Any]], metric: str = "Ativo Total") -> list[dict[str, Any]]:
    """Compute share of a metric across institutions.

    IF.data returns long-format rows (one row per institution+account).
    Filter to the metric, sum the sector total, compute each share.
    """
    values: dict[str, float] = {}
    for r in rows:
        if r.get("NomeColuna") == metric and r.get("Saldo") is not None:
            name = r.get("NomeInstituicao", "?")
            values[name] = values.get(name, 0.0) + float(r["Saldo"])

    total = sum(values.values()) or 1.0
    ranked = sorted(values.items(), key=lambda kv: kv[1], reverse=True)
    return [
        {"institution": name, "value": round(val, 2), "share_pct": round(100 * val / total, 3)}
        for name, val in ranked
    ]


if __name__ == "__main__":
    date = latest_base_date()
    print(f"Latest IF.data base date: {date}")
    rows = fetch_institutions(date)
    for row in market_share(rows)[:15]:
        print(f"{row['share_pct']:6.2f}%  {row['institution']}")
