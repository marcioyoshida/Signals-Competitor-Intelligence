"""Ingest CVM FIAGRO Informe Mensal — structured agri-fund universe + signals.

FIAGRO (Fundos de Investimento nas Cadeias Produtivas Agroindustriais) have
their own CVM open-data package (separate from FII and from the generic FI
informe diário). This module:

1. Fetches the latest available monthly informe (or a specific yyyymm).
2. Returns one record per active class with CNPJ, name, ISIN, admin, gestor,
   PL, cotistas, and a derived B3-style ticker when the ISIN encodes one.
3. Is the **high-precision structured source** for agri-funds entity discovery
   (ADR 011 / issue #14): every row carries a CNPJ — safe for auto-create /
   enrichment of the entities registry under industry ``agri-funds``.

Source package: https://dados.cvm.gov.br/dataset/fiagro-doc-inf_mensal
  ZIP pattern: .../FIAGRO/DOC/INF_MENSAL/DADOS/inf_mensal_fiagro_{YYYYMM}.zip
  Members: inf_mensal_fiagro_{YYYYMM}.csv (+ optional subclasse file)

Status: first vertical of the entity-discovery pipeline (structured official
registry sync). Complements the deferred FII plan
(docs/2026-08-20-fii-structured-source-plan.md).
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
import zipfile
from typing import Any

import requests

BASE = "https://dados.cvm.gov.br/dados/FIAGRO/DOC/INF_MENSAL/DADOS"
ZIP_TMPL = f"{BASE}/inf_mensal_fiagro_{{yyyymm}}.zip"
DATASET_URL = "https://dados.cvm.gov.br/dataset/fiagro-doc-inf_mensal"

# Brazilian fund ISIN → B3 ticker: BR + 4-char root + CTF… (or other suffixes).
# Examples: BRKNCACTF014 → KNCA11, BRXPAGCTF005 → XPAG11, BRVGIACTF004 → VGIA11,
# BRRURAR01M16 → RURA11. Unit funds trade as root + 11.
# Some ISINs encode a leading digit (BR0BVU…); those are not valid B3 letter
# roots — return None and let the curator / news path supply the ticker.
_ISIN_TICKER = re.compile(r"^BR([A-Z]{4})", re.I)


def _digits(cnpj: str | None) -> str:
    return "".join(ch for ch in (cnpj or "") if ch.isdigit())


def _parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _ticker_from_isin(isin: str | None) -> str | None:
    """Derive the common B3 unit ticker (XXXX11) from a Brazilian fund ISIN.

    Only letter roots (A–Z×4) are accepted — digit-leading ISINs are left
    without a derived ticker (still discoverable via CNPJ + name).
    """
    s = (isin or "").strip().upper()
    m = _ISIN_TICKER.match(s)
    if not m:
        return None
    root = m.group(1)
    return f"{root}11"


def latest_yyyymm(months_back: int = 0) -> str:
    """Most recent complete month (or N months earlier). CVM files lag ~1–2 mo."""
    d = dt.date.today().replace(day=1)
    for _ in range(months_back + 1):  # skip current partial month
        d = (d - dt.timedelta(days=1)).replace(day=1)
    return d.strftime("%Y%m")


def _find_available_yyyymm(max_lookback: int = 6) -> str | None:
    """Probe the directory for the newest ZIP that exists (HEAD)."""
    for back in range(max_lookback):
        yyyymm = latest_yyyymm(back)
        url = ZIP_TMPL.format(yyyymm=yyyymm)
        try:
            r = requests.head(url, timeout=20, allow_redirects=True)
            if r.status_code == 200:
                return yyyymm
        except requests.RequestException:
            continue
    return None


def fetch_fiagro(
    yyyymm: str | None = None,
    *,
    min_pl: float = 0.0,
    zip_url: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch FIAGRO class rows for a competency month.

    Returns normalized records suitable for entity discovery and move detection:
      id, source, kind, cnpj, fund_name, isin, ticker, admin, manager,
      admin_cnpj, manager_cnpj, pl, cotistas, registered, url, as_of, industry.

    ``min_pl`` drops tiny / test vehicles (default 0 = keep all).
    """
    if zip_url is None:
        yyyymm = yyyymm or _find_available_yyyymm() or latest_yyyymm()
        zip_url = ZIP_TMPL.format(yyyymm=yyyymm)
    else:
        # try to recover yyyymm from the URL for as_of
        m = re.search(r"(\d{6})", zip_url)
        yyyymm = yyyymm or (m.group(1) if m else latest_yyyymm())

    resp = requests.get(zip_url, timeout=180)
    resp.raise_for_status()

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        # Prefer the main class file; ignore subclasse for discovery (it has no admin).
        main = next(
            (n for n in zf.namelist() if n.startswith("inf_mensal_fiagro_") and "subclasse" not in n and n.endswith(".csv")),
            None,
        )
        if not main:
            return []

        with zf.open(main) as fh:
            text = io.TextIOWrapper(fh, encoding="latin-1", newline="")
            for row in csv.DictReader(text, delimiter=";"):
                cnpj = _digits(row.get("CNPJ_Classe"))
                if len(cnpj) < 14:
                    continue
                if cnpj in seen:
                    continue
                pl = _parse_float(row.get("Patrimonio_Liquido")) or 0.0
                if pl < min_pl:
                    continue
                seen.add(cnpj)
                isin = (row.get("Codigo_ISIN") or "").strip() or None
                ticker = _ticker_from_isin(isin)
                name = (row.get("Nome_Classe") or "").strip()
                admin = (row.get("Nome_Administrador") or "").strip() or None
                gestor = (row.get("Nome_Gestor") or "").strip() or None
                as_of = (row.get("Data_Referencia") or "")[:10] or f"{yyyymm[:4]}-{yyyymm[4:]}-01"
                out.append(
                    {
                        "id": f"cvm:fiagro:{cnpj}",
                        "source": "CVM",
                        "kind": "competitor",
                        "fund_name": name,
                        "fund_class": "FIAGRO",
                        "cnpj": cnpj,
                        "isin": isin,
                        "ticker": ticker,
                        "admin": admin,
                        "manager": gestor,
                        "admin_cnpj": _digits(row.get("CNPJ_Administrador")) or None,
                        "manager_cnpj": _digits(row.get("CNPJ_Gestor")) or None,
                        "pl": pl,
                        "cotistas": _parse_float(row.get("Numero_Cotistas")),
                        "registered": (row.get("Data_Registro") or "")[:10] or None,
                        "publico_alvo": (row.get("Publico_Alvo") or "").strip() or None,
                        "url": DATASET_URL,
                        "as_of": as_of,
                        "yyyymm": yyyymm,
                        "industry": "agri-funds",
                        "registry": "fiagro_inf_mensal",
                    }
                )

    out.sort(key=lambda r: r.get("pl") or 0.0, reverse=True)
    return out


