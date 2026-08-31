"""Ingest consumidor.gov.br complaints — cross-industry consumer-reputation (issue #63).

Generalizes the banking-only BCB complaints ranking (``bcb_reclamacoes``, #31) to ANY
consumer-facing industry: consumidor.gov.br publishes monthly microdata of every finalized
complaint (``finalizadas_YYYY-MM``), with the company (**Nome Fantasia**), whether it was
answered/resolved, and the consumer's rating. We aggregate per company and keep those that
resolve to a tracked entity — a durable reputation store, same shape as ``bcb_reclamacoes``.

**Source route — dados.gov.br** (chosen 2026-08-31): consumidor.gov.br's own dados-abertos
download is JSF/session-gated (not cleanly fetchable), so we resolve the resource URL via
the federated catalog client ([[gov_dados]]) and download from there. That route is
**token-gated** — with no valid ``GOV_DADOS_TOKEN`` (currently 401, needs regeneration) this
ingester is inert. Wired **default-off** (``ONCA_CONSUMIDOR_GOV``).

Resolution is by company NAME (the microdata has no CNPJ) via the injected ``resolver`` —
appropriate on the curated *Nome Fantasia* field, but only companies above a complaint-volume
floor are resolved (cost + precision). Best-effort: degrades to ``[]``. ``downloader`` /
``resolver`` / ``resource_finder`` are injected in tests.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import unicodedata
import zipfile
from typing import Any, Callable, Iterable

INDEX_KEY = "consumidor_gov/index.json"
DATASET_QUERY = "consumidor.gov.br"
PUBLIC_URL = "https://www.consumidor.gov.br/pages/dadosabertos/externo/"
# Canonical field -> accepted header variants (accent/period-tolerant, normalized-compared).
_FIELDS = {
    "company": ("nome fantasia", "fornecedor", "empresa"),
    "segment": ("segmento de mercado", "segmento"),
    "answered": ("respondida",),
    "evaluation": ("avaliacao reclamacao", "avaliacao da reclamacao"),
    "situation": ("situacao",),
    "score": ("nota do consumidor", "nota"),
}


def _norm(s: Any) -> str:
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().strip().lower()


def _colmap(header: list[str]) -> dict[str, int]:
    """Map canonical field -> column index using normalized header matching."""
    normed = [_norm(h) for h in header]
    out: dict[str, int] = {}
    for field, variants in _FIELDS.items():
        for v in variants:
            if v in normed:
                out[field] = normed.index(v)
                break
    return out


def _rows_from_bytes(blob: bytes) -> tuple[list[str], Iterable[list[str]]] | None:
    """Yield (header, row_iter) from a ZIP-of-CSV or a raw CSV (``;``-delim, latin-1)."""
    text: str | None = None
    if blob[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
            member = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
            if member:
                text = zf.read(member).decode("latin-1")
        except (zipfile.BadZipFile, KeyError):  # pragma: no cover - defensive
            return None
    else:
        text = blob.decode("latin-1", "ignore")
    if not text:
        return None
    reader = csv.reader(io.StringIO(text), delimiter=";")
    try:
        header = next(reader)
    except StopIteration:  # pragma: no cover
        return None
    return header, reader


def aggregate(blob: bytes, *, min_complaints: int = 30) -> list[dict[str, Any]]:
    """Aggregate microdata into per-company reputation indicators (volume floor applied)."""
    parsed = _rows_from_bytes(blob)
    if not parsed:
        return []
    header, rows = parsed
    cm = _colmap(header)
    if "company" not in cm:
        return []
    agg: dict[str, dict[str, Any]] = {}

    def cell(row, field):
        i = cm.get(field)
        return row[i] if i is not None and i < len(row) else ""

    for row in rows:
        name = (cell(row, "company") or "").strip()
        if not name:
            continue
        a = agg.setdefault(name, {"company": name, "complaints": 0, "answered": 0,
                                  "resolved": 0, "score_sum": 0.0, "score_n": 0,
                                  "segment": cell(row, "segment").strip() or None})
        a["complaints"] += 1
        if _norm(cell(row, "answered")) in ("s", "sim"):
            a["answered"] += 1
        if _norm(cell(row, "evaluation")).startswith("resolvid"):
            a["resolved"] += 1
        sc = (cell(row, "score") or "").replace(",", ".").strip()
        try:
            if sc:
                a["score_sum"] += float(sc)
                a["score_n"] += 1
        except ValueError:
            pass
    out = []
    for a in agg.values():
        if a["complaints"] < min_complaints:
            continue
        n = a["complaints"]
        out.append({
            "company": a["company"], "segment": a["segment"], "complaints": n,
            "answered_rate": round(a["answered"] / n, 3),
            "resolved_rate": round(a["resolved"] / n, 3),
            "avg_score": round(a["score_sum"] / a["score_n"], 2) if a["score_n"] else None,
        })
    return sorted(out, key=lambda r: -r["complaints"])


def _download(url: str) -> bytes | None:
    import requests
    try:
        resp = requests.get(url, timeout=90, headers={"User-Agent": "Onca-CI/1.0 (competitive-intelligence)"})
        return resp.content if resp.status_code == 200 else None
    except requests.RequestException as exc:  # pragma: no cover - network best-effort
        print(f"Warning: consumidor.gov download failed: {exc}")
        return None


def fetch_indicators(
    *,
    resource_finder: Callable[[], dict[str, Any] | None] | None = None,
    downloader: Callable[[str], bytes | None] | None = None,
    min_complaints: int = 30,
) -> list[dict[str, Any]]:
    """Resolve the latest consumidor.gov resource via the catalog, download, aggregate."""
    if resource_finder is None:
        from src.ingest import gov_dados
        resource_finder = lambda: gov_dados.find_resource(  # noqa: E731
            DATASET_QUERY, name_contains="finalizadas")
    res = resource_finder()
    if not res or not res.get("link"):
        return []
    blob = (downloader or _download)(res["link"])
    if not blob:
        return []
    rows = aggregate(blob, min_complaints=min_complaints)
    for r in rows:
        r["as_of"] = res.get("atualizado") or dt.date.today().isoformat()
    return rows


def map_to_entities(
    rows: Iterable[dict[str, Any]],
    *,
    resolver: Callable[[dict[str, Any]], list[str]],
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Per-company indicators that resolve (by name) to a tracked entity -> store records."""
    today = today or dt.date.today()
    best: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        name = row.get("company")
        try:
            ents = resolver({"source": "ConsumidorGov", "title": name, "institution": name}) or []
        except Exception:  # pragma: no cover - resolver best-effort
            ents = []
        for eid in ents:
            prev = best.get(eid)
            if prev is None or row["complaints"] > prev["complaints"]:
                best[eid] = {"id": f"consumidor:{eid}", "entity": eid, "source": "ConsumidorGov",
                             "url": PUBLIC_URL, "date": today.isoformat(), **row}
    return list(best.values())


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    worst = sorted((r for r in records if r.get("resolved_rate") is not None),
                   key=lambda r: r["resolved_rate"])[:5]
    return {
        "kind": "consumidor_gov_complaints",
        "source": "ConsumidorGov",
        "total": len(records),
        "worst_resolution": [{"entity": r["entity"], "resolved_rate": r["resolved_rate"],
                              "complaints": r["complaints"]} for r in worst],
    }


