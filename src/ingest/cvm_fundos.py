"""Ingest CVM fund registry — competitor product-filing / launch signals.

RCVM 175 live cadastral data (fundos + classes) lives in:
  https://dados.cvm.gov.br/dados/FI/CAD/DADOS/registro_fundo_classe.zip

Legacy cad_fi.csv is mostly cancelled / non-adapted funds and is no longer
usable for active watchlists (see DATA_SOURCES.md).

A NEW fund (or class) appearing for a watchlisted admin/gestor is the
'competitor launched a product' signal (detect_new on stable CNPJ ids).

Source package: https://dados.cvm.gov.br/dataset/fi-cad
"""
from __future__ import annotations

import csv
import io
import zipfile
from typing import Any

import requests

REGISTRO_ZIP = (
    "https://dados.cvm.gov.br/dados/FI/CAD/DADOS/registro_fundo_classe.zip"
)
DATASET_URL = "https://dados.cvm.gov.br/dataset/fi-cad"

# Keep for reference / emergency fallback only — not used by default.
LEGACY_CAD_FI_URL = "https://dados.cvm.gov.br/dados/FI/CAD/DADOS/cad_fi.csv"


def _digits(cnpj: str | None) -> str:
    return "".join(ch for ch in (cnpj or "") if ch.isdigit())


def _is_active(situacao: str | None) -> bool:
    sit = (situacao or "").upper()
    if not sit:
        return True
    if sit.startswith("CANCEL") or "LIQUID" in sit or "INCORPOR" in sit:
        return False
    # Prefer explicit functioning; allow pré-operacional as early signal
    return True


def fetch_funds(
    watchlist_admins: list[str] | None = None,
    *,
    include_classes: bool = True,
    registro_url: str = REGISTRO_ZIP,
) -> list[dict[str, Any]]:
    """Fetch active RCVM 175 funds (and optional classes) for watchlisted admins.

    watchlist_admins: case-insensitive substrings matched against
    Administrador and Gestor. Empty → return [] (universe is huge).
    """
    watch = [w.upper() for w in (watchlist_admins or []) if w]
    if not watch:
        return []

    resp = requests.get(registro_url, timeout=300)
    resp.raise_for_status()

    funds_by_id: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        with zf.open("registro_fundo.csv") as fh:
            text = io.TextIOWrapper(fh, encoding="latin-1", newline="")
            for row in csv.DictReader(text, delimiter=";"):
                if not _is_active(row.get("Situacao")):
                    continue
                admin = row.get("Administrador") or ""
                gestor = row.get("Gestor") or ""
                blob = f"{admin} {gestor}".upper()
                if not any(w in blob for w in watch):
                    continue
                fid = row.get("ID_Registro_Fundo") or ""
                cnpj = _digits(row.get("CNPJ_Fundo"))
                if not cnpj:
                    continue
                rec = {
                    "id": f"cvm:fund:{cnpj}",
                    "source": "CVM",
                    "kind": "competitor",
                    "fund_name": row.get("Denominacao_Social"),
                    "fund_class": row.get("Tipo_Fundo"),
                    "admin": admin or None,
                    "manager": gestor or None,
                    "registered": (row.get("Data_Registro") or "")[:10] or None,
                    "started": (row.get("Data_Constituicao") or "")[:10] or None,
                    "situation": row.get("Situacao"),
                    "cnpj": cnpj,
                    "adapted_rcvm175": (row.get("Data_Adaptacao_RCVM175") or "")[:10]
                    or None,
                    "url": DATASET_URL,
                    "registry": "registro_fundo",
                }
                funds_by_id[fid] = rec
                if rec["id"] not in seen:
                    seen.add(rec["id"])
                    out.append(rec)

        if include_classes and "registro_classe.csv" in zf.namelist():
            with zf.open("registro_classe.csv") as fh:
                text = io.TextIOWrapper(fh, encoding="latin-1", newline="")
                for row in csv.DictReader(text, delimiter=";"):
                    if not _is_active(row.get("Situacao")):
                        continue
                    fid = row.get("ID_Registro_Fundo") or ""
                    parent = funds_by_id.get(fid)
                    if not parent:
                        continue
                    cnpj = _digits(row.get("CNPJ_Classe"))
                    if not cnpj:
                        continue
                    cid = f"cvm:class:{cnpj}"
                    if cid in seen:
                        continue
                    seen.add(cid)
                    out.append(
                        {
                            "id": cid,
                            "source": "CVM",
                            "kind": "competitor",
                            "fund_name": row.get("Denominacao_Social")
                            or parent.get("fund_name"),
                            "fund_class": row.get("Tipo_Classe")
                            or row.get("Classificacao")
                            or parent.get("fund_class"),
                            "admin": parent.get("admin"),
                            "manager": parent.get("manager"),
                            "registered": (row.get("Data_Registro") or "")[:10]
                            or parent.get("registered"),
                            "started": (row.get("Data_Inicio") or row.get("Data_Constituicao") or "")[
                                :10
                            ]
                            or None,
                            "situation": row.get("Situacao"),
                            "cnpj": cnpj,
                            "parent_fund_cnpj": parent.get("cnpj"),
                            "url": DATASET_URL,
                            "registry": "registro_classe",
                        }
                    )

    out.sort(key=lambda r: r.get("registered") or "", reverse=True)
    return out


def inspect(watchlist: list[str] | None = None) -> None:
    watch = watchlist or ["ITAU", "BTG PACTUAL", "XP"]
    print(f"Registry: {REGISTRO_ZIP}")
    rows = fetch_funds(watchlist_admins=watch)
    print(f"{len(rows)} active funds/classes for {watch}")
    for r in rows[:15]:
        print(
            f"  [{r.get('registered')}] {(r.get('registry') or ''):16} "
            f"{(r.get('admin') or '')[:28]:28} {(r.get('fund_name') or '')[:45]}"
        )


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "inspect":
        inspect(sys.argv[2:] or None)
    else:
        watch = sys.argv[1:] or ["ITAU", "BTG"]
        sample = fetch_funds(watchlist_admins=watch)
        print(f"{len(sample)} active watchlisted funds/classes")
        for f in sample[:15]:
            print(
                f"{f.get('registered')}  {(f.get('admin') or '')[:35]:35}  "
                f"{(f.get('fund_name') or '')[:55]}"
            )