# ── Move detection (task b: informe → narrative signal) ────────────────────
# fetch_fiagro() already returns one row per class per competency month. These
# helpers shape that same list for the generic value-state move engine
# (src.diff.engine.detect_moves / DynamoDbValueState) — the same primitive
# already used for BCB Pix, juros médios, and CVM Informe Diário AUM moves
# (see src/ingest/lambda_port.py `_moves_since_last_run`). No bespoke snapshot
# storage needed: detect_moves(key_field="cnpj", value_field=...) persists the
# prior value per source name and returns only rows whose value moved
# >= min_pct, annotated with prev_value/delta/pct_change. First run seeds the
# baseline (no prior value to diff against) and reports nothing — intentional.

# Below this prior cotista count, percentage swings are noise (a qualified-
# investor class with 5 cotistas going to 7 reads as "+40%"). Filtered before
# handing rows to detect_moves so both the baseline and every later reading
# only track well-populated cotista bases.
COTISTA_MIN_BASE = 20


def for_pl_moves(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows with a CNPJ + PL — the AUM series for detect_moves(value_field='pl')."""
    out = []
    for r in rows:
        if not r.get("cnpj") or r.get("pl") is None:
            continue
        row = dict(r)
        # Event-scoped id: a fund with BOTH a PL move and a cotista move in one
        # run must not collide (candidates._section_pool dedups by id, which would
        # silently drop the second event).
        row["id"] = f"{r.get('id') or ('cvm:fiagro:' + str(r.get('cnpj')))}:pl"
        row["event"] = "pl_move"
        out.append(row)
    return out


def for_cotista_moves(
    rows: list[dict[str, Any]], min_base: int = COTISTA_MIN_BASE
) -> list[dict[str, Any]]:
    """Cotista-count series for detect_moves(value_field='cotistas').

    Drops the ``pl`` field: a cotista move and a PL move are different metrics,
    and downstream narrative rendering (``synthesize._describe_signal``) picks
    a branch by field presence — keeping both would let a cotista-surge event
    be mislabeled with the *PL* percentage change.
    """
    out = []
    for r in rows:
        cot = r.get("cotistas")
        if not r.get("cnpj") or cot is None:
            continue
        try:
            if float(cot) < min_base:
                continue
        except (TypeError, ValueError):
            continue
        row = {k: v for k, v in r.items() if k != "pl"}
        row["id"] = f"{r.get('id') or ('cvm:fiagro:' + str(r.get('cnpj')))}:cotista"
        row["event"] = "cotista_move"
        out.append(row)
    return out


def for_new_registrations(
    rows: list[dict[str, Any]],
    *,
    as_of: dt.date | None = None,
    lookback_days: int = 60,
) -> list[dict[str, Any]]:
    """Rows whose ``Data_Registro`` falls within ``lookback_days`` — candidate
    "new class" events. A fund's registration date never changes, so this is
    not itself deduplicated across runs: the caller must diff the result
    through ``detect_new`` (state key = the ``id`` set below, scoped to CNPJ)
    so a fund already alerted once does not resurface every month it happens
    to still fall inside the window.
    """
    cutoff = (as_of or dt.date.today()) - dt.timedelta(days=lookback_days)
    out = []
    for r in rows:
        cnpj = r.get("cnpj")
        reg = r.get("registered")
        if not cnpj or not reg:
            continue
        try:
            d = dt.date.fromisoformat(str(reg)[:10])
        except ValueError:
            continue
        if d < cutoff:
            continue
        row = dict(r)
        row["id"] = f"fiagro:newreg:{cnpj}"
        row["event"] = "new_registration"
        out.append(row)
    return out


def inspect(months_back: int = 0, top: int = 15) -> None:
    yyyymm = _find_available_yyyymm() or latest_yyyymm(months_back)
    rows = fetch_fiagro(yyyymm)
    print(f"FIAGRO {yyyymm}: {len(rows)} classes from {ZIP_TMPL.format(yyyymm=yyyymm)}")
    for r in rows[:top]:
        pl_mi = (r.get("pl") or 0) / 1e6
        print(
            f"  {r.get('ticker') or '----':6}  "
            f"{(r.get('cnpj') or '')[:14]:14}  "
            f"PL R${pl_mi:8.1f} mi  "
            f"{(r.get('fund_name') or '')[:48]}"
        )


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "inspect":
        inspect()
    else:
        yyyymm = sys.argv[1] if len(sys.argv) > 1 else None
        sample = fetch_fiagro(yyyymm)
        print(f"{len(sample)} FIAGRO classes")
        for f in sample[:12]:
            print(
                f"{f.get('ticker') or '----':6}  "
                f"{(f.get('fund_name') or '')[:55]}  "
                f"PL={(f.get('pl') or 0)/1e6:.0f}mi"
            )
