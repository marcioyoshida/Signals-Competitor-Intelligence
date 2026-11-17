"""SUSEP financial history per insurer (#390, ADR 031 Board M1): the insurer side of #333.

Source: SUSEP's statistics system (SES), ``BaseCompleta.zip``, read member by member through
HTTP range requests (``susep_entity_history.fetch_member``). Three members, measured
2026-11-16:

* ``Ses_seguros.csv`` (30 MB compressed, 1.8 M rows): premiums, claims and commercial expense
  per company, line of business (``coramo``) and month, 1995 onward. Values are monthly flows
  in R$, not year-to-date. 21 columns; the column use changes in 2013-12 (below).
* ``Ses_pl_margem.csv``: equity (``pl``), adjusted equity (``plajustado``, from 2016 also
  ``NovoPla``) and the minimum required capital (``CMR``, from 2016). The older solvency
  margin (``margem``) is sparse and not used.
* ``Ses_prov.csv``: total technical provisions, monthly, 1999 onward.

**The 2013-12 break.** Until 2013-11 the FIP form put VGBL pension contributions into
written premium (market ``premio_direto`` R$13bn in 2013-11, R$8bn in 2013-12) and claims into
``sinistro_retido``. From 2013-12 claims are in ``sinistro_ocorrido`` and the old claim column
is zero. No row fills both (checked 2012-09 to 2014-06), so claims are the sum of the two
columns on every row. Earned premium (``premio_ganho``) is continuous across the break, so
the loss ratio uses earned premium on both sides. Every point records its ``basis``
(``fip_pre2014``, ``fip_2014``, or ``mixed`` for 2013-Q4); written premium is not comparable
across the break. A first cut that switched columns by quarter halved 2013-Q4 loss ratios
(Porto 34%, Bradesco 30%): two of its three months were in the old column.

Attribution is by date through the #389 insurer map (``code_entity_at``), never today's owner.
Points are per company and quarter (flows summed over the quarter's months, stocks at the
quarter end). ``group_series`` sums companies into their group at each date and recomputes
the ratios from the sums. Ratio guards run on read (``series_for``), as in #333.

Store: ``board_history/susep/series.json.gz``. Each run rebuilds it from the current SES
members (SES republishes the whole history every month). It makes no model calls.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import json
from typing import Any, Iterable, Mapping

from src.ingest import susep_entity_history as sh

SERIES_KEY = "board_history/susep/series.json.gz"
SCHEMA = "onca/susep-history/v1"
MEMBERS = {"seguros": "Ses_seguros.csv", "capital": "Ses_pl_margem.csv", "provisoes": "Ses_prov.csv"}
BREAK_MONTH = 201312
MI = 1e6

# Ses_seguros columns kept. Claims move from sinistro_retido to sinistro_ocorrido in 2013-12.
FLOW_COLS = ("premio_direto", "premio_ganho", "desp_com", "sinistro_retido", "sinistro_ocorrido")

# Read-side plausibility, as #333's guard_fundamentals: a tiny or run-off book gives ratios
# that are arithmetic, not signal.
MIN_EARNED_MI = 1.0            # R$1 mi earned premium in the quarter
RATIO_BOUNDS = {"sinistralidade_pct": (0.0, 300.0), "comissionamento_pct": (-50.0, 150.0),
                "suficiencia_pct": (0.0, 5000.0)}


def basis_for(ym: int) -> str:
    """The FIP regime of a month."""
    return "fip_2014" if ym >= BREAK_MONTH else "fip_pre2014"


def quarter_basis(months: Iterable[int]) -> str:
    b = {basis_for(m) for m in months}
    return b.pop() if len(b) == 1 else "mixed"


def quarter_of_month(ym: int) -> int:
    y, m = divmod(int(ym), 100)
    return y * 100 + ((m - 1) // 3 + 1) * 3


def _num(v: Any) -> float:
    s = str(v or "").strip().replace(",", ".")
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def _rows(content: bytes) -> Iterable[dict[str, str]]:
    for r in csv.DictReader(io.StringIO(content.decode("latin-1")), delimiter=";"):
        yield {(k or "").strip(): (v or "").strip() for k, v in r.items()}


def _month(r: Mapping[str, str]) -> int | None:
    m = r.get("damesano") or ""
    return int(m) if len(m) == 6 and m.isdigit() and 1 <= int(m[4:]) <= 12 else None


# --- parse (per company and quarter) ---------------------------------------------

def parse_seguros(content: bytes, last_month: int | None = None) -> dict[tuple[str, int], dict[str, Any]]:
    """{(code, quarter): summed flows + the months seen}. Lines of business are summed."""
    out: dict[tuple[str, int], dict[str, Any]] = {}
    for r in _rows(content):
        ym = _month(r)
        if ym is None or (last_month and ym > last_month) or not r.get("coenti"):
            continue
        a = out.setdefault((r["coenti"], quarter_of_month(ym)),
                           {**{c: 0.0 for c in FLOW_COLS}, "months": set()})
        for c in FLOW_COLS:
            a[c] += _num(r.get(c))
        a["months"].add(ym)
    return out


def parse_stock(content: bytes, cols: Iterable[str], last_month: int | None = None
                ) -> dict[tuple[str, int], dict[str, float]]:
    """{(code, quarter): the values of the last month of the quarter seen}."""
    cols = tuple(cols)
    best: dict[tuple[str, int], tuple[int, dict[str, float]]] = {}
    for r in _rows(content):
        ym = _month(r)
        if ym is None or (last_month and ym > last_month) or not r.get("coenti"):
            continue
        k = (r["coenti"], quarter_of_month(ym))
        if k not in best or ym > best[k][0]:
            best[k] = (ym, {c: _num(r.get(c)) for c in cols})
    return {k: v for k, (_, v) in best.items()}


def months_of(content: bytes) -> dict[int, int]:
    """{month: rows} for the completeness check."""
    n: dict[int, int] = {}
    for r in _rows(content):
        ym = _month(r)
        if ym:
            n[ym] = n.get(ym, 0) + 1
    return n


def complete_through(counts: Mapping[int, int], ratio: float = sh.COMPLETE_RATIO) -> int | None:
    """The latest month SES has fully published (a late-filer month is partial), as #389."""
    ms = sorted(counts)
    while len(ms) > 1 and counts[ms[-1]] < ratio * counts[ms[-2]]:
        ms.pop()
    return ms[-1] if ms else None


