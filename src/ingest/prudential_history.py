"""Prudential history per entity, with ``as_of`` (#333, ADR 031 Board M1).

The live ingesters (``bcb_fundamentals``, ``bcb_soundness``, ``bcb_balancete``, ``bcb_km1``)
keep only the latest base date, and they resolve institutions by today's NAME. Historic replay
needs every period, attributed to whoever owned the institution **then**. This module keys
every number by its BCB code and attributes it through the dated entity map (#330).

Sources and depth (probed 2026-11-16):

* **IF.data Resumo** (Relatório 1): assets, credit, funding, net income, equity.
  ``basis="prudencial"`` (TipoInstituicao 1) from 2014-03, the live basis.
  ``basis="financeiro"`` (TipoInstituicao 2) before that, from 2000-03, so the 2004–2013
  failures (Banco Santos, Cruzeiro do Sul, BVA, Rural) have numbers. The two bases differ
  (financial vs prudential conglomerate), and every point says which it is.
* **IF.data Capital** (Relatório 5, TipoInstituicao 1): Basileia, Tier 1, CET1, leverage,
  from 2015-03.
* **COSIF** monthly balancete (doc 4010) per individual institution (CNPJ root), from
  1999-01: credit, deposits, equity, liquid assets, loan-loss provisions (R$ mil).
* **Pilar 3 KM1** via BCB's DASFN catalog: CET1, Tier 1, Basileia, leverage, **LCR,
  NSFR**. 77 institutions, from 2019. Each file also carries four prior quarters.

IF.data codes are a CNPJ root (an independent institution) or a conglomerate code
(``C0080312``). A conglomerate code resolves to its leader's CNPJ root through that quarter's
register snapshot (#330 cache), then to the owner at that date with ``entity_at``.

Store (digests bucket, ``board_history/prudential/``):
* one immutable extract per source and period (``ifdata/{basis}/{q}``, ``capital/{q}``,
  ``cosif/{ym}``, ``km1/points``);
* ``series.json.gz``, per entity and source, the points in date order.

Ratio guards (``guard_fundamentals``) run on read, in ``series_for``.
"""
from __future__ import annotations

import gzip
import json
from typing import Any, Iterable, Mapping

from src.ingest import bcb_entity_history as eh
from src.ingest import bcb_fundamentals as fund
from src.ingest import bcb_soundness as sound

PREFIX = "board_history/prudential/"
SERIES_KEY = f"{PREFIX}series.json.gz"
SCHEMA = "onca/prudential-history/v1"

FIRST_PRUDENCIAL = 201403      # TipoInstituicao 1 (prudential conglomerates)
FIRST_FINANCEIRO = 200003      # TipoInstituicao 2, used before FIRST_PRUDENCIAL
FIRST_CAPITAL = 201503         # Relatório 5 under TipoInstituicao 1
FIRST_COSIF = 199901
BASIS_TIPO = {"prudencial": 1, "financeiro": 2}
CONGLOMERATE_FIELD = {"prudencial": "CodConglomeradoPrudencial", "financeiro": "CodConglomeradoFinanceiro"}
SOURCES = ("ifdata", "capital", "cosif", "km1")


def basis_for(q: int) -> str:
    return "prudencial" if q >= FIRST_PRUDENCIAL else "financeiro"


def resumo_url(q: int, basis: str) -> str:
    return (f"{fund.BASE}/IfDataValores(AnoMes=@A,TipoInstituicao=@T,Relatorio=@R)"
            f"?@A={q}&@T={BASIS_TIPO[basis]}&@R='{fund.RELATORIO_RESUMO}'&$format=json")


# --- attribution ----------------------------------------------------------------

def leader_roots(snapshot: Iterable[Mapping[str, Any]], basis: str) -> dict[str, str]:
    """{conglomerate code: leader CNPJ root} for one quarter's register snapshot."""
    field, out = CONGLOMERATE_FIELD[basis], {}
    for r in snapshot or []:
        code, leader = r.get(field), eh._root(r.get("CnpjInstituicaoLider"))
        if code and leader:
            out.setdefault(str(code), leader)
    return out


def root_of(code: str, leaders: Mapping[str, str]) -> str | None:
    """An IF.data CodInst → the CNPJ root it stands for (itself, or its leader)."""
    c = str(code or "").strip()
    if c.isdigit():
        return eh._root(c)
    return leaders.get(c)


