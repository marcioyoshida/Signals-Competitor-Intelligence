"""Ingest CVM Informe Diário — fund AUM + daily flow signals.

CVM publishes monthly ZIP CSVs of per-fund/class daily reports:
  PL (VL_PATRIM_LIQ), quota, captação, resgate, cotistas.

Source: https://dados.cvm.gov.br/dataset/fi-doc-inf_diario
  ZIP: .../FI/DOC/INF_DIARIO/DADOS/inf_diario_fi_YYYYMM.zip

Admin names are not in the daily file. Join CNPJ_FUNDO_CLASSE to the
RCVM 175 cadastral package:
  https://dados.cvm.gov.br/dados/FI/CAD/DADOS/registro_fundo_classe.zip
  (registro_fundo.csv + registro_classe.csv)

Signal: detect_moves on PL (AUM) by class CNPJ across competency dates.
First run seeds baselines (no moves). Empty watchlist → no rows (too large
to ingest unfiltered in the prototype).
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import zipfile
from typing import Any, BinaryIO, Iterable

import requests

REGISTRO_ZIP = (
    "https://dados.cvm.gov.br/dados/FI/CAD/DADOS/registro_fundo_classe.zip"
)
INF_DIARIO_ZIP = (
    "https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/inf_diario_fi_{yyyymm}.zip"
)
DATASET_URL = "https://dados.cvm.gov.br/dataset/fi-doc-inf_diario"


def _digits(cnpj: str | None) -> str:
    return "".join(ch for ch in (cnpj or "") if ch.isdigit())


def _parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def latest_yyyymm(months_back: int = 0) -> str:
    d = dt.date.today().replace(day=1)
    for _ in range(months_back):
        d = (d - dt.timedelta(days=1)).replace(day=1)
    return d.strftime("%Y%m")


def resolve_watchlist_classes(
    watchlist_admins: list[str] | None = None,
    registro_url: str = REGISTRO_ZIP,
) -> dict[str, dict[str, str]]:
    """Map class CNPJ digits → fund/class metadata for watchlisted administrators.

    Uses RCVM 175 registro_fundo + registro_classe (not legacy cad_fi.csv).
    """
    watch = [w.upper() for w in (watchlist_admins or []) if w]
    if not watch:
        return {}

    resp = requests.get(registro_url, timeout=300)
    resp.raise_for_status()

    fundos: dict[str, dict[str, str]] = {}
    classes: dict[str, dict[str, str]] = {}

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        # fundo: ID_Registro_Fundo → admin / gest or / fund name
        with zf.open("registro_fundo.csv") as fh:
            text = io.TextIOWrapper(fh, encoding="latin-1", newline="")
            for row in csv.DictReader(text, delimiter=";"):
                sit = (row.get("Situacao") or "").upper()
                if sit.startswith("CANCEL"):
                    continue
                admin = row.get("Administrador") or ""
                if not any(w in admin.upper() for w in watch):
                    # also allow gestor match
                    gest = row.get("Gestor") or ""
                    if not any(w in gest.upper() for w in watch):
                        continue
                fid = row.get("ID_Registro_Fundo") or ""
                if not fid:
                    continue
                fundos[fid] = {
                    "admin": admin,
                    "manager": row.get("Gestor") or "",
                    "fund_name": row.get("Denominacao_Social") or "",
                    "cnpj_fundo": _digits(row.get("CNPJ_Fundo")),
                    "situacao_fundo": row.get("Situacao") or "",
                }

        with zf.open("registro_classe.csv") as fh:
            text = io.TextIOWrapper(fh, encoding="latin-1", newline="")
            for row in csv.DictReader(text, delimiter=";"):
                sit = (row.get("Situacao") or "").upper()
                if "FUNCIONAMENTO" not in sit and sit not in ("",):
                    # keep Em Funcionamento Normal; drop cancelados
                    if sit.startswith("CANCEL") or "LIQUID" in sit:
                        continue
                fid = row.get("ID_Registro_Fundo") or ""
                if fid not in fundos:
                    continue
                cnpj = _digits(row.get("CNPJ_Classe"))
                if not cnpj:
                    continue
                meta = dict(fundos[fid])
                meta["class_name"] = row.get("Denominacao_Social") or meta.get("fund_name") or ""
                meta["cnpj_fmt"] = row.get("CNPJ_Classe") or cnpj
                meta["tipo_classe"] = row.get("Tipo_Classe") or ""
                classes[cnpj] = meta

    return classes


def fetch_latest(
    watchlist_admins: list[str] | None = None,
    yyyymm: str | None = None,
    top_n: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch Informe Diário for the latest competency date in a month file.

    Filters to watchlisted admins/gestores via registro_fundo_classe.
    Empty watchlist → [] (unfiltered month is hundreds of thousands of rows).
    """
    class_meta = resolve_watchlist_classes(watchlist_admins)
    if not class_meta:
        return []

    months_to_try = (
        [yyyymm] if yyyymm else [latest_yyyymm(0), latest_yyyymm(1), latest_yyyymm(2)]
    )
    last_err: Exception | None = None
    for ym in months_to_try:
        if not ym:
            continue
        try:
            rows = _load_month_for_cnpjs(ym, set(class_meta.keys()))
            if not rows:
                continue
            # Absolute max date can be sparse (late filers). Prefer the most
            # recent date with healthy coverage (≥50% of peak daily volume).
            as_of = _select_competency_date(rows)
            day_rows = [r for r in rows if r.get("date") == as_of]
            for r in day_rows:
                meta = class_meta.get(r["cnpj"]) or {}
                r["fund_name"] = meta.get("class_name") or meta.get("fund_name")
                r["admin"] = meta.get("admin")
                r["manager"] = meta.get("manager")
                r["tipo_classe"] = meta.get("tipo_classe")
            day_rows.sort(key=lambda x: -(x.get("pl") or 0))
            if top_n is not None:
                day_rows = day_rows[:top_n]
            return day_rows
        except Exception as exc:
            last_err = exc
            continue
    if last_err:
        raise last_err
    return []


