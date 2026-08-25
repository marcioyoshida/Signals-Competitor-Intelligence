"""Ingest consumer-reputation signals from Reclame Aqui (issue #31).

Reclame Aqui (reclameaqui.com.br) is Brazil's dominant consumer-complaints platform;
every company has a public reputation page with a 0–10 score, a reputation label
(ÓTIMO/BOM/REGULAR/RUIM/NÃO RECOMENDADA), complaint volume, answered/solved rates and
a "would-do-business-again" index. For a competitor-intelligence tool these are a
first-class *customer-experience* signal — exactly what "quantas reclamações no Reclame
Aqui sobre bancos digitais?" (a coverage gap) needs.

There is **no official public API**; the site's own front-end reads a JSON search
service (the "raichu" endpoints). This module queries those best-effort:

  search:     GET {IOSEARCH}/raichu-io-site-search-v1/query/companySearch/{n}/{off}?q=
  reputation: GET {IOSEARCH}/raichu-io-site-search-v1/company/reputation/{id}/{period}

**Discipline (accuracy-critical, like the distress store):** a reputation/complaint
figure is a public but sensitive claim about a company, so the store fills only from a
real fetch — nothing is fabricated. Parsing is fully defensive (`.get()` everything) so
a shape change degrades to `[]` rather than crashing the digest. `fetcher` is injected
in tests; the live path is best-effort and rate-limited.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import unicodedata
from typing import Any, Callable, Iterable

# NOTE: the public reclameaqui.com.br front-end reads these "raichu" JSON services,
# but the origin sits behind **Cloudflare bot protection** — a plain server-side GET
# gets a 403 "Just a moment…" challenge (confirmed 2026-08-25). So this adapter is
# *correct but access-gated*: it works when `fetcher` is injected (tests) or when
# `ONCA_RA_IOSEARCH` points at an AUTHORIZED path (an official/partner RA data feed,
# a licensed proxy, or a rendering gateway). We do NOT ship Cloudflare evasion. The
# ingest gate `ONCA_RECLAME_AQUI` therefore defaults OFF; enable it only with a
# working, authorized endpoint. See issue #31.
IOSEARCH = os.environ.get(
    "ONCA_RA_IOSEARCH",
    "https://iosearch.reclameaqui.com.br/raichu-io-site-search-v1",
)
DEFAULT_PERIOD = "SIX_MONTHS"  # RA reputation window
INDEX_KEY = "reputation/index.json"

# Curated set of tracked registry entity_ids with a meaningful Reclame Aqui presence
# (retail-facing banks/fintechs — the "bancos digitais" the gap asks about). The RA
# query defaults to the registry display_name; a shortname override pins a page when
# the name search is ambiguous.
DEFAULT_ENTITY_IDS: tuple[str, ...] = (
    "nubank", "inter", "c6", "banco_pan", "bmg", "picpay", "mercado_pago",
    "pagseguro", "stone", "digio", "will_bank", "agibank", "neon", "original",
)


def _fold(s: Any) -> str:
    t = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def _get_json(url: str) -> dict[str, Any] | list[Any] | None:
    import requests
    resp = requests.get(
        url, timeout=15,
        headers={"User-Agent": "Onca-CI/1.0 (competitive-intelligence)",
                 "Accept": "application/json"},
    )
    if resp.status_code != 200:
        return None
    return resp.json()


def _search_company(name: str, fetch: Callable[[str], Any]) -> dict[str, Any] | None:
    """Best match for a company name → {id, shortname, name}, or None."""
    from urllib.parse import quote
    data = fetch(f"{IOSEARCH}/query/companySearch/10/0?q={quote(str(name))}")
    companies = []
    if isinstance(data, dict):
        companies = data.get("companies") or data.get("data") or []
    elif isinstance(data, list):
        companies = data
    if not companies:
        return None
    nf = _fold(name)
    # prefer an exact/startswith fantasy-name match; else the first result.
    best = None
    for c in companies:
        cn = _fold(c.get("fantasyName") or c.get("companyName") or c.get("name"))
        if cn == nf or cn.startswith(nf) or nf.startswith(cn):
            best = c
            break
    best = best or companies[0]
    cid = best.get("id") or best.get("companyId")
    if cid is None:
        return None
    return {
        "id": str(cid),
        "shortname": best.get("shortname") or best.get("shortName") or "",
        "name": best.get("fantasyName") or best.get("companyName") or best.get("name") or name,
    }


def _reputation(company_id: str, period: str, fetch: Callable[[str], Any]) -> dict[str, Any] | None:
    data = fetch(f"{IOSEARCH}/company/reputation/{company_id}/{period}")
    if not isinstance(data, dict):
        return None
    return data


def _to_float(v: Any) -> float | None:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def fetch_reputation(
    companies: Iterable[dict[str, Any]],
    *,
    period: str = DEFAULT_PERIOD,
    fetcher: Callable[[str], Any] | None = None,
    pause_sec: float = 0.4,
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Per-entity reputation snapshots for `companies` (each {entity_id, name,
    shortname?}). Best-effort; a failed company is skipped, never raised."""
    today = today or dt.date.today()
    fetch = fetcher or _get_json
    out: list[dict[str, Any]] = []
    for c in companies:
        eid = c.get("entity_id")
        name = c.get("name") or eid
        if not eid or not name:
            continue
        try:
            hit = _search_company(name, fetch)
            if not hit:
                continue
            rep = _reputation(hit["id"], period, fetch)
            if not rep:
                continue
            shortname = hit.get("shortname") or c.get("shortname") or ""
            out.append({
                "id": f"reclameaqui:{eid}",
                "source": "ReclameAqui",
                "kind": "reputation",
                "entity": eid,
                "company": hit.get("name") or name,
                "ra_id": hit["id"],
                "ra_shortname": shortname,
                "score": _to_float(rep.get("finalScore") or rep.get("score")),
                "status": rep.get("status") or rep.get("reputationStatus"),
                "complaints": rep.get("complainsCount") or rep.get("complaintsCount"),
                "answered_pct": _to_float(rep.get("answeredPercentual")),
                "solved_pct": _to_float(rep.get("solvedPercentual")),
                "deal_again_pct": _to_float(rep.get("dealAgainPercentual")),
                "period": period,
                "url": f"https://www.reclameaqui.com.br/empresa/{shortname}/" if shortname else
                       "https://www.reclameaqui.com.br/",
                "date": today.isoformat(),
            })
        except Exception as exc:  # pragma: no cover - per-company best-effort
            print(f"Warning: Reclame Aqui fetch failed for {eid}: {exc}")
        if pause_sec:
            time.sleep(pause_sec)
    return out