# --- durable store (mirrors bcb_reclamacoes) ------------------------------

def merge(existing: dict[str, Any] | None, records: list[dict[str, Any]], *,
          today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.date.today()
    store: dict[str, dict[str, Any]] = dict((existing or {}).get("records") or {})
    for r in records:
        store[r["entity"]] = r
    return {"as_of": today.isoformat(), "count": len(store), "records": store}


def load_index(bucket: str, *, s3: Any | None = None) -> dict[str, Any]:
    import boto3
    s3 = s3 or boto3.client("s3")
    try:
        body = s3.get_object(Bucket=bucket, Key=INDEX_KEY)["Body"].read()
        data = json.loads(body)
        return data if isinstance(data, dict) else {}
    except Exception:  # pragma: no cover - first run
        return {}


def publish(index: dict[str, Any], bucket: str, *, s3: Any | None = None) -> str:
    import boto3
    s3 = s3 or boto3.client("s3")
    s3.put_object(Bucket=bucket, Key=INDEX_KEY,
                  Body=json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8"),
                  ContentType="application/json")
    return f"s3://{bucket}/{INDEX_KEY}"


def list_records(index: dict[str, Any]) -> list[dict[str, Any]]:
    recs = list((index.get("records") or {}).values())
    recs.sort(key=lambda r: r.get("complaints") or 0, reverse=True)
    return recs


def update_store(records: list[dict[str, Any]], bucket: str, *,
                 s3: Any | None = None, today: dt.date | None = None) -> dict[str, Any]:
    merged = merge(load_index(bucket, s3=s3), records, today=today)
    publish(merged, bucket, s3=s3)
    return {"updated": len(records), "records": merged.get("count", 0)}


def run(bucket: str | None = None, *, today: dt.date | None = None,
        min_complaints: int = 30) -> dict[str, Any]:
    """Fetch → map → persist. Standalone entrypoint (token-gated; inert with no token)."""
    from src.synth.entities import resolve_entities
    rows = fetch_indicators(min_complaints=min_complaints)
    recs = map_to_entities(rows, resolver=resolve_entities, today=today)
    if bucket and recs:
        update_store(recs, bucket, today=today)
    return {"companies": len(rows), "mapped": len(recs), **summarize(recs)}


if __name__ == "__main__":
    import os
    print(json.dumps(run(os.environ.get("ONCA_DIGESTS_BUCKET")), ensure_ascii=False))
