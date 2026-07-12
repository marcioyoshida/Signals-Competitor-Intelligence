"""Ingest CVM fund registry — competitor product-filing signals.

CVM Dados Abertos publishes a daily-refreshed CSV of all registered
investment funds. A NEW fund appearing for an institution on the
watchlist is a 'competitor launched a product' signal.

Source: https://dados.cvm.gov.br/dataset/fi-cad
"""
from __future__ import annotations

import csv
import io
from typing import Any

import requests

CAD_FI_URL = "https://dados.cvm.gov.br/dados/FI/CAD/DADOS/cad_fi.csv"


def fetch_funds(watchlist_admins: list[str] | None = None) -> list[dict[str, Any]]:
    """Fetch active funds, optionally filtered to watchlisted administrators.

    The CSV is large (~100MB+); this streams it and keeps only active
    funds whose administrator matches a watchlist substring.
    """
    watch = [w.upper() for w in (watchlist_admins or [])]
    resp = requests.get(CAD_FI_URL, timeout=300, stream=True)
    resp.raise_for_status()
    resp.encoding = "latin-1"  # CVM CSVs are latin-1, semicolon-delimited

    funds = []
    reader = csv.DictReader(io.StringIO(resp.text), delimiter=";")
    for row in reader:
        if row.get("SIT") != "EM FUNCIONAMENTO NORMAL":
            continue
        admin = (row.get("ADMIN") or "").upper()
        if watch and not any(w in admin for w in watch):
            continue
        funds.append(
            {
                "id": f"cvm:fund:{row.get('CNPJ_FUNDO', '?')}",
                "source": "CVM",
                "kind": "competitor",
                "fund_name": row.get("DENOM_SOCIAL"),
                "fund_class": row.get("CLASSE"),
                "admin": row.get("ADMIN"),
                "manager": row.get("GESTOR"),
                "registered": row.get("DT_REG"),
                "started": row.get("DT_INI_ATIV"),
            }
        )
    return funds


if __name__ == "__main__":
    sample = fetch_funds(watchlist_admins=["ITAU", "BTG"])
    print(f"{len(sample)} active watchlisted funds")
    for f in sample[:10]:
        print(f"{f['registered']}  {f['admin'][:35]:35}  {f['fund_name'][:60]}")
