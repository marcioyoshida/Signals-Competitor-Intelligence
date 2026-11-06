"""BCB correspondents register (Informes_Correspondentes) — on-demand lookup (#357).

The BCB publishes, per contracting institution, every correspondente no país it has
registered (Resolução CMN 4.935): ``CnpjContratante``, ``NomeContratante``,
``CnpjCorrespondente``, ``NomeCorrespondente``, ``Tipo``, ``Ordem``, ``MunicipioIBGE``,
``Municipio``, ``UF``, ``ServicosCorrespondentes``, ``Posicao``. Live-verified
2026-11-05: ``contains(NomeCorrespondente,'EURO17')`` returns EURO17 EMPRESARIAL
(28924327) for Banco Votorantim and EURO17 PROMOTORA (49250480) for Banco Daycoval,
Posição 04/11/2026.

The full dataset is large (one row per correspondent point), so Onça never mirrors it:
the distress watch asks for ONE brand at a time, only when that brand just turned up in a
distress headline. ``$count`` is 403 on this service; ``$top`` bounds each call.
"""
from __future__ import annotations

import re
from typing import Any

import requests

BASE = ("https://olinda.bcb.gov.br/olinda/servico/Informes_Correspondentes/versao/v1/odata/"
        "Correspondentes")
DEFAULT_TOP = 500
TIMEOUT = 25


def _odata_literal(name: str) -> str:
    # OData string literal: quotes doubled; keep only safe characters of a brand token.
    clean = re.sub(r"[^A-Z0-9 &.\-]", "", str(name or "").upper()).strip()
    return clean.replace("'", "''")


def lookup_by_name(brand: str, *, top: int = DEFAULT_TOP, get: Any = None) -> list[dict[str, Any]]:
    """Correspondent rows whose ``NomeCorrespondente`` contains ``brand`` (uppercase).
    Empty for a blank/too-short brand. Raises on HTTP errors (the caller is best-effort)."""
    lit = _odata_literal(brand)
    if len(lit) < 4:
        return []
    params = {"$format": "json", "$top": str(int(top)),
              "$filter": f"contains(NomeCorrespondente,'{lit}')",
              "$select": "CnpjContratante,NomeContratante,CnpjCorrespondente,"
                         "NomeCorrespondente,Tipo,Posicao"}
    resp = (get or requests.get)(BASE, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    rows = resp.json().get("value") or []
    # one row per (contratante, correspondente) — the register lists every service point
    seen: set[tuple[str, str]] = set()
    out = []
    for r in rows:
        k = (str(r.get("CnpjContratante")), str(r.get("CnpjCorrespondente")))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out
