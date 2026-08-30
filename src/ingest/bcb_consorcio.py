"""BCB consórcio administradoras — structured entity-discovery source (issue #46).

The Central Bank publishes the branches (filiais) of every licensed consórcio
administrator via an OLINDA OData service, each row carrying the administrator's
**CNPJ** (8-digit root) and **razão social**. We collapse the branch rows to one
row per administrator (with a branch count), which feeds entity discovery under
industry ``consorcio`` — and, where the administrator is a conglomerate's arm
(e.g. "BRADESCO ADMINISTRADORA DE CONSÓRCIOS"), links it as a sub-entity of the
tier-1 parent (ADR 017).

Source: https://dadosabertos.bcb.gov.br/dataset/filiais-de-administradoras-de-consorcio
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import requests

ODATA = (
    "https://olinda.bcb.gov.br/olinda/servico/"
    "Informes_FiliaisAdministradorasConsorcios/versao/v1/odata/Filiais"
)
DATASET_URL = "https://dadosabertos.bcb.gov.br/dataset/filiais-de-administradoras-de-consorcio"


def _cnpj_root(cnpj: str | None) -> str:
    return "".join(ch for ch in str(cnpj or "") if ch.isdigit())[:8]


def fetch_consorcio(*, max_pages: int = 40, timeout: int = 60) -> list[dict[str, Any]]:
    """One row per consórcio administrator (deduped by CNPJ root), most-branches first.

    Each row: ``{cnpj, name, branches, as_of, url}``. Follows OData paging up to
    ``max_pages`` (the dataset is a few thousand branch rows). Best-effort: returns
    what it collected on a transport error.
    """
    url: str | None = (
        f"{ODATA}?$format=json&$select=CNPJ,NomeInstituicao,Posicao&$top=1000"
    )
    by_cnpj: dict[str, dict[str, Any]] = {}
    pages = 0
    while url and pages < max_pages:
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()
        except Exception:  # pragma: no cover - best-effort, network
            break
        for row in payload.get("value") or []:
            root = _cnpj_root(row.get("CNPJ"))
            name = (row.get("NomeInstituicao") or "").strip()
            if not root or not name:
                continue
            rec = by_cnpj.setdefault(
                root, {"cnpj": root, "name": name, "branches": 0,
                       "as_of": row.get("Posicao"), "url": DATASET_URL}
            )
            rec["branches"] += 1
        url = payload.get("@odata.nextLink")
        pages += 1

    rows = sorted(by_cnpj.values(), key=lambda r: r["branches"], reverse=True)
    # Normalize as_of (Posicao is dd/mm/yyyy) to ISO where possible.
    for r in rows:
        r["as_of"] = _iso_date(r.get("as_of"))
    return rows


def _iso_date(value: Any) -> str | None:
    s = str(value or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def inspect(top: int = 20) -> None:  # pragma: no cover - manual probe
    rows = fetch_consorcio()
    print(f"{len(rows)} administradoras (as_of {rows[0]['as_of'] if rows else '-'})")
    for r in rows[:top]:
        print(f"  {r['cnpj']:>8}  {r['branches']:>4} filiais  {r['name']}")
