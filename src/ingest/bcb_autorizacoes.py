"""Ingest BCB institutions-in-operation registry — new-entrant early warning.

BCB does not publish a clean 'pending authorization process' feed. What it
does publish is the list of institutions currently in operation. A NEW
entity appearing in that registry is the observable event: 'a competitor
just cleared authorization to operate'.

Live API (verified 2026-07-19):
  Instituicoes_em_funcionamento v1 — working EntitySets
  https://olinda.bcb.gov.br/olinda/servico/Instituicoes_em_funcionamento/versao/v1

  EntitySets used:
    - SedesBancoComMultCE  (banks + foreign bank branches)
    - SedesSociedades      (non-bank societies, incl. payment institutions)
    - SedesCooperativas
    - SedesConsorcios

BcBase v2's EntidadesSupervisionadas is a FunctionImport(dataBase) that
currently returns HTTP 500 for known date formats — kept as a future
fallback once BCB fixes it, not used as the primary path.

Lambda port note: handler wraps fetch_authorized(); JsonState → DynamoDB.
"""
from __future__ import annotations

from typing import Any

import requests

FUNCIONAMENTO = (
    "https://olinda.bcb.gov.br/olinda/servico/Instituicoes_em_funcionamento/versao/v1/odata"
)

# Working EntitySets on Instituicoes_em_funcionamento (live-verified).
DEFAULT_RESOURCES: list[str] = [
    "SedesBancoComMultCE",
    "SedesSociedades",
    "SedesCooperativas",
    "SedesConsorcios",
]

# Optional segment filter (case-insensitive substring). Empty = keep all.
# Examples: "Instituição de Pagamento", "Sociedade de Crédito Direto".
RELEVANT_TYPES: list[str] = []


def fetch_authorized(
    resources: list[str] | None = None, top: int = 10000
) -> list[dict[str, Any]]:
    """Fetch the current in-operation institutions registry (all resources)."""
    resources = resources or DEFAULT_RESOURCES
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for resource in resources:
        url = f"{FUNCIONAMENTO}/{resource}?$top={top}&$format=json"
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        for row in resp.json().get("value", []):
            if not _keep(row):
                continue
            rec = _normalize(row, resource)
            if rec["id"] in seen:
                continue
            seen.add(rec["id"])
            out.append(rec)
    return out


def _keep(row: dict[str, Any]) -> bool:
    if not RELEVANT_TYPES:
        return True
    etype = (row.get("SEGMENTO") or row.get("CLASSE") or "")
    return any(t.lower() in etype.lower() for t in RELEVANT_TYPES)


def _normalize(row: dict[str, Any], resource: str) -> dict[str, Any]:
    """Map a live Instituicoes_em_funcionamento row to a signal record."""
    cnpj = row.get("CNPJ") or row.get("Cnpj")
    name = row.get("NOME_INSTITUICAO") or row.get("Nome")
    entity_type = (
        row.get("SEGMENTO")
        or row.get("CLASSE")
        or resource  # cooperatives/consortia lack SEGMENTO
    )
    ident = cnpj or name or "unknown"
    return {
        "id": f"bcb-auth:{ident}",
        "source": "BCB-Autorizacoes",
        "kind": "competitor",
        "cnpj": cnpj,
        "name": name,
        "entity_type": entity_type,
        "legal_nature": None,
        "situation": "em_funcionamento",
        "registry": resource,
        "uf": row.get("UF"),
        "municipio": row.get("MUNICIPIO"),
        "raw": row,
    }


def inspect(resource: str | None = None) -> None:
    """One-shot schema check against a live EntitySet."""
    resource = resource or DEFAULT_RESOURCES[0]
    url = f"{FUNCIONAMENTO}/{resource}?$top=1&$format=json"
    resp = requests.get(url, timeout=60)
    print(f"HTTP {resp.status_code}  {url}")
    resp.raise_for_status()
    rows = resp.json().get("value", [])
    if not rows:
        print("No rows — list EntitySets at:")
        print(f"  {FUNCIONAMENTO}/")
        return
    print("Keys:", list(rows[0].keys()))
    print("Sample row:", rows[0])


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "inspect":
        inspect(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        rows = fetch_authorized()
        print(f"{len(rows)} institutions in operation")
        for r in rows[:15]:
            print(
                f"  {(r['entity_type'] or '?')[:30]:30}  "
                f"{(r['name'] or r['cnpj'] or '?')[:50]}"
            )
