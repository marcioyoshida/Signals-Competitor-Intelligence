"""ADR 022 (multi-bank Pilar 3) — Basel KM1 "Key Metrics" per institution, via BCB **DASFN**.

The robust, uniform multi-bank Pilar 3 source (Res. 54/20). BCB's **DASFN** catalog
(`olinda.bcb.gov.br/.../DASFN`) registers every SFN institution's own open-data Pilar 3 API; the
standardized **KM1** table (`Api=pilar3`, `Recurso=/km1/…`) carries the Basel Key Metrics as a fixed
template with a built-in 5-quarter trajectory (`t, t_1..t_4`):

    km1_7  → Índice de Basileia (total capital ratio)     km1_5/6 → CET1 / Tier 1
    km1_14 → Razão de Alavancagem                         km1_17 → **LCR** (Liquidez de Curto Prazo)
                                                          km1_20 → **NSFR**

This is what finally gives us **LCR/NSFR** — the liquidity that is NOT in IF.data (ADR §1). Ratios are
stored as fractions in the JSON → ×100. Institutions self-host the JSON (opaque per-quarter URLs), but
DASFN lists them uniformly, so discovery is robust (no per-bank scraping). We resolve the institution
name to a tracked entity, take the latest KM1, and persist `pilar3_km1/index.json`.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Callable

import requests

DASFN = "https://olinda.bcb.gov.br/olinda/servico/DASFN/versao/v1/odata"
INDEX_KEY = "pilar3_km1/index.json"
_UA = {"User-Agent": "Mozilla/5.0"}
# KM1 row code → canonical metric (fraction → %). Rows are found in any group (robust to layout).
_ROW_METRIC = {"km1_5": "cet1_pct", "km1_6": "tier1_pct", "km1_7": "basileia_pct",
               "km1_14": "leverage_pct", "km1_17": "lcr_pct", "km1_20": "nsfr_pct"}


def _get(url: str, *, tries: int = 4, timeout: int = 60) -> Any:
    last = ""
    for i in range(tries):
        try:
            r = requests.get(url, timeout=timeout, headers=_UA)
        except requests.RequestException as e:  # pragma: no cover
            last = str(e); continue
        if r.status_code != 200:
            last = f"HTTP {r.status_code}"; continue
        b = r.content.decode("utf-8-sig", errors="replace").strip()   # tolerate BOM (self-hosted JSON)
        b = re.sub(r"^\s*/\*|\*/\s*$", "", b).strip()
        if not b:
            last = "empty"; continue
        return json.loads(b)
    raise requests.RequestException(f"DASFN fetch failed ({last}) {url[:70]}")


def _base_date(url: str) -> int:
    """Latest-sort key for a self-hosted KM1 URL: prefer the reference date (…_20260630_…), else the
    filing date (…/26-08-29_…, YY-MM-DD → 2026-08-29). 0 if neither."""
    m = re.search(r"(20\d{6})", url or "")
    if m:
        return int(m.group(1))
    m = re.search(r"/km1/(\d{4})-(\d)\b", url or "")     # /km1/2026-2 → end-of-quarter YYYYMMDD
    if m:
        return int(m.group(1)) * 10000 + int(m.group(2)) * 3 * 100 + 30
    m = re.search(r"/(\d\d)-(\d\d)-(\d\d)_", url or "")   # filing date YY-MM-DD
    return int("20" + m.group(1) + m.group(2) + m.group(3)) if m else 0


def list_km1_resources() -> dict[str, dict[str, Any]]:
    """{cnpj: {name, url, base_date}} — latest KM1 per institution across the DASFN catalog."""
    data = _get(f"{DASFN}/Recursos?$filter=Api%20eq%20'pilar3'&$format=json&$top=100000")
    latest: dict[str, dict[str, Any]] = {}
    for r in data.get("value", []):
        if not (r.get("Recurso") or "").startswith("/km1"):
            continue
        cnpj, url = r.get("CnpjInstituicao"), r.get("URLDados")
        if not cnpj or not url:
            continue
        bd = _base_date(url)
        prev = latest.get(cnpj)
        if prev is None or bd > prev["base_date"]:
            latest[cnpj] = {"name": r.get("NomeInstituicao"), "url": url, "base_date": bd}
    return latest


def _find_row(doc: dict[str, Any], code: str) -> dict[str, Any] | None:
    # flat layout (Itaú): the row code is a top-level key
    if isinstance(doc.get(code), dict) and "t" in doc[code]:
        return doc[code]
    # grouped layout (Santander): the row code sits inside a group dict
    for v in doc.values():
        if isinstance(v, dict) and isinstance(v.get(code), dict) and "t" in v[code]:
            return v[code]
    return None


def _num(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def extract_km1(doc: Any) -> dict[str, Any]:
    """Pull the KM1 key metrics (×100) + the LCR 5-quarter trajectory from a KM1 JSON."""
    if isinstance(doc, list):           # some banks wrap the doc in a single-element list
        doc = doc[0] if doc and isinstance(doc[0], dict) else {}
    if not isinstance(doc, dict):
        return {}
    # plausibility bounds (%) — a self-hosted JSON with a different scale/structure parses to absurd
    # values (e.g. Basileia 1444%); reject rather than surface a wrong number.
    _bounds = {"cet1_pct": 60, "tier1_pct": 60, "basileia_pct": 60,
               "leverage_pct": 40, "lcr_pct": 3000, "nsfr_pct": 800}
    out: dict[str, Any] = {"trimestre": doc.get("km1_trimestreReferencia")}
    for code, metric in _ROW_METRIC.items():
        row = _find_row(doc, code)
        v = _num(row.get("t")) if row else None
        pct = round(v * 100, 2) if v is not None else None
        if pct is not None and not (0 <= pct <= _bounds[metric]):
            pct = None                      # implausible → bad parse, drop
        out[metric] = pct
    lcr = _find_row(doc, "km1_17")
    if lcr:
        series = [_num(lcr.get(c)) for c in ("t_4", "t_3", "t_2", "t_1", "t")]
        out["lcr_series_pct"] = [round(s * 100, 1) if s is not None else None for s in series]
        if out.get("lcr_pct") is not None and _num(lcr.get("t_1")) not in (None, 0):
            out["lcr_qoq_pp"] = round(out["lcr_pct"] - _num(lcr.get("t_1")) * 100, 1)
    return out


def lcr_band(lcr_pct: float | None) -> str | None:
    if lcr_pct is None:
        return None
    if lcr_pct < 100:   # below the 100% regulatory minimum
        return "crítica"
    if lcr_pct < 130:
        return "atenção"
    return "confortável"


def build(resources: dict[str, dict[str, Any]], *, resolver: Callable[[dict[str, Any]], list[str]],
          today: dt.date | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Resolve each institution → fetch its latest KM1 → per-entity record (largest LCR-HQLA wins)."""
    today = today or dt.date.today()
    best: dict[str, dict[str, Any]] = {}
    for cnpj, meta in resources.items():
        name = meta.get("name")
        if not name:
            continue
        try:
            ents = resolver({"source": "News", "title": name, "institution": name}) or []
        except Exception:  # pragma: no cover
            ents = []
        if not ents:
            continue
        try:
            km = extract_km1(_get(meta["url"]))
        except Exception as exc:  # pragma: no cover - self-hosted endpoints vary
            print(f"Warning: KM1 fetch failed for {name}: {exc}")
            continue
        if km.get("basileia_pct") is None and km.get("lcr_pct") is None:
            continue
        rec = {"cnpj": cnpj, "name": name, "base_date": meta.get("base_date"),
               "band_lcr": lcr_band(km.get("lcr_pct")), "date": today.isoformat(), **km}
        for eid in ents:
            prev = best.get(eid)
            if prev is None or (meta.get("base_date") or 0) >= (prev.get("base_date") or 0):
                best[eid] = {"id": f"bcb-km1:{eid}", "entity": eid, **rec}
        if limit and len(best) >= limit:
            break
    return list(best.values())


