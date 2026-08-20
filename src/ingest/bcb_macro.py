"""Ingest BCB macro signals — Copom/Selic decisions + the weekly Focus report.

These are MARKET-WIDE, not competitor-specific, so they surface as standalone
"macro" cards (a separate war-room panel), not entity narratives.

Sources (live-verified 2026-08-19):
  - Copom / Selic target: SGS series 432 ("Meta Selic definida pelo Copom", %
    a.a.). A change in the series is a Copom decision.
    https://api.bcb.gov.br/dados/serie/bcdata.sgs.432/dados/ultimos/{n}?formato=json
  - Focus (Expectativas de Mercado): BCB Olinda Expectativas, annual medians per
    indicator/reference-year, updated daily (the weekly Focus).
    https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/ExpectativasMercadoAnuais
"""
from __future__ import annotations

import datetime as dt
import urllib.parse
from typing import Any, Callable

import requests

# SGS ``ultimos/N`` caps at 20 values — too short to span the ~45-day Copom
# cycle — so pull a date range instead (no cap): dataInicial..dataFinal.
SGS_SELIC_URL = (
    "https://api.bcb.gov.br/dados/serie/bcdata.sgs.432/dados"
    "?formato=json&dataInicial={ini}&dataFinal={fim}"
)
EXPECT_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/"
    "ExpectativasMercadoAnuais"
)
# Focus indicators tracked (Olinda names). PIB/Câmbio carry accents in the API.
FOCUS_INDICATORS = ["IPCA", "Selic", "PIB Total", "Câmbio"]


def fetch_selic(
    lookback_days: int = 420,
    *,
    today: dt.date | None = None,
    fetcher: Callable[[str], Any] | None = None,
) -> dict[str, Any] | None:
    """Current Selic target + the last Copom decision (change point).

    Returns {current, as_of, last_decision: {date, previous, direction, bps}} or
    None on failure. direction ∈ {alta, baixa, manutenção}.
    """
    today = today or dt.date.today()
    ini = (today - dt.timedelta(days=lookback_days)).strftime("%d/%m/%Y")
    fim = today.strftime("%d/%m/%Y")
    try:
        rows = (fetcher or _get_json)(SGS_SELIC_URL.format(ini=ini, fim=fim))
    except Exception as exc:  # pragma: no cover - upstream best-effort
        print(f"Warning: SGS Selic fetch failed: {exc}")
        return None
    series: list[tuple[dt.date, float]] = []
    for r in rows or []:
        d = _parse_br_date(r.get("data"))
        v = _to_float(r.get("valor"))
        if d and v is not None:
            series.append((d, v))
    if not series:
        return None
    series.sort(key=lambda x: x[0])
    cur_date, cur_val = series[-1]
    # Walk back to the last value change — that's the last Copom decision.
    decision: dict[str, Any] | None = None
    for i in range(len(series) - 1, 0, -1):
        if series[i][1] != series[i - 1][1]:
            prev = series[i - 1][1]
            new = series[i][1]
            decision = {
                "date": series[i][0].isoformat(),
                "previous": prev,
                "value": new,
                "direction": "alta" if new > prev else "baixa",
                "bps": round((new - prev) * 100),
            }
            break
    if decision is None:
        decision = {"date": series[0][0].isoformat(), "previous": cur_val,
                    "value": cur_val, "direction": "manutenção", "bps": 0}
    return {"current": cur_val, "as_of": cur_date.isoformat(), "last_decision": decision}


def fetch_focus(
    indicators: list[str] | None = None,
    years: list[int] | None = None,
    *,
    today: dt.date | None = None,
    fetcher: Callable[[str, dict], Any] | None = None,
) -> list[dict[str, Any]]:
    """Latest Focus medians + week-over-week shift, per indicator/reference-year.

    Returns [{indicator, ref_year, median, prev_median, delta, date}].
    """
    today = today or dt.date.today()
    indicators = indicators or FOCUS_INDICATORS
    years = years or [today.year, today.year + 1]
    get = fetcher or _get_odata
    out: list[dict[str, Any]] = []
    for ind in indicators:
        for yr in years:
            try:
                # baseCalculo is filtered in Python — putting `baseCalculo eq 0`
                # in the OData $filter triggers an Edm type error (HTTP 400).
                recs = get(EXPECT_URL, {
                    "$top": "40",
                    "$format": "json",
                    "$orderby": "Data desc",
                    "$filter": f"Indicador eq '{ind}' and DataReferencia eq '{yr}'",
                })
            except Exception as exc:  # pragma: no cover - per-indicator best-effort
                print(f"Warning: Focus fetch failed for {ind}/{yr}: {exc}")
                continue
            recs = [
                r for r in (recs or [])
                if _to_float(r.get("Mediana")) is not None and int(r.get("baseCalculo", 0) or 0) == 0
            ]
            if not recs:
                continue
            cur = recs[0]
            cur_date = _parse_iso(cur.get("Data"))
            cur_med = _to_float(cur.get("Mediana"))
            # Prior = first snapshot at least ~5 calendar days older (a Focus week).
            prev_med = None
            for r in recs[1:]:
                rd = _parse_iso(r.get("Data"))
                if cur_date and rd and (cur_date - rd).days >= 5:
                    prev_med = _to_float(r.get("Mediana"))
                    break
            out.append({
                "indicator": ind,
                "ref_year": yr,
                "median": cur_med,
                "prev_median": prev_med,
                "delta": (round(cur_med - prev_med, 4) if prev_med is not None else None),
                "date": cur.get("Data"),
            })
    return out


def _get_json(url: str) -> Any:
    resp = requests.get(url, timeout=25, headers={"User-Agent": "Onca-CI/1.0 (macro)"})
    return resp.json() if resp.status_code == 200 else []


def _get_odata(url: str, params: dict) -> Any:
    # OData needs %20 for spaces in $filter/$orderby — requests' default param
    # encoding uses '+', which BCB's OData parser rejects (HTTP 400). Encode with
    # quote (space -> %20) and build the query string ourselves.
    qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    resp = requests.get(f"{url}?{qs}", timeout=25, headers={"User-Agent": "Onca-CI/1.0 (macro)"})
    return (resp.json() or {}).get("value", []) if resp.status_code == 200 else []


def _to_float(v: Any) -> float | None:
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _parse_br_date(s: Any) -> dt.date | None:
    try:
        return dt.datetime.strptime(str(s), "%d/%m/%Y").date()
    except (TypeError, ValueError):
        return None


def _parse_iso(s: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def inspect() -> None:  # pragma: no cover - manual helper
    print("Selic:", fetch_selic())
    for f in fetch_focus():
        print("Focus:", f)


if __name__ == "__main__":  # pragma: no cover
    inspect()
