"""Ingest the SUSEP supervised-entities registry — insurance new-entrant early warning (#77, E1).

Insurance / open-pension / capitalização (SUSEP-regulated) had NO entrant detection. SUSEP's
Sistema de Estatísticas (SES) publishes the list of supervised companies as an open CSV; a NEW
row appearing (a new CodigoFIP / CNPJ) is the observable event: "a new insurer/EAPC/capitalização
company cleared authorization to operate". Same two-job shape as `bcb_autorizacoes` (Job 1):
fetch registry → `detect_new` on a stable id (seed-suppressed first run) → `entrants` lens →
Receita-enrich the CNPJ. Insurers are NOT fintech, so they surface as review-gated entrant SIGNALS
(not auto-created), exactly like the CVM-participantes path.

Live API (verified 2026-09-06):
  https://www2.susep.gov.br/menuestatistica/SES/download/LISTAEMPRESAS.csv
  HTTP 200, ~13 KB, semicolon-delimited, latin-1. Columns: CodigoFIP;NomeEntidade;CNPJ
  Covers the FIP-coded supervised operating companies (seguradoras + previdência + capitalização)
  — deliberately NOT the tens of thousands of corretoras (would flood the entrant feed).

The SUSEP PRODUCT registry (P1, "Consulta de Produtos") is dados.gov.br CKAN-only and gated behind
the (currently dead) GOV_DADOS_TOKEN — deferred, not built blind. Lambda port: handler wraps
fetch_entities(); JsonState → DynamoDB delta.
"""
from __future__ import annotations

import csv
import io
from typing import Any

import requests

LISTA_EMPRESAS = (
    "https://www2.susep.gov.br/menuestatistica/SES/download/LISTAEMPRESAS.csv"
)

# Coarse, honest license class inferred from the company name (SES gives no type column). Ordered
# so the first substring match wins; the fallback is the dominant "Seguradora".
_CLASS_RULES: list[tuple[str, str]] = [
    ("resseg", "Resseguradora"),
    ("capitaliz", "Capitalização"),
    ("previd", "Previdência aberta (EAPC)"),
    ("vida", "Seguradora (vida/previdência)"),
    ("saude", "Seguradora (saúde)"),
    ("saúde", "Seguradora (saúde)"),
]


def classify_class(name: str | None) -> str:
    text = (name or "").lower()
    for needle, label in _CLASS_RULES:
        if needle in text:
            return label
    return "Seguradora"


def _digits(v: Any) -> str:
    return "".join(ch for ch in str(v or "") if ch.isdigit())


def _first_content_line(text: str) -> int:
    """Index of the first non-blank line (the real CSV header)."""
    for i, line in enumerate(text.splitlines()):
        if line.strip():
            return i
    return 0


def parse_entities(text: str) -> list[dict[str, Any]]:
    """Parse the SES LISTAEMPRESAS CSV (semicolon-delimited) into entrant records. Rows without a
    name are skipped; the stable delta id prefers the CNPJ, else the CodigoFIP."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    # The SES file ships with a leading blank line; DictReader would take it as the header.
    text = "\n".join(text.splitlines()[_first_content_line(text):])
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    for row in reader:
        # tolerate header/case/whitespace variance
        norm = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() }
        name = norm.get("nomeentidade") or norm.get("nome")
        if not name:
            continue
        cnpj = _digits(norm.get("cnpj"))
        codigo = norm.get("codigofip") or norm.get("codigo")
        ident = cnpj or codigo or name
        if ident in seen:
            continue
        seen.add(ident)
        out.append({
            "id": f"susep-ent:{ident}",
            "source": "SUSEP-Entidades",
            "kind": "competitor",
            "cnpj": cnpj or None,
            "name": name,
            "codigo_fip": codigo or None,
            "license_class": classify_class(name),
            "is_fintech": False,
            "legal_nature": None,
            "situation": "supervisionada",
            "registry": "SES/LISTAEMPRESAS",
        })
    return out


def fetch_entities(url: str | None = None, *, timeout: int = 90) -> list[dict[str, Any]]:
    """Fetch + parse the current SUSEP supervised-companies registry."""
    resp = requests.get(url or LISTA_EMPRESAS, timeout=timeout)
    resp.raise_for_status()
    # SES CSV is latin-1; fall back to the apparent encoding if that ever changes.
    try:
        text = resp.content.decode("latin-1")
    except Exception:  # pragma: no cover - defensive
        text = resp.text
    return parse_entities(text)


def inspect() -> None:  # pragma: no cover - manual live check
    rows = fetch_entities()
    print(f"{len(rows)} SUSEP supervised entities")
    for r in rows[:15]:
        print(f"  {r['license_class']:32} {(r['name'] or '')[:44]:44} {r['cnpj'] or ''}")


if __name__ == "__main__":  # pragma: no cover
    inspect()
