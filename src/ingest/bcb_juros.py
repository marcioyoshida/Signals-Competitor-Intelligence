"""Ingest BCB average interest rates by institution — pricing intelligence.

BCB publishes institution-level average rates for selected credit modalities
under Instrução Normativa nº 563 (2024-12-12), via Olinda OData taxaJuros v2.

Two series:
  - **Daily** (`TaxasJurosDiariaPorInicioPeriodo`) — rolling ~5 business-day
    averages; rich product set (card revolving, personal credit, overdraft,
    working capital, vehicles, …). **Primary CI path.**
  - **Monthly** (`TaxasJurosMensalPorMes`) — month-end averages; currently
    dominated by real-estate modalities only.

API (live-verified 2026-07-19):
  https://olinda.bcb.gov.br/olinda/servico/taxaJuros/versao/v2

Signal: rate moves by institution × modality (detect_moves on TaxaJurosAoAno).
First run seeds baselines; subsequent runs flag relative % rate changes.

Lambda port note: handler wraps fetch_daily() + detect_moves → DynamoDB.
"""
from __future__ import annotations

from typing import Any

import requests

BASE = "https://olinda.bcb.gov.br/olinda/servico/taxaJuros/versao/v2/odata"

# High-CI modalities for payments/retail credit (substring match, case-insensitive).
# Empty list in callers = keep all modalities published for the period.
DEFAULT_MODALITY_FILTERS: list[str] = [
    "Cartão de crédito - rotativo",
    "Cartão de crédito - parcelado",
    "Crédito pessoal não consignado",
    "Cheque especial",
]


def latest_daily_period() -> dict[str, str]:
    """Return the most recent daily window from ConsultaDatas.

    tipoModalidade='D' → daily modalities. Fields: inicioPeriodo, fimPeriodo.
    """
    url = (
        f"{BASE}/ConsultaDatas"
        f"?$filter=tipoModalidade eq 'D'"
        f"&$orderby=inicioPeriodo desc"
        f"&$top=1"
        f"&$format=json"
    )
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    rows = resp.json().get("value", [])
    if not rows:
        raise requests.RequestException("No daily juros periods available from ConsultaDatas")
    row = rows[0]
    return {
        "inicio_periodo": row["inicioPeriodo"],
        "fim_periodo": row["fimPeriodo"],
    }


def latest_monthly_anomes() -> str:
    """Return latest monthly anoMes ('YYYY-MM') with data, probing recent months."""
    import datetime as dt

    d = dt.date.today().replace(day=1)
    for _ in range(12):
        anomes = d.strftime("%Y-%m")
        url = (
            f"{BASE}/TaxasJurosMensalPorMes"
            f"?$filter=anoMes eq '{anomes}'"
            f"&$top=1&$format=json"
        )
        resp = requests.get(url, timeout=60)
        if resp.ok and resp.json().get("value"):
            return anomes
        d = (d - dt.timedelta(days=1)).replace(day=1)
    raise requests.RequestException("Could not determine a monthly juros anoMes")


def fetch_daily(
    inicio_periodo: str | None = None,
    top: int = 10000,
) -> list[dict[str, Any]]:
    """Fetch daily average rates for one publication window.

    inicio_periodo: 'YYYY-MM-DD' (start of the ~5 business-day window).
    Defaults to the latest period from ConsultaDatas.
    """
    if not inicio_periodo:
        inicio_periodo = latest_daily_period()["inicio_periodo"]
    # OData date literal in single quotes
    url = (
        f"{BASE}/TaxasJurosDiariaPorInicioPeriodo"
        f"?$filter=InicioPeriodo eq '{inicio_periodo}'"
        f"&$top={top}"
        f"&$format=json"
    )
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    rows = resp.json().get("value", [])
    return [_normalize_daily(r) for r in rows]


def fetch_monthly(
    anomes: str | None = None,
    top: int = 10000,
) -> list[dict[str, Any]]:
    """Fetch monthly average rates for one YYYY-MM competency month."""
    anomes = anomes or latest_monthly_anomes()
    url = (
        f"{BASE}/TaxasJurosMensalPorMes"
        f"?$filter=anoMes eq '{anomes}'"
        f"&$top={top}"
        f"&$format=json"
    )
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    rows = resp.json().get("value", [])
    return [_normalize_monthly(r) for r in rows]


