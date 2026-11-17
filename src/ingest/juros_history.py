"""BCB interest-rate history per institution and product (#334, ADR 031 Board M1).

The live reader (``bcb_juros``) keeps the latest five-business-day window. Board replay needs
how each bank priced credit over time: the main public trace of how a downgrade, a funding
squeeze or a Selic cycle reached customers' prices.

Source: Olinda ``taxaJuros`` v2 (probed 2026-11-16).

* ``TaxasJurosDiariaPorInicioPeriodo``: average rates per institution, segment (PF/PJ) and
  product over a five-business-day window. A new window starts **every business day**
  (3,719 windows from 2012-01-02), so consecutive windows overlap by four days. About 770
  rows from about 170 institutions per window, 21 products in 2012 (card revolving and card
  instalments arrive later). We take a non-overlapping chain (``chain``): each window starts
  after the previous one ends, about 745 windows, one per business week.
* ``TaxasJurosMensalPorMes``: monthly, real-estate products only (about 40 rows a month).

Each row carries ``Posicao`` (1 = cheapest of the window) and the annual and monthly rate.
Product names are kept exactly as published; BCB renames products (card revolving became
"rotativo total"), and a silent merge would fake continuity.

Reference: the Selic target (SGS 432), so pass-through can be measured against policy.
Attribution: every row goes through the #330 dated map at the window's end date (group and
direct), never today's owner.

Store under ``board_history/juros/``: ``daily/{year}.json.gz`` (a past year is immutable),
``monthly.json.gz``, ``selic.json.gz`` and ``series.json.gz``. It makes no model calls.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import statistics
import time
from typing import Any, Callable, Iterable, Mapping

from src.ingest import bcb_entity_history as eh

BASE = "https://olinda.bcb.gov.br/olinda/servico/taxaJuros/versao/v2/odata"
SGS_SELIC = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.432/dados"
PREFIX = "board_history/juros/"
SERIES_KEY = f"{PREFIX}series.json.gz"
MONTHLY_KEY = f"{PREFIX}monthly.json.gz"
SELIC_KEY = f"{PREFIX}selic.json.gz"
SCHEMA = "onca/juros-history/v1"
FIRST_DAY = "2012-01-02"
SEGMENTS = {"PESSOA FÍSICA": "PF", "PESSOA JURÍDICA": "PJ"}


def daily_key(year: int) -> str:
    return f"{PREFIX}daily/{year}.json.gz"


# --- fetch --------------------------------------------------------------------------

def _get(url: str, *, tries: int = 4, timeout: int = 120) -> Any:
    import requests
    last: Exception | None = None
    for i in range(tries):
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001 - retried, then raised
            last = exc
            time.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


def daily_periods(get: Callable[[str], Any] = _get) -> list[dict[str, str]]:
    url = (f"{BASE}/ConsultaDatas?$filter=tipoModalidade%20eq%20'D'"
           "&$orderby=inicioPeriodo%20asc&$top=100000&$format=json")
    return [{"inicio": r["inicioPeriodo"], "fim": r["fimPeriodo"]} for r in get(url).get("value", [])]


def monthly_periods(get: Callable[[str], Any] = _get) -> list[str]:
    url = f"{BASE}/ConsultaDatas?$filter=tipoModalidade%20eq%20'M'&$top=100000&$format=json"
    return sorted(r["inicioPeriodo"][:7] for r in get(url).get("value", []))


def chain(periods: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    """Non-overlapping windows: each starts after the previous one ends."""
    out: list[dict[str, str]] = []
    for p in sorted(periods, key=lambda p: p["inicio"]):
        if not out or p["inicio"] > out[-1]["fim"]:
            out.append({"inicio": p["inicio"], "fim": p["fim"]})
    return out


def names_of(rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """{cnpj8: institution name} from raw rows."""
    return {str(r.get("cnpj8")): str(r.get("InstituicaoFinanceira") or "").strip()
            for r in rows if r.get("cnpj8")}


def fetch_daily(inicio: str, get: Callable[[str], Any] = _get) -> list[dict[str, Any]]:
    url = (f"{BASE}/TaxasJurosDiariaPorInicioPeriodo?$filter=InicioPeriodo%20eq%20'{inicio}'"
           "&$top=100000&$format=json")
    return get(url).get("value", [])


def fetch_monthly(anomes: str, get: Callable[[str], Any] = _get) -> list[dict[str, Any]]:
    url = f"{BASE}/TaxasJurosMensalPorMes?$filter=anoMes%20eq%20'{anomes}'&$top=100000&$format=json"
    return get(url).get("value", [])


def fetch_selic(start: dt.date, end: dt.date, get: Callable[[str], Any] = _get) -> list[list[Any]]:
    """[[ISO date, % a.a.], ...] at every change of the Selic target. SGS serves at most ten
    years of a daily series per call, so the span is fetched in five-year slices."""
    rows: list[list[Any]] = []
    a = start
    while a <= end:
        b = min(end, dt.date(a.year + 5, a.month, a.day) - dt.timedelta(days=1))
        url = (f"{SGS_SELIC}?formato=json&dataInicial={a:%d/%m/%Y}&dataFinal={b:%d/%m/%Y}")
        for r in get(url) or []:
            d = dt.datetime.strptime(r["data"], "%d/%m/%Y").date().isoformat()
            v = float(str(r["valor"]).replace(",", "."))
            if not rows or rows[-1][1] != v:
                rows.append([d, v])
        a = b + dt.timedelta(days=1)
    return rows


# --- compact rows -------------------------------------------------------------------

def compact(rows: Iterable[Mapping[str, Any]]) -> list[list[Any]]:
    """[cnpj8, segment (PF/PJ/None), product, position, % a.m., % a.a.] per row."""
    out = []
    for r in rows:
        cnpj8 = str(r.get("cnpj8") or "").strip()
        if len(cnpj8) != 8:
            continue
        seg = SEGMENTS.get(str(r.get("Segmento") or "").strip().upper())
        out.append([cnpj8, seg, str(r.get("Modalidade") or "").strip(), r.get("Posicao"),
                    r.get("TaxaJurosAoMes"), r.get("TaxaJurosAoAno")])
    return out


def product_key(seg: str | None, product: str) -> str:
    return f"{seg}|{product}" if seg else product


# --- series ---------------------------------------------------------------------------

def build_series(windows: Iterable[Mapping[str, Any]], emap: Mapping[str, Any], built_at: str,
                 *, monthly: Iterable[Mapping[str, Any]] = (),
                 names: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Per group entity → product key → points; a market median per product and window; and
    the ``panel`` (product key → cnpj8 → [[as_of, % a.a.], ...]) of every filer, attributed or
    not, which ``pass_through`` needs to compare like with like.

    ``windows``: {"fim": ISO date, "rows": compact rows}. ``monthly``: {"anomes", "rows"},
    dated at the month's last day. ``names`` ({cnpj8: institution}) labels the roots that
    resolve to no registry entity, so the registry gap can be worked from the series itself.
    """
    entities: dict[str, dict[str, list[dict[str, Any]]]] = {}
    market: dict[str, list[list[Any]]] = {}
    panel: dict[str, dict[str, list[list[Any]]]] = {}
    unattributed = attributed = 0
    unknown_roots: dict[str, dict[str, Any]] = {}

    def add(as_of: str, rows: list[list[Any]], series: str) -> None:
        nonlocal unattributed, attributed
        by_key: dict[str, list[float]] = {}
        for cnpj8, seg, prod, pos, am, aa in rows:
            key = product_key(seg, prod)
            if isinstance(aa, (int, float)):
                by_key.setdefault(key, []).append(float(aa))
                panel.setdefault(key, {}).setdefault(cnpj8, []).append([as_of, aa])
            eid = eh.entity_at(emap, cnpj8, as_of)
            if not eid:
                unattributed += 1
                u = unknown_roots.setdefault(cnpj8, {"rows": 0, "first": as_of, "last": as_of,
                                                     "name": (names or {}).get(cnpj8)})
                u["rows"] += 1
                u["first"], u["last"] = min(u["first"], as_of), max(u["last"], as_of)
                continue
            attributed += 1
            entities.setdefault(eid, {}).setdefault(key, []).append({
                "as_of": as_of, "rate_aa": aa, "rate_am": am, "pos": pos, "cnpj8": cnpj8,
                "direct_entity_id": eh.entity_at(emap, cnpj8, as_of, level="direct"),
                "series": series})
        for key, vals in by_key.items():
            market.setdefault(key, []).append([as_of, round(statistics.median(vals), 2), len(vals)])

    for w in sorted(windows, key=lambda w: w["fim"]):
        add(w["fim"], w["rows"], "daily")
    for m in sorted(monthly, key=lambda m: m["anomes"]):
        y, mo = map(int, m["anomes"].split("-"))
        last = (dt.date(y + mo // 12, mo % 12 + 1, 1) - dt.timedelta(days=1)).isoformat()
        add(last, m["rows"], "monthly")
    for by in entities.values():
        for pts in by.values():
            pts.sort(key=lambda p: (p["as_of"], p["cnpj8"]))
    return {"schema": SCHEMA, "built_at": built_at, "source": BASE,
            "attributed_rows": attributed, "unattributed_rows": unattributed,
            "unattributed_roots": len(unknown_roots),
            "unattributed": dict(sorted(unknown_roots.items(), key=lambda kv: -kv[1]["rows"])),
            "entities": entities, "market": market, "panel": panel}


def series_for(s: Mapping[str, Any], entity_id: str, key: str, *, start: str | None = None,
               end: str | None = None, direct: str | None = None) -> list[dict[str, Any]]:
    """Points of one group and product key, optionally only one direct entity's rows."""
    pts = ((s.get("entities") or {}).get(entity_id) or {}).get(key) or []
    return [p for p in pts if (start is None or p["as_of"] >= start)
            and (end is None or p["as_of"] <= end)
            and (direct is None or p.get("direct_entity_id") == direct)]


def products(s: Mapping[str, Any], entity_id: str) -> list[str]:
    return sorted(((s.get("entities") or {}).get(entity_id) or {}).keys())


def selic_at(selic: list[list[Any]], as_of: str) -> float | None:
    """The Selic target in force on ``as_of`` (ISO date)."""
    val = None
    for d, v in selic:
        if d > as_of:
            break
        val = v
    return val


def _end_means(series: Mapping[str, list[list[Any]]], t: str, span_days: int) -> dict[str, float]:
    """{cnpj8: mean % a.a. over (t - span_days, t]} for every filer quoting in that span."""
    lo = (dt.date.fromisoformat(t) - dt.timedelta(days=span_days)).isoformat()
    out = {}
    for cnpj8, pts in series.items():
        vals = [float(v) for d, v in pts if lo < d <= t and isinstance(v, (int, float))]
        if vals:
            out[cnpj8] = statistics.fmean(vals)
    return out


def pass_through(s: Mapping[str, Any], selic: list[list[Any]], entity_id: str, key: str,
                 t0: str, t1: str, *, span_days: int = 28) -> dict[str, Any] | None:
    """How a group's price for one product moved from t0 to t1, next to the Selic target and
    the market. Rates in % a.a.

    Matched panel, never a median of whoever filed: each legal entity (cnpj8) is compared with
    itself, each end averaged over ``span_days``, and only entities quoting at both ends count.
    A group can quote one product through several entities at very different prices
    (Bradescard at 9% next to Bradesco at 100%), and lenders enter and leave the published
    list every week, so a median of the quotes present at each date measures composition, not
    pricing. Probed 2026-11-16: through the 2021–22 hiking cycle (Selic +11.75pp) the plain
    medians showed Bradesco −30pp and the market −11pp on personal credit, while Bradesco's
    own bank had gone from 94.5% to 107.7%.

    The group's change is the median of its matched entities' changes (each listed in
    ``entities``), the market's the median over every matched filer. ``None`` when the group
    has no entity quoting at both ends."""
    panel = (s.get("panel") or {}).get(key) or {}
    own = {p["cnpj8"]: p.get("direct_entity_id") for p in series_for(s, entity_id, key, end=t1)
           if p["as_of"] > (dt.date.fromisoformat(t0) - dt.timedelta(days=span_days)).isoformat()}
    a, b = _end_means(panel, t0, span_days), _end_means(panel, t1, span_days)
    matched = sorted(set(a) & set(b))
    # A root is the group's when the group's series carries it at either end.
    mine = [c for c in matched if c in own]
    if not mine:
        return None
    ent = [{"cnpj8": c, "direct_entity_id": own[c], "rate_from": round(a[c], 2), "rate_to": round(b[c], 2),
            "change_pp": round(b[c] - a[c], 2)} for c in mine]
    group = round(statistics.median(e["change_pp"] for e in ent), 2)
    mkt = round(statistics.median(b[c] - a[c] for c in matched), 2)
    s0, s1 = selic_at(selic, t0), selic_at(selic, t1)
    return {"entity_id": entity_id, "key": key, "from": t0, "to": t1, "span_days": span_days,
            "rate_change_pp": group, "entities": ent,
            "selic_from": s0, "selic_to": s1,
            "selic_change_pp": round(s1 - s0, 2) if s0 is not None and s1 is not None else None,
            "market_change_pp": mkt, "market_matched": len(matched),
            "vs_market_pp": round(group - mkt, 2)}


# --- store ------------------------------------------------------------------------------

def put_json(s3: Any, bucket: str, key: str, doc: Any) -> None:
    s3.put_object(Bucket=bucket, Key=key, ContentType="application/json", ContentEncoding="gzip",
                  Body=gzip.compress(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()))


def get_json(s3: Any, bucket: str, key: str) -> Any:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception:  # noqa: BLE001 - absent = not built yet
        return None
    try:
        body = gzip.decompress(body)
    except OSError:
        pass
    return json.loads(body)
