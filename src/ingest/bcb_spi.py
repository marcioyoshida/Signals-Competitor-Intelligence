"""Ingest BCB SPI settlement-flow statistics — competitor liquidity signal.

SPI (Sistema de Pagamentos Instantâneos) is Brazil's instant payment infrastructure
that settles transactions through participants' PI (Pagamento Instantâneo) accounts.
BCB publishes monthly aggregates covering financial movements settled in SPI, providing
a settlement-flow angle that complements the DICT/transaction view from Pix.

This module focuses specifically on SPI settlement data, which shows how much liquidity
is flowing through each institution's PI accounts — a key indicator of real payment
activity and competitor health.

API: Olinda OData
  https://olinda.bcb.gov.br/olinda/servico/SPI/versao/v1

Key resources:
- MovimentacoesContaPI: Settlement movements by institution (primary resource)
- SaldoContaPI: Balance information for PI accounts
- ParticipantesPI: List of SPI participants

IMPORTANT: The SPI service schema may vary. Use inspect() to verify field names
before relying on the default mappings.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import requests

BASE = "https://olinda.bcb.gov.br/olinda/servico/SPI/versao/v1/odata"
DEFAULT_RESOURCE = "MovimentacoesContaPI"  # Settlement movements by institution


def _default_anomes(months_back: int = 1) -> int:
    """Return YYYYMM competency month (AnoMes), defaulting to previous month.
    
    Args:
        months_back: How many months back from current date. Default 1.
    
    Returns:
        Integer in YYYYMM format representing the competency month.
    """
    d = dt.date.today().replace(day=1) - dt.timedelta(days=1)
    for _ in range(months_back - 1):
        d = d.replace(day=1) - dt.timedelta(days=1)
    return int(d.strftime("%Y%m"))


def fetch_recent(
    anomes: int | None = None,
    resource: str = DEFAULT_RESOURCE,
    top: int = 10000,
) -> list[dict[str, Any]]:
    """Fetch SPI settlement-flow stats for a competency month (YYYYMM).
    
    Args:
        anomes: Competency month in YYYYMM format. If None, uses previous month.
        resource: SPI resource name to query. Default: MovimentacoesContaPI
        top: Maximum number of records to return.
    
    Returns:
        List of normalized signal records with settlement flow data.
        Each record contains institution identifier, transaction count,
        transaction value, and raw API response.
    """
    anomes = anomes or _default_anomes()
    url = (
        f"{BASE}/{resource}"
        f"?$filter=AnoMes eq {anomes}"
        f"&$top={top}&$format=json"
    )
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    rows = resp.json().get("value", [])
    
    return [_normalize(r, anomes) for r in rows]


def _normalize(row: dict[str, Any], anomes: int) -> dict[str, Any]:
    """Map a raw SPI row to a signal record.
    
    Field names are best-effort based on BCB's schema. Common variants include:
    - ISPB / ispb: Institution identification code
    - NomeInstituicao / Instituicao: Institution name  
    - Quantidade / QuantidadeLancamentos: Number of settlements
    - Valor / ValorLancamentos: Total value settled
    
    Args:
        row: Raw API response row
        anomes: Competency month (YYYYMM)
    
    Returns:
        Normalized signal record with standard fields.
    """
    ispb = row.get("ISPB") or row.get("ispb")
    inst = row.get("NomeInstituicao") or row.get("Instituicao")
    ident = ispb or inst or "unknown"
    
    return {
        "id": f"spi:{anomes}:{ident}",
        "source": "BCB-SPI",
        "kind": "competitor",
        "anomes": anomes,
        "ispb": ispb,
        "institution": inst,
        "tx_count": row.get("Quantidade") or row.get("QuantidadeLancamentos"),
        "tx_value": row.get("Valor") or row.get("ValorLancamentos"),
        "raw": row,
    }


def by_institution(
    rows: list[dict[str, Any]], watchlist_ispb: list[str] | None = None
) -> list[dict[str, Any]]:
    """Aggregate SPI volume/count per institution, optional watchlist filter.
    
    Some SPI resources may be broken down by additional dimensions, so the same
    institution appears in many rows. This function sums them for an institution view.
    
    Args:
        rows: List of raw or normalized SPI records
        watchlist_ispb: Optional list of ISPB codes to filter by
    
    Returns:
        List of aggregated records sorted by transaction value (descending),
        with value_share_pct showing market share.
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
                "tx_count": 0.0,
                "tx_value": 0.0,
            },
        )
        a["tx_count"] += float(r.get("tx_count") or 0)
        a["tx_value"] += float(r.get("tx_value") or 0)
    
    ranked = sorted(agg.values(), key=lambda x: x["tx_value"], reverse=True)
    total_val = sum(x["tx_value"] for x in ranked) or 1.0
    
    for x in ranked:
        x["value_share_pct"] = round(100 * x["tx_value"] / total_val, 3)
    
    return ranked