def _normalize_daily(row: dict[str, Any]) -> dict[str, Any]:
    """Map a live TaxasJurosDiariaPorInicioPeriodo row to a signal record."""
    cnpj8 = row.get("cnpj8") or ""
    modality = row.get("Modalidade") or ""
    segment = row.get("Segmento") or ""
    period = row.get("InicioPeriodo") or ""
    rate_year = row.get("TaxaJurosAoAno")
    rate_month = row.get("TaxaJurosAoMes")
    return {
        "id": f"juros-d:{period}:{cnpj8}:{_slug(segment)}:{_slug(modality)}",
        "source": "BCB-Juros",
        "kind": "competitor",
        "series": "daily",
        "period_start": period,
        "period_end": row.get("FimPeriodo"),
        "cnpj8": cnpj8,
        "institution": row.get("InstituicaoFinanceira"),
        "segment": segment or None,
        "modality": modality,
        "position": row.get("Posicao"),
        "rate_month": rate_month,
        "rate_year": rate_year,
        # Must include Segmento: same cnpj+modality can appear as PF and PJ
        # with different rates; omitting it thrashes value state every run.
        "move_key": f"{cnpj8}|{segment}|{modality}",
        "raw": row,
    }


def _normalize_monthly(row: dict[str, Any]) -> dict[str, Any]:
    """Map a live TaxasJurosMensalPorMes row to a signal record."""
    cnpj8 = row.get("cnpj8") or ""
    modality = row.get("Modalidade") or ""
    anomes = row.get("anoMes") or ""
    return {
        "id": f"juros-m:{anomes}:{cnpj8}:{_slug(modality)}",
        "source": "BCB-Juros",
        "kind": "competitor",
        "series": "monthly",
        "anomes": anomes,
        "period_start": anomes,
        "cnpj8": cnpj8,
        "institution": row.get("InstituicaoFinanceira"),
        "segment": None,
        "modality": modality,
        "position": row.get("Posicao"),
        "rate_month": row.get("TaxaJurosAoMes"),
        "rate_year": row.get("TaxaJurosAoAno"),
        "move_key": f"{cnpj8}|{modality}",
        "raw": row,
    }


def _slug(text: str) -> str:
    """Compact modality for ids — keep alnum, collapse rest to '-'."""
    out = []
    prev_dash = False
    for ch in (text or "").lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-")[:80] or "mod"


def filter_rates(
    rows: list[dict[str, Any]],
    institutions: list[str] | None = None,
    modalities: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Filter by institution-name and/or modality substrings (case-insensitive).

    Empty / None lists mean no filter on that dimension.
    """
    inst_needles = [s.lower() for s in (institutions or []) if s]
    mod_needles = [s.lower() for s in (modalities or []) if s]
    out = []
    for r in rows:
        name = (r.get("institution") or "").lower()
        mod = (r.get("modality") or "").lower()
        if inst_needles and not any(n in name for n in inst_needles):
            continue
        if mod_needles and not any(n in mod for n in mod_needles):
            continue
        out.append(r)
    return out


def for_moves(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop rows without a usable annual rate; keep move_key for detect_moves."""
    return [r for r in rows if r.get("rate_year") is not None and r.get("move_key")]


def list_modalities(rows: list[dict[str, Any]]) -> list[str]:
    """Sorted unique modality names in a rate set."""
    return sorted({r.get("modality") or "" for r in rows if r.get("modality")})


def inspect(series: str = "daily") -> None:
    """One-shot live schema check for daily or monthly juros."""
    if series == "monthly":
        anomes = latest_monthly_anomes()
        print(f"Latest monthly anoMes: {anomes}")
        rows = fetch_monthly(anomes)
        print(f"{len(rows)} monthly rows")
    else:
        period = latest_daily_period()
        print(f"Latest daily period: {period}")
        rows = fetch_daily(period["inicio_periodo"])
        print(f"{len(rows)} daily rows")
    if not rows:
        print("No rows returned.")
        return
    print("Keys:", [k for k in rows[0] if k != "raw"])
    print("Sample:", {k: rows[0][k] for k in rows[0] if k != "raw"})
    print("Modalities:", list_modalities(rows)[:20])


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "inspect":
        inspect(sys.argv[2] if len(sys.argv) > 2 else "daily")
    elif len(sys.argv) > 1 and sys.argv[1] == "monthly":
        rows = fetch_monthly()
        print(f"{len(rows)} monthly rates")
        for r in rows[:15]:
            print(
                f"  {r['rate_year']:7.2f}% a.a.  "
                f"{(r['institution'] or '?')[:28]:28}  "
                f"{(r['modality'] or '')[:50]}"
            )
    else:
        period = latest_daily_period()
        print(f"Daily juros {period['inicio_periodo']} → {period['fim_periodo']}")
        rows = fetch_daily(period["inicio_periodo"])
        focus = filter_rates(rows, modalities=DEFAULT_MODALITY_FILTERS)
        print(f"{len(rows)} total rows; {len(focus)} after default modality filter")
        for r in sorted(focus, key=lambda x: -(x.get("rate_year") or 0))[:20]:
            print(
                f"  {r['rate_year']:7.2f}% a.a.  "
                f"{(r['institution'] or '?')[:28]:28}  "
                f"{(r['modality'] or '')[:50]}"
            )
