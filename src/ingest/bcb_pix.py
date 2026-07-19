"""Ingest BCB Pix DICT key stats — competitor traction signal.

Pix is Brazil's instant-payment system. The open Pix_DadosAbertos service
exposes several FunctionImports; the one that carries **per-institution**
detail (ISPB + name) is ChavesPix — monthly DICT key counts by institution,
user nature, and key type. That is the closest free public proxy to
competitor Pix footprint (and fresher than IF.data's quarterly cadence).

API (live-verified 2026-07-19):
  https://olinda.bcb.gov.br/olinda/servico/Pix_DadosAbertos/versao/v1

  ChavesPix(Data=date) → Data, ISPB, Nome, NaturezaUsuario, TipoChave,
                          qtdChaves, Segmento

Note: EstatisticasTransacoesPix / TransacoesPixPorMunicipio are FunctionImports
too, but they have no ISPB (aggregate demographics / municipality only).
There is no public per-institution transaction-value series on this service.

Lambda port note: handler wraps fetch_recent(); value state → DynamoDB.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import requests

BASE = "https://olinda.bcb.gov.br/olinda/servico/Pix_DadosAbertos/versao/v1/odata"

# Live FunctionImport that returns per-ISPB rows (requires Data=Edm.Date).
DEFAULT_RESOURCE = "ChavesPix"


def _default_data_date(months_back: int = 1) -> str:
    """First day of the competency month, as 'YYYY-MM-DD' for ChavesPix(Data=).

    months_back=1 → previous calendar month (latest typically complete).
    """
    d = dt.date.today().replace(day=1)
    for _ in range(months_back):
        d = (d - dt.timedelta(days=1)).replace(day=1)
    return d.isoformat()


def _anomes_from_date(iso_date: str) -> int:
    return int(iso_date[:4] + iso_date[5:7])


def fetch_recent(
    data_date: str | None = None,
    resource: str = DEFAULT_RESOURCE,
    top: int = 10000,
) -> list[dict[str, Any]]:
    """Fetch Pix DICT key stats for a competency month.

    data_date: 'YYYY-MM-DD' (first of month works; API returns end-of-month Data).
    Returns normalized signal records ready for by_institution() + detect_moves().
    """
    data_date = data_date or _default_data_date()
    anomes = _anomes_from_date(data_date)
    # FunctionImport form required — plain EntitySet path returns 400.
    url = (
        f"{BASE}/{resource}(Data=@d)"
        f"?@d='{data_date}'"
        f"&$top={top}"
        f"&$format=json"
    )
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    rows = resp.json().get("value", [])
    return [_normalize(r, anomes) for r in rows]


def _normalize(row: dict[str, Any], anomes: int) -> dict[str, Any]:
    """Map a live ChavesPix row to a signal record.

    Live keys: Data, ISPB, Nome, NaturezaUsuario, TipoChave, qtdChaves, Segmento.
    tx_value is set to qtdChaves so detect_moves(value_field='tx_value') tracks
    DICT-key stock momentum (the available per-ISPB metric).
    """
    ispb = row.get("ISPB") or row.get("ispb")
    inst = row.get("Nome") or row.get("NomeInstituicao") or row.get("Instituicao")
    keys = row.get("qtdChaves") or row.get("Quantidade") or 0
    ident = ispb or inst or "unknown"
    return {
        "id": f"pix:{anomes}:{ident}:{row.get('TipoChave') or ''}:{row.get('NaturezaUsuario') or ''}",
        "source": "BCB-Pix",
        "kind": "competitor",
        "anomes": anomes,
        "ispb": ispb,
        "institution": inst,
        "segment": row.get("Segmento"),
        "key_type": row.get("TipoChave"),
        "user_nature": row.get("NaturezaUsuario"),
        "tx_count": keys,
        "tx_value": keys,  # DICT key stock — see module docstring
        "raw": row,
    }


def by_institution(
    rows: list[dict[str, Any]], watchlist_ispb: list[str] | None = None
) -> list[dict[str, Any]]:
    """Aggregate Pix DICT keys per institution, optional watchlist filter.

    ChavesPix is broken down by key type × user nature, so the same ISPB
    appears in many rows — sum them for an institution view.
    """
    watch = set(watchlist_ispb or [])
    agg: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = r.get("ispb") or r.get("institution") or "unknown"
        if watch and key not in watch:
            continue
        a = agg.setdefault(
            key,
            {
                "institution": r.get("institution"),
                "ispb": r.get("ispb"),
                "segment": r.get("segment"),
                "tx_count": 0.0,
                "tx_value": 0.0,
            },
        )
        a["tx_count"] += float(r.get("tx_count") or 0)
        a["tx_value"] += float(r.get("tx_value") or 0)
        if not a.get("institution") and r.get("institution"):
            a["institution"] = r["institution"]
        if not a.get("segment") and r.get("segment"):
            a["segment"] = r["segment"]

    ranked = sorted(agg.values(), key=lambda x: x["tx_value"], reverse=True)
    total_val = sum(x["tx_value"] for x in ranked) or 1.0
    for x in ranked:
        x["value_share_pct"] = round(100 * x["tx_value"] / total_val, 3)
    return ranked


# ── SPI settlement-flow view ────────────────────────────────────────────
# SPI publishes aggregate settlement series (PixLiquidadosAtual, etc.) —
# not per-ISPB. Kept for inspect/CLI; not used by the Lambda digest yet.
SPI_BASE = "https://olinda.bcb.gov.br/olinda/servico/SPI/versao/v1/odata"
SPI_DEFAULT_RESOURCE = "PixLiquidadosAtual"


def fetch_spi(
    anomes: int | None = None,
    resource: str = SPI_DEFAULT_RESOURCE,
    top: int = 10000,
) -> list[dict[str, Any]]:
    """Fetch SPI aggregate settlement stats (not per-institution)."""
    url = f"{SPI_BASE}/{resource}?$top={top}&$format=json"
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    rows = resp.json().get("value", [])
    out = []
    for r in rows:
        out.append(
            {
                "id": f"spi:{r.get('Data') or 'unknown'}",
                "source": "BCB-SPI",
                "kind": "market",
                "date": r.get("Data"),
                "tx_count": r.get("Quantidade"),
                "tx_value": r.get("Total"),
                "raw": r,
            }
        )
    return out


def inspect_spi(resource: str = SPI_DEFAULT_RESOURCE) -> None:
    """One-shot schema check for the SPI service."""
    url = f"{SPI_BASE}/{resource}?$top=1&$format=json"
    resp = requests.get(url, timeout=60)
    print(f"HTTP {resp.status_code}  {url}")
    resp.raise_for_status()
    rows = resp.json().get("value", [])
    if not rows:
        print("No rows — list EntitySets at:")
        print(f"  {SPI_BASE}/")
        return
    print("Keys:", list(rows[0].keys()))
    print("Sample row:", rows[0])


def inspect(resource: str = DEFAULT_RESOURCE, data_date: str | None = None) -> None:
    """One-shot schema check: call ChavesPix for a month and print sample."""
    data_date = data_date or _default_data_date()
    url = (
        f"{BASE}/{resource}(Data=@d)"
        f"?@d='{data_date}'&$top=1&$format=json"
    )
    resp = requests.get(url, timeout=60)
    print(f"HTTP {resp.status_code}  {url}")
    resp.raise_for_status()
    rows = resp.json().get("value", [])
    if not rows:
        print("No rows — try an older data_date (first of month).")
        return
    print("Keys:", list(rows[0].keys()))
    print("Sample row:", rows[0])


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "inspect":
        inspect(
            sys.argv[2] if len(sys.argv) > 2 else DEFAULT_RESOURCE,
            sys.argv[3] if len(sys.argv) > 3 else None,
        )
    elif len(sys.argv) > 1 and sys.argv[1] == "inspect-spi":
        inspect_spi(sys.argv[2] if len(sys.argv) > 2 else SPI_DEFAULT_RESOURCE)
    elif len(sys.argv) > 1 and sys.argv[1] == "spi":
        rows = fetch_spi()
        print(f"{len(rows)} SPI rows")
        for r in rows[:10]:
            print(r.get("date"), r.get("tx_count"), r.get("tx_value"))
    else:
        data_date = _default_data_date()
        print(f"Fetching Pix DICT keys for {data_date} ...")
        rows = fetch_recent(data_date)
        print(f"{len(rows)} raw rows")
        for inst in by_institution(rows)[:15]:
            print(
                f"{inst['value_share_pct']:6.2f}%  "
                f"keys {inst['tx_value']:>12,.0f}  "
                f"{(inst['institution'] or inst['ispb'] or '?')[:40]}"
            )
