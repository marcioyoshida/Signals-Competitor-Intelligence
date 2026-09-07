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

# Optional segment filter (case-insensitive substring). Empty = keep all
# (classification below prioritizes fintech licenses without dropping the rest).
RELEVANT_TYPES: list[str] = []

# Normalize the registry's SEGMENTO/CLASSE into a compact license class. The
# fintech-entrant licenses (IP, SCD, SEP, SCFI, microcrédito) are what a
# "quiet registration" radar cares about; ordered so the first substring match
# wins. Values verified live against SedesSociedades/SedesBancoComMultCE.
_LICENSE_RULES: list[tuple[str, str]] = [
    ("instituição de pagamento", "Instituição de Pagamento"),
    ("crédito direto", "Crédito Direto (SCD)"),
    ("empréstimo entre pessoas", "Empréstimo P2P (SEP)"),
    ("financiamento e investimento", "Financeira (SCFI)"),
    ("microempreendedor", "Microcrédito (SCMEPP)"),
    ("banco", "Banco"),
    ("corretora", "Corretora/DTVM"),
    ("distribuidora", "Corretora/DTVM"),
    ("arrendamento", "Leasing"),
    ("fomento", "Agência de Fomento"),
    ("hipotecária", "Companhia Hipotecária"),
    ("cooperativa", "Cooperativa"),
    # SedesCooperativas rows classify as "Singular" / "Central".
    ("singular", "Cooperativa"),
    ("central", "Cooperativa"),
    ("consórcio", "Consórcio"),
    ("consorcio", "Consórcio"),
]

# License classes considered "fintech entrants" for the radar.
FINTECH_LICENSES = frozenset({
    "Instituição de Pagamento",
    "Crédito Direto (SCD)",
    "Empréstimo P2P (SEP)",
    "Financeira (SCFI)",
    "Microcrédito (SCMEPP)",
})


def classify_license(entity_type: str | None) -> str:
    """Map a raw SEGMENTO/CLASSE to a compact license class label."""
    text = (entity_type or "").lower()
    for needle, label in _LICENSE_RULES:
        if needle in text:
            return label
    return (entity_type or "Outro").strip() or "Outro"


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
    license_class = classify_license(entity_type)
    return {
        "id": f"bcb-auth:{ident}",
        "source": "BCB-Autorizacoes",
        "kind": "competitor",
        "cnpj": cnpj,
        "name": name,
        "entity_type": entity_type,
        "license_class": license_class,
        "is_fintech": license_class in FINTECH_LICENSES,
        "legal_nature": None,
        "situation": "em_funcionamento",
        "registry": resource,
        "uf": row.get("UF"),
        "municipio": row.get("MUNICIPIO"),
        "raw": row,
    }


# --- R2 / #74: register-VERIFIED certifications for tracked entities -------------------
# The in-operation registry is a real authorization fact per institution (CNPJ + SEGMENTO/CLASSE +
# "em funcionamento"). Joining it to the tracked entities by CNPJ root lifts `certifications` from
# 0% real to a register-verified value — the honest answer to the CPO decision "is competitor X
# authorized for segment Y?", which the industry-INFERRED derivation could only assume.
def certification_label(row: dict[str, Any]) -> str:
    """A verified certification string from an in-operation registry row:
    ``BCB · <license_class> · em funcionamento``."""
    lic = (row.get("license_class") or row.get("entity_type") or "").strip() or "instituição autorizada"
    return f"BCB · {lic} · em funcionamento"


def certifications_by_cnpj(authorized: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Map CNPJ 8-digit root → the set of verified BCB certification labels. Rows without a CNPJ
    are skipped (they cannot be joined to a tracked entity)."""
    out: dict[str, set[str]] = {}
    for r in authorized:
        root = "".join(ch for ch in str(r.get("cnpj") or "") if ch.isdigit())[:8]
        if not root:
            continue
        out.setdefault(root, set()).add(certification_label(r))
    return out


def apply_verified_certifications(
    authorized: list[dict[str, Any]], *, source: str = "structured", table: Any | None = None
) -> list[str]:
    """#74/R2: stamp register-verified ``certifications`` onto every tracked entity that resolves to
    a BCB in-operation row (by CNPJ root). ``structured`` provenance — wins over the industry-
    inferred derivation but (ADR-018) never demotes a curated list. Returns the entity_ids updated.
    Best-effort/pure-local (no network): iterates the already-fetched ``authorized`` rows."""
    from src.synth import entity_registry as er

    changed: list[str] = []
    seen: set[str] = set()
    for root, labels in certifications_by_cnpj(authorized).items():
        eid = er.resolve_by_cnpj(root, table=table)
        if not eid or eid in seen:
            continue
        seen.add(eid)
        cur = set((er.get_entity(eid, table=table) or {}).get("certifications") or [])
        if er.set_certifications(eid, cur | labels, source=source, table=table):
            changed.append(eid)
    return changed


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