def companies_from_registry(entity_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Build the {entity_id, name} list from the registry display_names."""
    ids = list(entity_ids or DEFAULT_ENTITY_IDS)
    out: list[dict[str, Any]] = []
    try:
        from src.synth import entity_registry
        for eid in ids:
            e = entity_registry.get_entity(eid)
            if e and e.get("active", True):
                out.append({"entity_id": eid, "name": e.get("display_name") or eid})
    except Exception as exc:  # pragma: no cover - registry optional
        print(f"Warning: registry unavailable for Reclame Aqui companies: {exc}")
    return out


def summarize(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the reputation signal: count + worst/least-recommended entities."""
    scored = [s for s in snapshots if isinstance(s.get("score"), (int, float))]
    worst = sorted(scored, key=lambda s: s["score"])[:5]
    return {
        "kind": "consumer_reputation",
        "source": "ReclameAqui",
        "total": len(snapshots),
        "worst": [{"entity": s["entity"], "score": s["score"], "status": s.get("status")}
                  for s in worst],
    }


# --- durable store (reputation/index.json) --------------------------------

def merge_reputation(
    existing: dict[str, Any] | None,
    snapshots: list[dict[str, Any]],
    *,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Upsert the latest snapshot per entity, keeping the previous score for a
    trend arrow."""
    today = today or dt.date.today()
    idx = dict(existing or {})
    records: dict[str, dict[str, Any]] = dict(idx.get("records") or {})
    for s in snapshots:
        eid = s.get("entity")
        if not eid:
            continue
        prev = records.get(eid)
        rec = dict(s)
        if prev and isinstance(prev.get("score"), (int, float)) and prev.get("date") != s.get("date"):
            rec["prev_score"] = prev.get("score")
        records[eid] = rec
    return {"as_of": today.isoformat(), "count": len(records), "records": records}


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
    s3.put_object(
        Bucket=bucket, Key=INDEX_KEY,
        Body=json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{bucket}/{INDEX_KEY}"


def list_records(index: dict[str, Any]) -> list[dict[str, Any]]:
    recs = list((index.get("records") or {}).values())
    recs.sort(key=lambda r: (r.get("score") if isinstance(r.get("score"), (int, float)) else 99))
    return recs


def update_store(
    snapshots: list[dict[str, Any]], bucket: str, *,
    s3: Any | None = None, today: dt.date | None = None,
) -> dict[str, Any]:
    index = load_index(bucket, s3=s3)
    merged = merge_reputation(index, snapshots, today=today)
    publish(merged, bucket, s3=s3)
    return {"updated": len(snapshots), "records": merged.get("count", 0)}


if __name__ == "__main__":  # manual/scheduled fetch
    bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    comps = companies_from_registry()
    snaps = fetch_reputation(comps)
    if bucket and snaps:
        print(json.dumps(update_store(snaps, bucket), ensure_ascii=False))
    else:
        print(json.dumps({"fetched": len(snaps), "persisted": False}, ensure_ascii=False))
