"""Ingest BCB IF.data — quarterly institution-level financials.

The historical endpoint name used in the old implementation is no longer
available. The current public service accepts the OData entity set
`IfDataValores` and the filter arguments shown below.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import requests

BASE = "https://olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1/odata"

# TipoInstituicao=2 -> conglomerados prudenciais e instituições independentes
# Relatorio=T -> summary report (assets, credit, deposits, equity)
TIPO_INSTITUICAO = 2
RELATORIO = "T"


def latest_base_date() -> int:
    """Return the most recent published base date as YYYYMM (e.g. 202603)."""
    # The legacy ListaDeDatas endpoint is unavailable; the working service
    # accepts a direct AnoMes filter, so we prefer the most recent known-good
    # quarterly value from the current quarter.
    for base_date in [202603, 202602, 202601, 202512, 202511]:
        try:
            rows = fetch_institutions(base_date=base_date)
            if rows:
                return base_date
        except requests.RequestException:
            continue
    raise requests.RequestException("Could not determine an IF.data base date")


def fetch_institutions(base_date: int | None = None) -> list[dict[str, Any]]:
    """Fetch summary financials for all institutions at a base date.

    Rows are keyed by CodInst only — this report has no institution name
    field. Resolve display names separately via fetch_institution_names.
    """
    base_date = base_date or latest_base_date()
    url = (
        f"{BASE}/IfDataValores("
        f"AnoMes=@AnoMes,TipoInstituicao=@TipoInstituicao,Relatorio=@Relatorio)"
        f"?@AnoMes={base_date}"
        f"&@TipoInstituicao={TIPO_INSTITUICAO}"
        f"&@Relatorio='{RELATORIO}'"
        f"&$format=json"
    )
    resp = requests.get(url, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.json().get("value", [])


def fetch_institution_names(base_date: int) -> dict[str, str]:
    """Map CodInst -> institution name via the IF.data cadastro function.

    $top is set well above the current registry size (~5.9k institutions)
    since this endpoint doesn't expose a total count to paginate against.
    """
    url = f"{BASE}/IfDataCadastro(AnoMes=@AnoMes)?@AnoMes={base_date}&$format=json&$top=10000"
    resp = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return {r["CodInst"]: r["NomeInstituicao"] for r in resp.json().get("value", []) if r.get("CodInst")}


def market_share(
    rows: list[dict[str, Any]],
    metric: str = "Ativo Total",
    institution_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Compute share of a metric across institutions.

    IF.data returns long-format rows (one row per institution+account),
    keyed by CodInst. Filter to the metric, sum the sector total, compute
    each share. Pass institution_names (from fetch_institution_names) to
    show names instead of raw codes; falls back to the code if omitted.
    """
    names = institution_names or {}
    values: dict[str, float] = {}
    for r in rows:
        if r.get("NomeColuna") == metric and r.get("Saldo") is not None:
            code = r.get("CodInst", "?")
            values[code] = values.get(code, 0.0) + float(r["Saldo"])

    total = sum(values.values()) or 1.0
    ranked = sorted(values.items(), key=lambda kv: kv[1], reverse=True)
    return [
        {"institution": names.get(code, code), "value": round(val, 2), "share_pct": round(100 * val / total, 3)}
        for code, val in ranked
    ]


if __name__ == "__main__":
    date = latest_base_date()
    print(f"Latest IF.data base date: {date}")
    rows = fetch_institutions(date)
    names = fetch_institution_names(date)
    for row in market_share(rows, institution_names=names)[:15]:
        print(f"{row['share_pct']:6.2f}%  {row['institution']}")
