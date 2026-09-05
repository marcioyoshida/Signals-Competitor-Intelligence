"""Ingest CVM market-participant registries — new-entrant early warning (Job 1, E5).

The CVM publishes cadastros of registered market participants (consultores de valores
mobiliários, administradores de carteira, …). A NEW row (a CNPJ not seen before) is the
observable event: "a competitor just registered with the CVM to operate". Mirrors the
`bcb_autorizacoes` pattern — this module fetches + normalizes; the Lambda handler runs
`detect_new` (seed-suppressed on first run) → the `entrants` lens → discovery + Receita enrich.

Endpoints DISCOVERED via the CVM CKAN catalog (`/api/3/action/package_show`) — the authoritative
resource URLs, not guessed. Verified live 2026-09-05 (consultores: 1057 PJ rows).

Extensible: add a CADASTROS spec (url + csv + industry) to cover more CVM participant registries.
"""
from __future__ import annotations

import csv
import io
import zipfile
from typing import Any

import requests

# CVM participant cadastros → (DADOS zip, the PJ csv inside, the Onça industry the participant
# competes in). URLs are the CKAN-authoritative resource URLs (dados.cvm.gov.br/dados/<pkg>/...).
CADASTROS: dict[str, dict[str, str]] = {
    "consultores": {
        "url": "https://dados.cvm.gov.br/dados/CONSULTOR_VLMOB/CAD/DADOS/cad_consultor_vlmob.zip",
        "csv": "cad_consultor_vlmob_pj.csv", "industry": "advisory", "source": "CVM-Consultores"},
    # e.g. administradores de carteira (adm_cart-cad) can be added the same way once its PJ csv
    # column set is verified.
}
_ACTIVE_HINT = "FUNCIONAMENTO"  # SIT column value for an operating participant


def parse_csv(text: str, source: str, industry: str) -> list[dict[str, Any]]:
    """Parse a CVM cadastro PJ CSV (semicolon-delimited) into normalized entrant records. Pure —
    no network — so it is unit-tested against a fixture."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    for row in reader:
        cnpj = (row.get("CNPJ") or "").strip()
        sit = (row.get("SIT") or "").strip()
        if not cnpj or _ACTIVE_HINT not in sit.upper():
            continue  # only participants currently in operation
        if cnpj in seen:
            continue
        seen.add(cnpj)
        name = (row.get("DENOM_SOCIAL") or row.get("DENOM_COMERC") or "").strip()
        out.append({
            "id": f"cvm-part:{cnpj}",
            "source": source,
            "kind": "competitor",
            "cnpj": cnpj,
            "name": name,
            "brand": (row.get("DENOM_COMERC") or "").strip() or None,
            "entity_type": source,
            "industry": industry,
            "registered": (row.get("DT_REG") or "").strip() or None,
            "status": sit,
            "uf": (row.get("UF") or "").strip() or None,
            "site": (row.get("SITE_ADMIN") or "").strip() or None,
        })
    return out


def _fetch_zip_csv(url: str, csv_name: str) -> str:
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        name = next((n for n in zf.namelist() if n.endswith(csv_name)), None)
        if not name:
            name = next((n for n in zf.namelist() if n.lower().endswith("_pj.csv")), None)
        if not name:
            return ""
        raw = zf.read(name)
    # CVM cadastros are latin-1 / cp1252
    for enc in ("latin-1", "cp1252", "utf-8"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("latin-1", "replace")


def fetch_participants(cadastros: dict[str, dict[str, str]] | None = None) -> list[dict[str, Any]]:
    """Fetch + normalize every configured CVM participant cadastro. One record per active CNPJ."""
    cadastros = cadastros or CADASTROS
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in cadastros.values():
        try:
            text = _fetch_zip_csv(spec["url"], spec["csv"])
        except Exception as exc:  # pragma: no cover - upstream best-effort
            print(f"Warning: CVM participantes fetch failed for {spec.get('source')}: {exc}")
            continue
        for rec in parse_csv(text, spec["source"], spec["industry"]):
            if rec["id"] in seen:
                continue
            seen.add(rec["id"])
            out.append(rec)
    return out