def attribute(emap: Mapping[str, Any], root: str | None, as_of: Any) -> dict[str, Any]:
    if not root:
        return {"root": None, "entity_id": None, "direct_entity_id": None, "lead": False}
    iv = eh.interval_at(emap, root, as_of) or {}
    group = iv.get("group_entity_id")
    leader = eh._root(iv.get("leader"))
    return {"root": root, "entity_id": group, "direct_entity_id": iv.get("entity_id"),
            # the conglomerate leader's own root: the row a per-entity series prefers
            "lead": bool(group) and (leader == root or not leader)}


# --- IF.data extracts -----------------------------------------------------------

def ifdata_points(rows: list[dict[str, Any]], q: int, basis: str, leaders: Mapping[str, str],
                  emap: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One point per IF.data code with the live ratios (same ``_ratios`` as the feed)."""
    lines = fund.extract_lines(rows)
    cart_total = sum(v.get("carteira") or 0 for c, v in lines.items() if not str(c).isdigit()) or \
        sum(v.get("carteira") or 0 for v in lines.values())
    lucro_total = sum(v.get("lucro") or 0 for c, v in lines.items() if not str(c).isdigit()) or \
        sum(v.get("lucro") or 0 for v in lines.values())
    out = []
    for code, v in sorted(lines.items()):
        att = attribute(emap, root_of(code, leaders), q)
        out.append({"as_of": q, "basis": basis, "code": code, "conglomerate": not code.isdigit(), **att,
                    **fund._ratios(v, q, carteira_total=cart_total, lucro_total=lucro_total)})
    return out


def capital_points(rows: list[dict[str, Any]], q: int, leaders: Mapping[str, str],
                   emap: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    for code, m in sorted(sound.extract_solvency(rows).items()):
        att = attribute(emap, root_of(code, leaders), q)
        out.append({"as_of": q, "basis": "prudencial", "code": code,
                    "conglomerate": not code.isdigit(), **att, **m,
                    "band": sound.soundness_band(m.get("indice_basileia"))})
    return out


# --- COSIF and KM1 -----------------------------------------------------------------

def cosif_points(month: Mapping[str, Mapping[str, Any]], ym: int,
                 emap: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``bcb_balancete.fetch_month`` output → points (R$ mil), one per CNPJ root."""
    q = eh.quarter_of(ym)
    out = []
    for cnpj, rec in sorted(month.items()):
        root = eh._root(cnpj)
        out.append({"as_of": ym, "name": rec.get("name"), "chart": rec.get("chart"),
                    **attribute(emap, root, q),
                    **{k: v for k, v in (rec.get("lines") or {}).items()}})
    return out


COSIF_URLS = ("https://www.bcb.gov.br/content/estabilidadefinanceira/cosif/Bancos/{ym}BANCOS.csv.zip",
              "https://www.bcb.gov.br/content/estabilidadefinanceira/cosif/Bancos/{ym}BANCOS.zip")
# Two charts of accounts. The legacy COSIF plan (to 2024-12; 8-digit codes, zero-padded to 10
# in the 2000-era files) and the CMN Res. 4.966 plan (from 2025-01; the live ingester's map).
# Matched by account NAME in 2000-12, 2014-12 and 2025-01 BB rows (probed 2026-11-16).
COSIF_LEGACY = {"credito": "16000001", "depositos": "41000007", "patrimonio_liquido": "60000002",
                "disponibilidades": "11000006"}
COSIF_LEGACY_PDD = frozenset({"16900008"})       # (-) provisões para operações de crédito


def _cosif_num(v: str | None) -> float | None:
    t = (v or "").strip()
    if not t:
        return None
    try:
        return float(t.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def parse_cosif(content: bytes) -> dict[str, dict[str, Any]]:
    """{cnpj8: {name, chart, lines}} from one monthly BANCOS file, any of the three layouts.
    Document 4010 (balancete) only; values in R$ mil. Provisions are a positive magnitude."""
    import csv
    import io
    import zipfile
    from src.ingest import bcb_balancete as bal
    zf = zipfile.ZipFile(io.BytesIO(content))
    name = next(n for n in zf.namelist() if n.upper().endswith(".CSV"))
    fh = io.TextIOWrapper(zf.open(name), encoding="latin-1", newline="")
    for _ in range(3):
        fh.readline()
    header = [h.strip().lstrip("#").replace(" ", "_") for h in fh.readline().rstrip("\r\n").split(";")]
    raw: dict[str, dict[str, Any]] = {}
    for row in csv.reader(fh, delimiter=";"):
        r = dict(zip(header, row))
        if (r.get("DOCUMENTO") or "").strip() != "4010":
            continue
        conta = (r.get("CONTA") or "").strip()
        if len(conta) == 10 and conta.startswith("00"):
            conta, chart = conta[2:], "legacy"
        else:
            chart = "legacy" if len(conta) == 8 else "res4966"
        lines, pdd = (COSIF_LEGACY, COSIF_LEGACY_PDD) if chart == "legacy" else (bal.LINE_CODES, bal.PDD_CODES)
        line = next((k for k, c in lines.items() if c == conta), None)
        if not line and conta not in pdd:
            continue
        val = _cosif_num(r.get("SALDO"))
        cnpj = (r.get("CNPJ") or "").strip()
        if val is None or not cnpj:
            continue
        rec = raw.setdefault(cnpj, {"name": (r.get("NOME_INSTITUICAO") or "").strip(), "chart": chart,
                                    "lines": {}})
        key = line or "pdd"
        v = val / 1000.0
        rec["lines"][key] = round(rec["lines"].get(key, 0.0) + (-v if key == "pdd" else v), 2)
    return raw


def fetch_cosif(ym: int, *, timeout: int = 180) -> bytes | None:
    import requests
    for tmpl in COSIF_URLS:
        r = requests.get(tmpl.format(ym=ym), timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and r.content[:2] == b"PK":
            return r.content
    return None


_KM1_SHIFT = {"t": 0, "t_1": 1, "t_2": 2, "t_3": 3, "t_4": 4}


def quarter_back(q: int, n: int) -> int:
    y, m = divmod(q, 100)
    idx = y * 4 + (m - 1) // 3 - n
    return (idx // 4) * 100 + (idx % 4) * 3 + 3


def km1_quarter(doc: Any) -> int | None:
    """``km1_trimestreReferencia`` ("2025-3", "3T25", "202509") → YYYYMM quarter end."""
    if not isinstance(doc, dict):
        return None
    t = str(doc.get("km1_trimestreReferencia") or doc.get("km1TrimestreReferencia") or "").strip()
    import re
    m = re.fullmatch(r"(\d{4})\D?([1-4])", t) or re.fullmatch(r"([1-4])T(\d{2,4})", t, re.I)
    if m and t[0].isdigit() and len(m.group(1)) == 4:
        return int(m.group(1)) * 100 + int(m.group(2)) * 3
    if m:
        y = int(m.group(2))
        return (2000 + y if y < 100 else y) * 100 + int(m.group(1)) * 3
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})?", t)
    return eh.quarter_of(int(m.group(1)) * 100 + int(m.group(2))) if m else None


# Fraction vs percent: v2 rows hold fractions (0.1479) and are scaled in ``km1_values``; v1
# scalars are usually percent (14.79). No capital ratio is under 1% and no LCR/NSFR under 10%,
# so a smaller scalar is a fraction.
_FRACTION_BELOW = {"cet1_pct": 1, "tier1_pct": 1, "basileia_pct": 1, "leverage_pct": 1,
                   "lcr_pct": 10, "nsfr_pct": 10}
_KM1_BOUNDS = {"cet1_pct": 60, "tier1_pct": 60, "basileia_pct": 60, "leverage_pct": 40,
               "lcr_pct": 3000, "nsfr_pct": 800}


def km1_values(doc: Mapping[str, Any], code: str) -> dict[str, float | None]:
    """{column: raw value} for one KM1 row in any of the three published layouts:
    v2 rows ``{"t": .., "t_1": ..}`` (flat or grouped), v1 scalars ``km1_7: 14.79`` (current
    quarter only) and the camelCase v1 ``km17: 18.59``."""
    from src.ingest import bcb_km1 as k
    row = k._find_row(doc, code)  # type: ignore[arg-type]
    if row:   # v2 rows are always fractions (as the live ingester reads them)
        return {c: (None if k._num(row.get(c)) is None else k._num(row.get(c)) * 100) for c in _KM1_SHIFT}
    for key in (code, code.replace("_", "")):
        v = doc.get(key)
        if v is not None and not isinstance(v, (dict, list)):
            return {"t": k._num(v)}
    return {}


def _pct(metric: str, v: float | None) -> float | None:
    if v is None:
        return None
    pct = round(v * 100, 2) if abs(v) < _FRACTION_BELOW[metric] else round(v, 2)
    # 0 means "not applicable" (outside the LCR rule), never a 0% ratio
    return pct if 0 < pct <= _KM1_BOUNDS[metric] else None


def km1_points(doc: Any, cnpj: str, name: str, url: str, emap: Mapping[str, Any],
               fallback_q: int | None = None) -> list[dict[str, Any]]:
    """Every quarter a KM1 file carries (t and, in v2, the four before it), within the bounds
    the live ingester applies. Later files win over earlier ones when merged (``merge_km1``)."""
    from src.ingest import bcb_km1 as k
    if isinstance(doc, list):
        doc = doc[0] if doc and isinstance(doc[0], dict) else {}
    if not isinstance(doc, dict):
        return []
    q0 = km1_quarter(doc) or fallback_q
    if not q0:
        return []
    root = eh._root(cnpj)
    cols = {metric: km1_values(doc, code) for code, metric in k._ROW_METRIC.items()}
    out = []
    for col, n in _KM1_SHIFT.items():
        q = quarter_back(q0, n)
        vals = {metric: _pct(metric, by.get(col)) for metric, by in cols.items()}
        if any(v is not None for v in vals.values()):
            out.append({"as_of": q, "reported_in": q0, "name": name, "url": url,
                        **attribute(emap, root, q), **vals})
    return out


def merge_km1(points: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One point per (root, quarter): the value as reported in the LATEST file (restatements
    replace earlier figures)."""
    best: dict[tuple, dict[str, Any]] = {}
    for p in points:
        key = (p.get("root"), p["as_of"])
        if key not in best or p["reported_in"] >= best[key]["reported_in"]:
            best[key] = dict(p)
    return sorted(best.values(), key=lambda p: (p.get("root") or "", p["as_of"]))


# --- series --------------------------------------------------------------------------

def build_series(points_by_source: Mapping[str, Iterable[Mapping[str, Any]]], built_at: str) -> dict[str, Any]:
    """{entity: {source: [points by as_of]}} for attributed points only. Each point keeps its
    root/code, so a consumer can pick the lead row (``lead``) or look at every member."""
    ents: dict[str, dict[str, list]] = {}
    counts = {}
    for src, pts in points_by_source.items():
        n = 0
        for p in pts:
            e = p.get("entity_id")
            if not e:
                continue
            ents.setdefault(e, {}).setdefault(src, []).append(dict(p))
            n += 1
        counts[src] = n
    for by in ents.values():
        for src in by:
            by[src].sort(key=lambda p: (p["as_of"], not p.get("lead"), str(p.get("code") or p.get("root"))))
    return {"schema": SCHEMA, "built_at": built_at, "points": counts, "entities": ents}


def series_for(series: Mapping[str, Any], entity_id: str, source: str, *, start: int | None = None,
               end: int | None = None, lead_only: bool = True) -> list[dict[str, Any]]:
    """One entity's points for a source, within ``[start, end]`` (YYYYMM), one per period.

    ``lead_only`` keeps, per period, the conglomerate row (IF.data) or the leader's own root
    (COSIF/KM1); if neither is marked, the largest by assets/credit. Ratio guards run here, on
    read, exactly as for the live feed."""
    from src.synth.ratio_guards import guard_fundamentals
    pts = ((series.get("entities") or {}).get(entity_id) or {}).get(source) or []
    pts = [p for p in pts if (start is None or p["as_of"] >= start) and (end is None or p["as_of"] <= end)]
    if lead_only:
        by: dict[int, list] = {}
        for p in pts:
            by.setdefault(p["as_of"], []).append(p)

        def rank(p: Mapping[str, Any]) -> tuple:
            size = p.get("ativo_bi") or p.get("credito") or p.get("carteira_bi") or 0
            return (bool(p.get("conglomerate")), bool(p.get("lead")), size)
        pts = [max(v, key=rank) for _, v in sorted(by.items())]
    out = [dict(p) for p in pts]
    if source == "ifdata":
        out = [guard_fundamentals(p) for p in out]
    return out


# --- store ---------------------------------------------------------------------------

def put_json(s3: Any, bucket: str, key: str, doc: Any) -> None:
    s3.put_object(Bucket=bucket, Key=key, ContentType="application/json", ContentEncoding="gzip",
                  Body=gzip.compress(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()))


def get_json(s3: Any, bucket: str, key: str) -> Any | None:
    try:
        raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as exc:
        if "NoSuchKey" in type(exc).__name__ or "NoSuchKey" in str(exc):
            return None
        raise
    return json.loads(gzip.decompress(raw))


def extract_key(source: str, period: int | str, basis: str | None = None) -> str:
    if source == "ifdata":
        return f"{PREFIX}ifdata/{basis}/{period}.json.gz"
    return f"{PREFIX}{source}/{period}.json.gz"
