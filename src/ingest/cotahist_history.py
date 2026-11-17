"""B3 daily share prices for listed banks, insurers and their holdings (#336, ADR 031 Board M1).

Source: B3's free yearly historic quote files (COTAHIST), probed 2026-11-16:

    https://bvmf.bmfbovespa.com.br/InstDados/SerHist/COTAHIST_A{YYYY}.ZIP

One ZIP per year (6.7 MB in 2000, 95 MB for 2026 so far), one fixed-width latin-1 line of 245
characters per instrument and trading day. No robots.txt on that host; B3's allows all.

Three traps:

* **No CNPJ.** A line carries the ticker, a 12-character short name and the ISIN, whose
  characters 3-6 are B3's issuer code (``BRBBDCACNPR8`` → ``BBDC``). Issuer codes map to a CNPJ
  through CVM's FCA securities table (``fca_cia_aberta_valor_mobiliario``, 2010 on), plus the
  curated ``config/b3_issuers.json`` for issuers that delisted before 2010 or whose FCA rows
  have no trading code (Unibanco, Banespa, Nossa Caixa, BTG's units). Each supplement root was
  read from the IF.data register name.
* **Quote factor.** ``FATCOT`` is the number of shares a price refers to: 1000 for many
  quotes in the early 2000s. Prices here are per share (price / FATCOT).
* **Unadjusted.** COTAHIST prices are not adjusted for splits, bonus shares or dividends. A
  day-on-day move beyond ±40% is flagged ``suspect_corporate_action`` and excluded from
  ``reaction``; any other dividend drift is left in. Treat returns across known corporate
  actions with care.

Scope: issuers whose CNPJ root is in the #330 dated map (banks, financeiras, Cielo), or in
the registry (insurer and exchange holdings: BB Seguridade, Porto Seguro, IRB, B3...).
Spot market (``TPMERC`` 010), standard lot (``CODBDI`` 02). Attribution by date through the #330
map: Unibanco's 2007 shares are Unibanco's, not Itaú's. A holding outside the BCB map is
attributed to its registry entity (``attribution: "registry"``).

Store: ``board_history/b3/{year}.json.gz`` (rows of in-scope issuers; a past year is
immutable) and ``series.json.gz``. It makes no model calls.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import json
import re
import statistics
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.ingest import bcb_entity_history as eh

URL = "https://bvmf.bmfbovespa.com.br/InstDados/SerHist/COTAHIST_A{year}.ZIP"
FCA_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_{year}.zip"
FIRST_YEAR, FIRST_FCA_YEAR = 2000, 2010
PREFIX = "board_history/b3/"
SERIES_KEY = f"{PREFIX}series.json.gz"
SCHEMA = "onca/b3-history/v1"
CONFIG = Path(__file__).resolve().parents[2] / "config" / "b3_issuers.json"
SPOT, STANDARD_LOT = "010", "02"
JUMP = 0.40


def year_key(year: int) -> str:
    return f"{PREFIX}{year}.json.gz"


# --- issuers ------------------------------------------------------------------------

def fca_pairs(content: bytes) -> dict[str, set[str]]:
    """{issuer code: {CNPJ root}} from one FCA year ZIP (the securities table)."""
    out: dict[str, set[str]] = {}
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        names = [n for n in z.namelist() if "valor_mobiliario" in n]
        if not names:
            return out
        for r in csv.DictReader(io.StringIO(z.read(names[0]).decode("latin-1")), delimiter=";"):
            t = (r.get("Codigo_Negociacao") or "").strip().upper()
            cnpj = re.sub(r"\D", "", r.get("CNPJ_Companhia") or "")[:8]
            if re.fullmatch(r"[A-Z0-9]{4}\d{1,2}", t) and len(cnpj) == 8:
                out.setdefault(t[:4], set()).add(cnpj)
    return out


def load_supplement(path: Path | str = CONFIG) -> dict[str, str]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return {code: e["cnpj8"] for code, e in (doc.get("supplement") or {}).items()}


def issuers(pairs: Mapping[str, Iterable[str]], supplement: Mapping[str, str],
            in_scope: Mapping[str, Any] | set[str]) -> dict[str, str]:
    """{issuer code: CNPJ root} for in-scope roots. An FCA code with more than one root keeps
    the one in scope; ambiguity left after that drops the code (never a guess)."""
    out: dict[str, str] = {}
    for code, roots in pairs.items():
        hit = sorted(r for r in roots if r in in_scope)
        if len(hit) == 1:
            out[code] = hit[0]
    for code, root in supplement.items():
        out.setdefault(code, root)
    return out


# --- parse ----------------------------------------------------------------------------

def _price(s: str, fatcot: int) -> float | None:
    v = int(s) / 100.0
    return round(v / (fatcot or 1), 6) if v else None


def parse_line(line: str) -> dict[str, Any] | None:
    """One spot, standard-lot quote line, or None."""
    if line[:2] != "01" or line[24:27] != SPOT or line[10:12] != STANDARD_LOT:
        return None
    fat = int(line[210:217] or "1") or 1
    d = line[2:10]
    return {
        "date": f"{d[:4]}-{d[4:6]}-{d[6:8]}", "ticker": line[12:24].strip(),
        "issuer": line[232:236], "isin": line[230:242], "name": line[27:39].strip(),
        "kind": line[39:49].strip(),
        "open": _price(line[56:69], fat), "high": _price(line[69:82], fat), "low": _price(line[82:95], fat),
        "close": _price(line[108:121], fat), "trades": int(line[147:152] or 0),
        "shares": int(line[152:170] or 0), "volume": round(int(line[170:188] or 0) / 100.0, 2),
        "fatcot": fat,
    }


def parse_year(content: bytes, codes: Iterable[str]) -> list[dict[str, Any]]:
    want = set(codes)
    out = []
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        with z.open(z.namelist()[0]) as fh:
            for raw in fh:
                if raw[232:236].decode("latin-1") not in want:
                    continue
                rec = parse_line(raw.decode("latin-1"))
                if rec:
                    out.append(rec)
    return out


# --- series -----------------------------------------------------------------------------

def build_series(rows: Iterable[Mapping[str, Any]], codes: Mapping[str, str], emap: Mapping[str, Any],
                 registry_roots: Mapping[str, str], built_at: str) -> dict[str, Any]:
    """{entity: {ticker: [[date, open, high, low, close, volume, trades, flag], ...]}}, each day
    attributed through the #330 map at that date (or the registry for a holding outside it)."""
    by_ticker: dict[str, list[Mapping[str, Any]]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)
    entities: dict[str, dict[str, list[list[Any]]]] = {}
    tickers: dict[str, dict[str, Any]] = {}
    unattributed: dict[str, int] = {}
    for t, rs in by_ticker.items():
        rs = sorted(rs, key=lambda r: r["date"])
        root = codes.get(rs[0]["issuer"])
        tickers[t] = {"issuer": rs[0]["issuer"], "cnpj8": root, "name": rs[-1]["name"], "kind": rs[-1]["kind"],
                      "first": rs[0]["date"], "last": rs[-1]["date"], "days": len(rs)}
        prev = None
        for r in rs:
            in_map = root in (emap.get("roots") or {})
            eid = eh.entity_at(emap, root, r["date"]) if in_map else registry_roots.get(root or "")
            flag = None
            if prev and prev.get("close") and r.get("close"):
                ret = r["close"] / prev["close"] - 1
                flag = "suspect_corporate_action" if abs(ret) > JUMP else None
            prev = r
            if not eid:
                unattributed[t] = unattributed.get(t, 0) + 1
                continue
            entities.setdefault(eid, {}).setdefault(t, []).append(
                [r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"], r["trades"], flag])
            tickers[t].setdefault("attribution", "dated" if in_map else "registry")
    return {"schema": SCHEMA, "built_at": built_at, "source": URL.format(year="YYYY"),
            "note": "unadjusted closes, per share; see suspect_corporate_action",
            "tickers": tickers, "entities": entities, "unattributed_days": unattributed}


def main_ticker(s: Mapping[str, Any], entity_id: str, around: str, days: int = 250) -> str | None:
    """The entity's most traded ticker (by volume) in the ``days`` sessions before ``around``."""
    best, vol = None, -1.0
    for t, pts in ((s.get("entities") or {}).get(entity_id) or {}).items():
        v = sum(p[5] or 0 for p in [p for p in pts if p[0] <= around][-days:])
        if v > vol:
            best, vol = t, v
    return best if vol > 0 else None


def _ret(pts: list[list[Any]], d0: str, d1: str) -> float | None:
    """Close-to-close return from the last session before d0 to the last on or before d1;
    None when a flagged corporate-action day falls inside."""
    before = [p for p in pts if p[0] < d0 and p[4]]
    inside = [p for p in pts if d0 <= p[0] <= d1 and p[4]]
    if not before or not inside or any(p[7] for p in inside):
        return None
    return inside[-1][4] / before[-1][4] - 1


def reaction(s: Mapping[str, Any], entity_id: str, event_date: str, *, before: int = 1,
             after: int = 5) -> dict[str, Any] | None:
    """Share-price reaction around an event: the entity's main ticker over [event - ``before``
    calendar days, event + ``after``], next to the median of every other in-scope entity's
    main ticker over the same window (a sector benchmark; COTAHIST carries no index)."""
    d = dt.date.fromisoformat(event_date)
    d0, d1 = (d - dt.timedelta(days=before)).isoformat(), (d + dt.timedelta(days=after)).isoformat()
    ents = s.get("entities") or {}
    t = main_ticker(s, entity_id, event_date)
    if not t:
        return None
    own = _ret(ents[entity_id][t], d0, d1)
    if own is None:
        return None
    peers = []
    for e in ents:
        if e == entity_id:
            continue
        pt = main_ticker(s, e, event_date)
        r = _ret(ents[e][pt], d0, d1) if pt else None
        if r is not None:
            peers.append(r)
    bench = statistics.median(peers) if peers else None
    return {"entity_id": entity_id, "ticker": t, "from": d0, "to": d1,
            "return_pct": round(100 * own, 2),
            "sector_median_pct": round(100 * bench, 2) if bench is not None else None,
            "abnormal_pct": round(100 * (own - bench), 2) if bench is not None else None,
            "peers": len(peers)}


# --- store ------------------------------------------------------------------------------

def put_json(s3: Any, bucket: str, key: str, doc: Any) -> None:
    s3.put_object(Bucket=bucket, Key=key, ContentType="application/json", ContentEncoding="gzip",
                  Body=gzip.compress(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()))


def get_json(s3: Any, bucket: str, key: str) -> Any:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception:  # noqa: BLE001
        return None
    try:
        body = gzip.decompress(body)
    except OSError:
        pass
    return json.loads(body)