# --- points -------------------------------------------------------------------------

def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or not den:
        return None
    return round(100.0 * num / den, 2)


def company_points(seguros: Mapping[tuple[str, int], Mapping[str, Any]],
                   capital: Mapping[tuple[str, int], Mapping[str, float]],
                   provisoes: Mapping[tuple[str, int], Mapping[str, float]],
                   imap: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One point per company and quarter, attributed through the #389 map at that quarter."""
    out = []
    for code, q in sorted(set(seguros) | set(capital) | set(provisoes)):
        s, c, p = seguros.get((code, q)), capital.get((code, q)), provisoes.get((code, q))
        iv = sh.code_interval_at(imap, code, q)
        pt: dict[str, Any] = {
            "as_of": q, "susep_code": code,
            "basis": quarter_basis(s["months"]) if s else basis_for(q),
            "name": iv.get("name") if iv else None,
            "cnpj_root": iv.get("cnpj_root") if iv else None,
            "entity_id": iv.get("group_entity_id") if iv else None,
            "direct_entity_id": iv.get("entity_id") if iv else None,
        }
        if s:
            claims = s["sinistro_ocorrido"] + s["sinistro_retido"]     # never both on one row
            pt.update({
                "months": len(s["months"]),
                "premio_emitido_mi": round(s["premio_direto"] / MI, 6),
                "premio_ganho_mi": round(s["premio_ganho"] / MI, 6),
                "sinistros_mi": round(claims / MI, 6),
                "desp_com_mi": round(s["desp_com"] / MI, 6),
            })
        if c:
            pla = c.get("NovoPla") or c.get("plajustado") or None
            cmr = c.get("CMR") or None
            pt.update({"pl_mi": round(c.get("pl", 0.0) / MI, 6) if c.get("pl") else None,
                       "pla_mi": round(pla / MI, 6) if pla else None,
                       "cmr_mi": round(cmr / MI, 6) if cmr else None})
        if p and p.get("valor"):
            pt["provisoes_mi"] = round(p["valor"] / MI, 6)
        out.append(with_ratios(pt))
    return out


def with_ratios(pt: dict[str, Any]) -> dict[str, Any]:
    pt["sinistralidade_pct"] = _ratio(pt.get("sinistros_mi"), pt.get("premio_ganho_mi"))
    pt["comissionamento_pct"] = _ratio(pt.get("desp_com_mi"), pt.get("premio_ganho_mi"))
    pt["suficiencia_pct"] = _ratio(pt.get("pla_mi"), pt.get("cmr_mi"))
    return pt


SUMMED = ("premio_emitido_mi", "premio_ganho_mi", "sinistros_mi", "desp_com_mi", "pl_mi",
          "pla_mi", "cmr_mi", "provisoes_mi")


def group_series(points: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """{group entity: per-quarter sums of its companies at that date, ratios recomputed}."""
    acc: dict[tuple[str, int], dict[str, Any]] = {}
    for p in points:
        if not p.get("entity_id"):
            continue
        a = acc.setdefault((p["entity_id"], p["as_of"]),
                           {"as_of": p["as_of"], "basis": p["basis"], "companies": [],
                            **{k: None for k in SUMMED}})
        a["companies"].append(p["susep_code"])
        for k in SUMMED:
            if p.get(k) is not None:
                a[k] = round((a[k] or 0.0) + p[k], 6)
    out: dict[str, list[dict[str, Any]]] = {}
    for (eid, _), a in sorted(acc.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        a["companies"] = sorted(a["companies"])
        out.setdefault(eid, []).append(with_ratios(a))
    return out


def build_series(points: list[dict[str, Any]], built_at: str, *,
                 complete: int | None = None, imap_quarters: Iterable[int] = ()) -> dict[str, Any]:
    by_code: dict[str, list[dict[str, Any]]] = {}
    for p in points:
        by_code.setdefault(p["susep_code"], []).append(p)
    groups = group_series(points)
    return {
        "schema": SCHEMA, "built_at": built_at, "source": "SUSEP SES BaseCompleta",
        "source_url": sh.PUBLIC_URL, "complete_through": complete,
        "insurer_map_quarters": list(imap_quarters),
        "points": len(points), "attributed": sum(1 for p in points if p.get("entity_id")),
        "companies": by_code, "groups": groups,
    }


# --- read side ------------------------------------------------------------------------

def guard(p: Mapping[str, Any]) -> dict[str, Any]:
    """Blank a ratio that is arithmetic rather than signal: a tiny earned premium, an
    incomplete quarter, or a value outside its plausible range."""
    out = dict(p)
    thin = (out.get("premio_ganho_mi") or 0.0) < MIN_EARNED_MI or (out.get("months") or 3) < 3
    for k, (lo, hi) in RATIO_BOUNDS.items():
        v = out.get(k)
        if v is None:
            continue
        if (k != "suficiencia_pct" and thin) or not lo <= v <= hi:
            out[k] = None
    return out


def series_for(s: Mapping[str, Any], entity_id: str, *, start: int | None = None,
               level: str = "group") -> list[dict[str, Any]]:
    """The guarded quarterly series of a group (default) or of the companies attributed to
    ``entity_id`` directly (``level="company"``), oldest first."""
    if level == "group":
        pts = (s.get("groups") or {}).get(entity_id) or []
    else:
        pts = sorted((p for ps in (s.get("companies") or {}).values() for p in ps
                      if p.get("direct_entity_id") == entity_id),
                     key=lambda p: (p["as_of"], p["susep_code"]))
    return [guard(p) for p in pts if start is None or p["as_of"] >= start]


# --- store ----------------------------------------------------------------------------

def put_series(s3: Any, bucket: str, s: Mapping[str, Any]) -> None:
    s3.put_object(Bucket=bucket, Key=SERIES_KEY, ContentType="application/json", ContentEncoding="gzip",
                  Body=gzip.compress(json.dumps(s, ensure_ascii=False, separators=(",", ":")).encode()))


def load_series(s3: Any, bucket: str) -> dict[str, Any] | None:
    try:
        body = s3.get_object(Bucket=bucket, Key=SERIES_KEY)["Body"].read()
    except Exception:  # noqa: BLE001
        return None
    try:
        body = gzip.decompress(body)
    except OSError:
        pass
    return json.loads(body)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