def _load_month_for_cnpjs(yyyymm: str, cnpjs: set[str]) -> list[dict[str, Any]]:
    url = INF_DIARIO_ZIP.format(yyyymm=yyyymm)
    resp = requests.get(url, timeout=300)
    resp.raise_for_status()
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        for name in zf.namelist():
            with zf.open(name) as fh:
                for rec in _iter_csv_rows(fh, cnpjs):
                    key = (rec["cnpj"], rec["date"])
                    a = agg.get(key)
                    if not a:
                        agg[key] = rec
                    else:
                        a["pl"] = (a.get("pl") or 0) + (rec.get("pl") or 0)
                        a["portfolio"] = (a.get("portfolio") or 0) + (
                            rec.get("portfolio") or 0
                        )
                        a["captacao"] = (a.get("captacao") or 0) + (
                            rec.get("captacao") or 0
                        )
                        a["resgate"] = (a.get("resgate") or 0) + (rec.get("resgate") or 0)
                        a["net_flow"] = (a.get("captacao") or 0) - (a.get("resgate") or 0)
                        if rec.get("cotistas"):
                            a["cotistas"] = (a.get("cotistas") or 0) + rec["cotistas"]
    out = list(agg.values())
    for r in out:
        r["id"] = f"cvm-inf:{r['cnpj']}:{r['date']}"
        r["move_key"] = r["cnpj"]
        r["source"] = "CVM-InfDiario"
        r["kind"] = "competitor"
        r["url"] = DATASET_URL
        r["yyyymm"] = yyyymm
    return out


def _iter_csv_rows(fh: BinaryIO, cnpjs: set[str]) -> Iterable[dict[str, Any]]:
    text = io.TextIOWrapper(fh, encoding="latin-1", newline="")
    reader = csv.DictReader(text, delimiter=";")
    for row in reader:
        cnpj = _digits(row.get("CNPJ_FUNDO_CLASSE") or row.get("CNPJ_FUNDO"))
        if not cnpj or cnpj not in cnpjs:
            continue
        date = (row.get("DT_COMPTC") or "")[:10]
        if not date:
            continue
        capt = _parse_float(row.get("CAPTC_DIA")) or 0.0
        resg = _parse_float(row.get("RESG_DIA")) or 0.0
        pl = _parse_float(row.get("VL_PATRIM_LIQ"))
        cot = row.get("NR_COTST")
        yield {
            "cnpj": cnpj,
            "date": date,
            "tp_fundo": row.get("TP_FUNDO_CLASSE"),
            "subclasse": row.get("ID_SUBCLASSE") or None,
            "pl": pl,
            "portfolio": _parse_float(row.get("VL_TOTAL")),
            "quota": _parse_float(row.get("VL_QUOTA")),
            "captacao": capt,
            "resgate": resg,
            "net_flow": capt - resg,
            "cotistas": int(float(cot)) if cot not in (None, "") else None,
        }


def for_moves(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("move_key") and r.get("pl") is not None]


def _select_competency_date(rows: list[dict[str, Any]]) -> str:
    """Most recent DT_COMPTC with substantial reporting volume."""
    from collections import Counter

    counts = Counter(r.get("date") for r in rows if r.get("date"))
    if not counts:
        raise ValueError("no competency dates in informe diário rows")
    peak = max(counts.values())
    threshold = max(1, int(0.5 * peak))
    eligible = [d for d, c in counts.items() if c >= threshold]
    return max(eligible)


def inspect(yyyymm: str | None = None) -> None:
    ym = yyyymm or latest_yyyymm(0)
    url = INF_DIARIO_ZIP.format(yyyymm=ym)
    print(f"GET {url}")
    resp = requests.get(url, timeout=300)
    print(f"HTTP {resp.status_code} bytes={len(resp.content)}")
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as fh:
            text = io.TextIOWrapper(fh, encoding="latin-1", newline="")
            reader = csv.DictReader(text, delimiter=";")
            print("cols:", reader.fieldnames)
            print("sample:", next(reader, None))
    meta = resolve_watchlist_classes(["BTG PACTUAL", "ITAU", "XP"])
    print(f"watchlist classes: {len(meta)}")
    rows = fetch_latest(
        watchlist_admins=["BTG PACTUAL", "ITAU", "XP"], yyyymm=ym, top_n=8
    )
    print(f"latest day (top {len(rows)} by PL):")
    for r in rows:
        print(
            f"  {r.get('date')}  PL={r.get('pl'):,.0f}  "
            f"net={r.get('net_flow'):+,.0f}  {(r.get('admin') or '')[:28]:28}  "
            f"{(r.get('fund_name') or '')[:40]}"
        )


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "inspect":
        inspect(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        watch = sys.argv[1:] or ["ITAU", "BTG PACTUAL", "XP"]
        rows = fetch_latest(watchlist_admins=watch, top_n=20)
        print(f"{len(rows)} funds on latest competency day for {watch}")
        for r in rows:
            print(
                f"  {r.get('date')}  PL R$ {(r.get('pl') or 0):>14,.0f}  "
                f"flow {(r.get('net_flow') or 0):>+12,.0f}  "
                f"{(r.get('fund_name') or r.get('cnpj') or '')[:45]}"
            )
