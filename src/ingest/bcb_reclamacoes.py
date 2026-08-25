"""Ingest the Banco Central complaints ranking (issue #31, the RA alternative).

The BCB publishes a **quarterly public ranking of fundamented complaints** per
financial institution — an official, entity-tied consumer-experience signal, with
none of the Cloudflare/ToS problems of Reclame Aqui (see `reclame_aqui.py`). Served
on the BCB Olinda OData platform, same pattern as the other `bcb_*` modules:

  service:   RankingReclamacoes/versao/v1/odata
  resources: RankingMaioresBancos | RankingDemaisBancos | RankingConsorcio
  row:       {Posicao, Ano, Periodo, TipoPeriodo, Categoria, Tipo, CnpjIf,
              InstituicaoFinanceira, Indice}

`Indice` = fundamented complaints normalised per client (BR number format, e.g.
"84,90"). We take the latest published quarter, map each institution name to a
tracked registry entity (only those that resolve are kept), and persist a durable
`bcb_reclamacoes/index.json` store surfaced in the feed + agent.

Best-effort: any HTTP/parse issue degrades to `[]`. `fetcher` is injected in tests.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any, Callable, Iterable

BASE = "https://olinda.bcb.gov.br/olinda/servico/RankingReclamacoes/versao/v1/odata"
RESOURCES = ("RankingMaioresBancos", "RankingDemaisBancos")
INDEX_KEY = "bcb_reclamacoes/index.json"
PUBLIC_URL = "https://www.bcb.gov.br/estabilidadefinanceira/rankingreclamacoes"


def _get_json(url: str) -> dict[str, Any] | None:
    import requests
    resp = requests.get(
        url, timeout=30,
        headers={"User-Agent": "Onca-CI/1.0 (competitive-intelligence)",
                 "Accept": "application/json"},
    )
    return resp.json() if resp.status_code == 200 else None


def _to_float(v: Any) -> float | None:
    """Parse a BR-formatted number ("1.263.435,45" -> 1263435.45)."""
    s = str(v or "").strip()
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _fetch_resource(resource: str, fetch: Callable[[str], Any], top: int) -> list[dict[str, Any]]:
    url = f"{BASE}/{resource}?$top={top}&$orderby=Ano desc,Periodo desc&$format=json"
    data = fetch(url)
    return (data or {}).get("value", []) if isinstance(data, dict) else []


def fetch_ranking(
    *, fetcher: Callable[[str], Any] | None = None, top: int = 400,
) -> list[dict[str, Any]]:
    """All rows for the LATEST published quarter across the tracked resources."""
    fetch = fetcher or _get_json
    rows: list[dict[str, Any]] = []
    for res in RESOURCES:
        try:
            rows.extend(_fetch_resource(res, fetch, top))
        except Exception as exc:  # pragma: no cover - per-resource best-effort
            print(f"Warning: BCB reclamações {res} failed: {exc}")
    if not rows:
        return []
    # Keep only the most recent (Ano, Periodo) present.
    latest = max((int(r.get("Ano") or 0), int(r.get("Periodo") or 0)) for r in rows)
    return [r for r in rows if (int(r.get("Ano") or 0), int(r.get("Periodo") or 0)) == latest]


def _period_label(row: dict[str, Any]) -> str:
    kind = "T" if str(row.get("TipoPeriodo") or "T").upper().startswith("T") else ""
    return f"{row.get('Ano')}-{kind}{row.get('Periodo')}"


def map_to_entities(
    rows: Iterable[dict[str, Any]],
    *,
    resolver: Callable[[dict[str, Any]], list[str]],
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Normalise ranking rows that resolve to a tracked entity into store records.

    One record per entity — the best (lowest) rank if an entity appears twice.
    """
    today = today or dt.date.today()
    best: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        name = row.get("InstituicaoFinanceira")
        if not name:
            continue
        try:
            ents = resolver({"source": "News", "title": name, "institution": name}) or []
        except Exception:  # pragma: no cover - resolver best-effort
            ents = []
        if not ents:
            continue
        rank = int(row.get("Posicao") or 0) or None
        rec_base = {
            "source": "BCB",
            "company": name,
            "rank": rank,
            "index": _to_float(row.get("Indice")),
            "category": row.get("Categoria"),
            "period": _period_label(row),
            "url": PUBLIC_URL,
            "date": today.isoformat(),
        }
        for eid in ents:
            prev = best.get(eid)
            if prev is None or (rank is not None and prev.get("rank") and rank < prev["rank"]):
                best[eid] = {"id": f"bcb-reclamacoes:{eid}", "entity": eid, **rec_base}
    return list(best.values())


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    ranked = [r for r in records if isinstance(r.get("rank"), int)]
    worst = sorted(ranked, key=lambda r: r["rank"])[:5]
    return {
        "kind": "bcb_complaints_ranking",
        "source": "BCB",
        "total": len(records),
        "period": records[0]["period"] if records else None,
        "worst": [{"entity": r["entity"], "rank": r["rank"], "index": r.get("index")}
                  for r in worst],
    }


# --- durable store --------------------------------------------------------

def merge(existing: dict[str, Any] | None, records: list[dict[str, Any]], *,
          today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.date.today()
    idx = dict(existing or {})
    store: dict[str, dict[str, Any]] = dict(idx.get("records") or {})
    for r in records:
        eid = r.get("entity")
        if eid:
            store[eid] = r
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
    s3.put_object(
        Bucket=bucket, Key=INDEX_KEY,
        Body=json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{bucket}/{INDEX_KEY}"


def list_records(index: dict[str, Any]) -> list[dict[str, Any]]:
    recs = list((index.get("records") or {}).values())
    recs.sort(key=lambda r: r.get("rank") if isinstance(r.get("rank"), int) else 9999)
    return recs


def update_store(records: list[dict[str, Any]], bucket: str, *,
                 s3: Any | None = None, today: dt.date | None = None) -> dict[str, Any]:
    index = load_index(bucket, s3=s3)
    merged = merge(index, records, today=today)
    publish(merged, bucket, s3=s3)
    return {"updated": len(records), "records": merged.get("count", 0)}


def run(bucket: str | None = None, *, today: dt.date | None = None) -> dict[str, Any]:
    """Fetch → map → persist. Standalone/scheduled entrypoint."""
    from src.synth.entities import resolve_entities
    rows = fetch_ranking()
    recs = map_to_entities(rows, resolver=resolve_entities, today=today)
    if bucket and recs:
        update_store(recs, bucket, today=today)
    return {"rows": len(rows), "mapped": len(recs), **summarize(recs)}


if __name__ == "__main__":
    print(json.dumps(run(os.environ.get("ONCA_DIGESTS_BUCKET")), ensure_ascii=False))
