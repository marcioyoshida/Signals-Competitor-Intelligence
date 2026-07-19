"""Ingest CVM public securities offerings — capital-raise / product-launch signal.

CVM Dados Abertos publishes Ofertas Públicas de Distribuição (ICVM 400 /
RCVM 160 / restricted efforts legacy). A NEW offering in the file is a
competitor (issuer or lead coordinator) raising capital or launching a
securitized product.

Source package: https://dados.cvm.gov.br/dataset/oferta-distrib
ZIP: https://dados.cvm.gov.br/dados/OFERTA/DISTRIB/DADOS/oferta_distribuicao.zip

Contents (live-verified 2026-07-19):
  - oferta_resolucao_160.csv  — RCVM 160 (active; ~14k rows, recent activity)
  - oferta_distribuicao.csv   — older ICVM 400/476-era history (~49k; few recent)

Primary path: resolucao_160. Historical file still scanned for recent dates.

Signal: detect_new on stable offering ids. First Lambda/local run seeds
baseline (seed_if_empty) so the full lookback window is not alerted as new.

Lambda port note: handler wraps fetch_recent(); DynamoDB state for ids.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import zipfile
from typing import Any, BinaryIO, Iterable

import requests

ZIP_URL = "https://dados.cvm.gov.br/dados/OFERTA/DISTRIB/DADOS/oferta_distribuicao.zip"

# RCVM 160 is the live series; legacy file kept for completeness.
FILE_RESOLUCAO_160 = "oferta_resolucao_160.csv"
FILE_LEGACY = "oferta_distribuicao.csv"

DEFAULT_LOOKBACK_DAYS = 30


def fetch_recent(
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    watchlist: list[str] | None = None,
    include_legacy: bool = True,
    zip_url: str = ZIP_URL,
) -> list[dict[str, Any]]:
    """Fetch offerings with event date within lookback_days.

    watchlist: case-insensitive substrings matched against issuer, lead,
    ofertante, administrator, manager. Empty/None = keep all recent offerings.
    """
    cutoff = dt.date.today() - dt.timedelta(days=lookback_days)
    watch = [w.upper() for w in (watchlist or []) if w]

    resp = requests.get(zip_url, timeout=300)
    resp.raise_for_status()

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = set(zf.namelist())
        if FILE_RESOLUCAO_160 in names:
            for rec in _iter_resolucao_160(zf.open(FILE_RESOLUCAO_160)):
                if not _keep(rec, cutoff, watch):
                    continue
                if rec["id"] in seen:
                    continue
                seen.add(rec["id"])
                out.append(rec)
        if include_legacy and FILE_LEGACY in names:
            for rec in _iter_legacy(zf.open(FILE_LEGACY)):
                if not _keep(rec, cutoff, watch):
                    continue
                if rec["id"] in seen:
                    continue
                seen.add(rec["id"])
                out.append(rec)

    out.sort(key=lambda r: r.get("event_date") or "", reverse=True)
    return out


def _keep(rec: dict[str, Any], cutoff: dt.date, watch: list[str]) -> bool:
    event = _parse_date(rec.get("event_date"))
    if not event or event < cutoff:
        return False
    if not watch:
        return True
    blob = " ".join(
        str(rec.get(k) or "")
        for k in ("issuer", "leader", "offeror", "admin", "manager")
    ).upper()
    return any(w in blob for w in watch)


def _iter_resolucao_160(fh: BinaryIO) -> Iterable[dict[str, Any]]:
    text = io.TextIOWrapper(fh, encoding="latin-1", newline="")
    reader = csv.DictReader(text, delimiter=";")
    for row in reader:
        yield _normalize_resolucao_160(row)


def _iter_legacy(fh: BinaryIO) -> Iterable[dict[str, Any]]:
    text = io.TextIOWrapper(fh, encoding="latin-1", newline="")
    reader = csv.DictReader(text, delimiter=";")
    for row in reader:
        yield _normalize_legacy(row)


def _normalize_resolucao_160(row: dict[str, Any]) -> dict[str, Any]:
    """Map RCVM 160 row (live schema) to a signal record."""
    req = (row.get("Numero_Requerimento") or "").strip()
    proc = (row.get("Numero_Processo") or "").strip()
    ident = req or proc or "unknown"
    event = (
        row.get("Data_Registro")
        or row.get("Data_requerimento")
        or row.get("Data_Encerramento")
        or ""
    )
    cnpj = _clean_cnpj(row.get("CNPJ_Emissor"))
    return {
        "id": f"cvm-oferta:r160:{ident}",
        "source": "CVM-Ofertas",
        "kind": "competitor",
        "series": "rcvm160",
        "event_date": (event or "")[:10],
        "registered": (row.get("Data_Registro") or "")[:10] or None,
        "requested": (row.get("Data_requerimento") or "")[:10] or None,
        "status": row.get("Status_Requerimento"),
        "security": row.get("Valor_Mobiliario"),
        "offer_type": row.get("Tipo_Oferta"),
        "rito": row.get("Rito_Requerimento"),
        "request_type": row.get("Tipo_requerimento"),
        "issuer": row.get("Nome_Emissor"),
        "issuer_cnpj": cnpj,
        "leader": row.get("Nome_Lider"),
        "leader_cnpj": _clean_cnpj(row.get("CNPJ_Lider")),
        "offeror": None,
        "admin": row.get("Administrador"),
        "manager": row.get("Gestor"),
        "amount": _parse_float(row.get("Valor_Total_Registrado")),
        "quantity": _parse_float(row.get("Qtde_Total_Registrada")),
        "process": proc or None,
        "url": "https://dados.cvm.gov.br/dataset/oferta-distrib",
        "raw": None,  # omit full raw to keep digest/Lambda payload small
    }


def _normalize_legacy(row: dict[str, Any]) -> dict[str, Any]:
    """Map historical oferta_distribuicao.csv row to a signal record."""
    reg = (row.get("Numero_Registro_Oferta") or "").strip()
    proc = (row.get("Numero_Processo") or "").strip()
    ident = reg or proc or "unknown"
    event = (
        row.get("Data_Inicio_Oferta")
        or row.get("Data_Registro_Oferta")
        or row.get("Data_Abertura_Processo")
        or row.get("Data_Protocolo")
        or ""
    )
    return {
        "id": f"cvm-oferta:leg:{ident}",
        "source": "CVM-Ofertas",
        "kind": "competitor",
        "series": "legacy",
        "event_date": (event or "")[:10],
        "registered": (row.get("Data_Registro_Oferta") or "")[:10] or None,
        "requested": (row.get("Data_Abertura_Processo") or "")[:10] or None,
        "status": row.get("Modalidade_Oferta"),
        "security": row.get("Tipo_Ativo"),
        "offer_type": row.get("Tipo_Oferta"),
        "rito": row.get("Rito_Oferta"),
        "request_type": row.get("Modalidade_Registro"),
        "issuer": row.get("Nome_Emissor"),
        "issuer_cnpj": _clean_cnpj(row.get("CNPJ_Emissor")),
        "leader": row.get("Nome_Lider"),
        "leader_cnpj": _clean_cnpj(row.get("CNPJ_Lider")),
        "offeror": row.get("Nome_Ofertante"),
        "admin": None,
        "manager": None,
        "amount": _parse_float(row.get("Valor_Total")),
        "quantity": _parse_float(row.get("Quantidade_Total")),
        "process": proc or None,
        "url": "https://dados.cvm.gov.br/dataset/oferta-distrib",
        "raw": None,
    }


def _clean_cnpj(value: Any) -> str | None:
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits or None


def _parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _parse_date(value: Any) -> dt.date | None:
    s = (str(value) if value is not None else "").strip()
    if not s:
        return None
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            return dt.date.fromisoformat(s[:10])
        except ValueError:
            return None
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            try:
                d, m, y = int(parts[0]), int(parts[1]), int(parts[2])
                return dt.date(y, m, d)
            except ValueError:
                return None
    return None


def inspect(zip_url: str = ZIP_URL, sample: int = 3) -> None:
    """Download ZIP, print file list + sample normalized recent rows."""
    resp = requests.get(zip_url, timeout=300)
    print(f"HTTP {resp.status_code}  {zip_url}  bytes={len(resp.content)}")
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        print("Files:", zf.namelist())
        for name in zf.namelist():
            with zf.open(name) as fh:
                text = io.TextIOWrapper(fh, encoding="latin-1", newline="")
                reader = csv.DictReader(text, delimiter=";")
                row = next(reader, None)
                print(f"\n{name} cols ({len(reader.fieldnames or [])}):", (reader.fieldnames or [])[:12], "...")
                if row:
                    print(" sample raw keys used:", {k: row.get(k) for k in list(row)[:8]})
    recent = fetch_recent(lookback_days=30)
    print(f"\nfetch_recent(30d): {len(recent)} offerings")
    for r in recent[:sample]:
        print(
            f"  [{r.get('event_date')}] {(r.get('security') or '?')[:30]:30} "
            f"{(r.get('issuer') or '?')[:35]:35} "
            f"leader={(r.get('leader') or '')[:25]}"
        )


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "inspect":
        inspect()
    else:
        days = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LOOKBACK_DAYS
        watch = sys.argv[2:] if len(sys.argv) > 2 else None
        rows = fetch_recent(lookback_days=days, watchlist=watch)
        print(f"{len(rows)} offerings in last {days}d"
              + (f" matching {watch}" if watch else ""))
        for r in rows[:25]:
            amt = r.get("amount")
            amt_s = f"R$ {amt:,.0f}" if isinstance(amt, (int, float)) else ""
            print(
                f"  [{r.get('event_date')}] {(r.get('security') or '?')[:28]:28} "
                f"{(r.get('issuer') or '?')[:32]:32} {amt_s}"
            )