def merge(existing: dict[str, Any] | None, records: list[dict[str, Any]], *,
          today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.date.today()
    store = dict((existing or {}).get("records") or {})
    for r in records:
        if r.get("entity"):
            store[r["entity"]] = r
    return {"as_of": today.isoformat(), "count": len(store), "records": store}


def km1_by_entity(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    _keys = ("lcr_pct", "nsfr_pct", "band_lcr", "lcr_qoq_pp", "basileia_pct", "leverage_pct", "trimestre")
    return {eid: {k: r.get(k) for k in _keys}
            for eid, r in ((index or {}).get("records") or {}).items()
            if r.get("lcr_pct") is not None or r.get("basileia_pct") is not None}


def load_index(bucket: str, *, s3: Any | None = None) -> dict[str, Any]:
    import boto3
    s3 = s3 or boto3.client("s3")
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=INDEX_KEY)["Body"].read())
    except Exception:  # pragma: no cover
        return {}


def publish(index: dict[str, Any], bucket: str, *, s3: Any | None = None) -> str:
    import boto3
    s3 = s3 or boto3.client("s3")
    s3.put_object(Bucket=bucket, Key=INDEX_KEY,
                  Body=json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8"),
                  ContentType="application/json")
    return f"s3://{bucket}/{INDEX_KEY}"


def run(bucket: str | None = None, *, today: dt.date | None = None, s3: Any | None = None) -> dict[str, Any]:
    from src.synth.entities import resolve_entities
    resources = list_km1_resources()
    recs = build(resources, resolver=resolve_entities, today=today)
    if bucket and recs:
        publish(merge(load_index(bucket, s3=s3), recs, today=today), bucket, s3=s3)
    return {"status": "ok", "institutions": len(resources), "mapped": len(recs)}


if __name__ == "__main__":
    res = list_km1_resources()
    print(f"KM1 resources (institutions): {len(res)}")
    recs = build(res, resolver=lambda i: [i["institution"]], limit=12)
    for r in sorted(recs, key=lambda r: r.get("lcr_pct") or 0):
        print(f"  LCR {r.get('lcr_pct')!s:>7}% [{r.get('band_lcr')!s:11}] NSFR {r.get('nsfr_pct')!s:>6} "
              f"Basileia {r.get('basileia_pct')!s:>6}  {r['name'][:36]}")