def detect_moves(
    current: list[dict[str, Any]], previous: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Detect significant changes in SPI settlement patterns between periods.
    
    Compares two time periods and identifies institutions with notable changes
    in transaction volume or value. Useful for spotting competitor moves.
    
    Args:
        current: SPI records for the more recent period
        previous: SPI records for the earlier period
    
    Returns:
        List of change detection records with institution, change metrics,
        and confidence score.
    """
    # Build maps by institution key
    curr_map = {}
    prev_map = {}
    
    for r in current:
        key = r.get("ispb") or r.get("institution") or "unknown"
        curr_map[key] = {
            "tx_count": float(r.get("tx_count") or 0),
            "tx_value": float(r.get("tx_value") or 0),
        }
    
    for r in previous:
        key = r.get("ispb") or r.get("institution") or "unknown"
        prev_map[key] = {
            "tx_count": float(r.get("tx_count") or 0),
            "tx_value": float(r.get("tx_value") or 0),
        }
    
    changes = []
    all_keys = set(curr_map.keys()) | set(prev_map.keys())
    
    for key in all_keys:
        curr = curr_map.get(key, {"tx_count": 0.0, "tx_value": 0.0})
        prev = prev_map.get(key, {"tx_count": 0.0, "tx_value": 0.0})
        
        # Skip if no activity in either period
        if curr["tx_value"] == 0 and prev["tx_value"] == 0:
            continue
        
        value_change = ((curr["tx_value"] - prev["tx_value"]) / (prev["tx_value"] or 1)) * 100
        count_change = ((curr["tx_count"] - prev["tx_count"]) / (prev["tx_count"] or 1)) * 100
        
        # Confidence: higher for larger absolute changes and higher volumes
        confidence = min(
            1.0,
            abs(value_change) * 0.01 + abs(count_change) * 0.01,
            curr["tx_value"] / 1_000_000,  # Cap at 100% for R$1M+ volume
        )
        
        changes.append({
            "institution": key,
            "value_change_pct": round(value_change, 2),
            "count_change_pct": round(count_change, 2),
            "confidence": round(confidence, 3),
            "current_value": curr["tx_value"],
            "previous_value": prev["tx_value"],
        })
    
    # Sort by absolute change magnitude
    return sorted(
        changes,
        key=lambda x: abs(x["value_change_pct"]) + abs(x["count_change_pct"]),
        reverse=True,
    )


def inspect(resource: str = DEFAULT_RESOURCE) -> None:
    """One-shot schema check for the SPI service.
    
    Prints HTTP status, resource URL, first row's keys, and sample data.
    Run this FIRST against the live service to confirm resource name and
    column names before relying on default mappings.
    
    Args:
        resource: SPI resource name to inspect. Default: MovimentacoesContaPI
    """
    url = f"{BASE}/{resource}?$top=1&$format=json"
    resp = requests.get(url, timeout=60)
    print(f"HTTP {resp.status_code}  {url}")
    resp.raise_for_status()
    rows = resp.json().get("value", [])
    
    if not rows:
        print("No rows — verify the resource against the SPI swagger:")
        print("  https://olinda.bcb.gov.br/olinda/servico/SPI/versao/v1/swagger-ui2")
        return
    
    print("Keys:", list(rows[0].keys()))
    print("Sample row:", rows[0])


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "inspect":
        # python -m src.ingest.bcb_spi inspect [ResourceName]
        inspect(sys.argv[2] if len(sys.argv) > 2 else DEFAULT_RESOURCE)
    elif len(sys.argv) > 1 and sys.argv[1] == "detect":
        # python -m src.ingest.bcb_spi detect [CurrentAnomes] [PreviousAnomes]
        if len(sys.argv) < 4:
            print("Usage: python -m src.ingest.bcb_spi detect CURRENT_ANOMES PREVIOUS_ANOMES")
            sys.exit(1)
        
        current_anomes = int(sys.argv[2])
        previous_anomes = int(sys.argv[3])
        
        print(f"Fetching SPI stats for {current_anomes} and {previous_anomes} ...")
        current_rows = fetch_recent(current_anomes)
        previous_rows = fetch_recent(previous_anomes)
        
        changes = detect_moves(current_rows, previous_rows)
        print(f"\nTop 20 institutions with significant changes:")
        for i, change in enumerate(changes[:20], 1):
            inst_name = change["institution"]
            value_pct = change["value_change_pct"]
            count_pct = change["count_change_pct"]
            confidence = change["confidence"]
            
            print(
                f"{i:2d}. {inst_name[:40]:<40} "
                f"Value: {value_pct:+7.2f}%  "
                f"Count: {count_pct:+7.2f}%  "
                f"Confidence: {confidence:.3f}"
            )
    else:
        # Default: fetch recent SPI data and show top institutions
        anomes = _default_anomes()
        print(f"Fetching SPI settlement stats for {anomes} ...")
        rows = fetch_recent(anomes)
        print(f"{len(rows)} raw rows")
        
        aggregated = by_institution(rows)
        total_value = sum(x["tx_value"] for x in aggregated)
        
        print(f"\nTop 20 institutions by settlement value (Total: R$ {total_value:,.2f}):")
        for inst in aggregated[:20]:
            print(
                f"{inst['value_share_pct']:6.2f}%  "
                f"R$ {inst['tx_value']:>16,.2f}  "
                f"{(inst['institution'] or inst['ispb'] or '?')[:40]}"
            )
