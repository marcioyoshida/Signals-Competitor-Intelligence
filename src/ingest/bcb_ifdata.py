"""Ingest BCB IF.data — quarterly institution-level financials.

The historical endpoint name used in the old implementation is no longer
available. The current public service accepts the OData entity set
`IfDataValores` and the filter arguments shown below.

`market_share()` returns rows keyed by raw institution NAME. To surface a
per-entity share on the dashboard (ADR 015 §3), those names are resolved to
registry `entity_id`s via the same resolver `bcb_reclamacoes.map_to_entities`
uses and persisted in a durable `bcb_ifdata/index.json` store (same shape as
`bcb_reclamacoes/index.json`), loaded by `feed_builder` into `entities[].
market_share_pct`. Only institutions that resolve are kept — the rest stay null,
never an invented number (CLAUDE.md no-unlabeled-proxy rule).
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable, Iterable

import requests

BASE = "https://olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1/odata"
INDEX_KEY = "bcb_ifdata/index.json"
PUBLIC_URL = "https://www3.bcb.gov.br/ifdata/"

# TipoInstituicao=2 -> conglomerados prudenciais e instituições independentes
# Relatorio=T -> summary report (assets, credit, deposits, equity)
TIPO_INSTITUICAO = 2
RELATORIO = "T"


def latest_base_date() -> int:
    """Return the most recent published base date as YYYYMM (e.g. 202603)."""
    # The legacy ListaDeDatas endpoint is unavailable; the working service
    # accepts a direct AnoMes filter, so we prefer the most recent known-good
    # quarterly value from the current quarter.
    for base_date in [202603, 202602, 202601, 202512, 202511]:
        try:
            rows = fetch_institutions(base_date=base_date)
            if rows:
                return base_date
        except requests.RequestException:
            continue
    raise requests.RequestException("Could not determine an IF.data base date")


def fetch_institutions(base_date: int | None = None) -> list[dict[str, Any]]:
    """Fetch summary financials for all institutions at a base date.

    Rows are keyed by CodInst only — this report has no institution name
    field. Resolve display names separately via fetch_institution_names.
    """
    base_date = base_date or latest_base_date()
    url = (
        f"{BASE}/IfDataValores("
        f"AnoMes=@AnoMes,TipoInstituicao=@TipoInstituicao,Relatorio=@Relatorio)"
        f"?@AnoMes={base_date}"
        f"&@TipoInstituicao={TIPO_INSTITUICAO}"
        f"&@Relatorio='{RELATORIO}'"
        f"&$format=json"
    )
    resp = requests.get(url, timeout=120, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.json().get("value", [])


def fetch_institution_names(base_date: int) -> dict[str, str]:
    """Map CodInst -> institution name via the IF.data cadastro function.

    $top is set well above the current registry size (~5.9k institutions)
    since this endpoint doesn't expose a total count to paginate against.
    """
    url = f"{BASE}/IfDataCadastro(AnoMes=@AnoMes)?@AnoMes={base_date}&$format=json&$top=10000"
    resp = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return {r["CodInst"]: r["NomeInstituicao"] for r in resp.json().get("value", []) if r.get("CodInst")}


def market_share(
    rows: list[dict[str, Any]],
    metric: str = "Ativo Total",
    institution_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Compute share of a metric across institutions.

    IF.data returns long-format rows (one row per institution+account),
    keyed by CodInst. Filter to the metric, sum the sector total, compute
    each share. Pass institution_names (from fetch_institution_names) to
    show names instead of raw codes; falls back to the code if omitted.
    """
    names = institution_names or {}
    values: dict[str, float] = {}
    for r in rows:
        if r.get("NomeColuna") == metric and r.get("Saldo") is not None:
            code = r.get("CodInst", "?")
            values[code] = values.get(code, 0.0) + float(r["Saldo"])

    total = sum(values.values()) or 1.0
    ranked = sorted(values.items(), key=lambda kv: kv[1], reverse=True)
    return [
        {"institution": names.get(code, code), "value": round(val, 2), "share_pct": round(100 * val / total, 3)}
        for code, val in ranked
    ]


# --- name -> entity_id resolution + durable store (ADR 015 §3) -----------
# Mirrors bcb_reclamacoes: resolve the raw institution NAME to a tracked
# registry entity, keep only rows that resolve, and persist one record per
# entity in a durable index (same shape as bcb_reclamacoes/index.json).


def map_to_entities(
    shares: Iterable[dict[str, Any]],
    *,
    resolver: Callable[[dict[str, Any]], list[str]],
    metric: str = "Ativo Total",
    base_date: int | None = None,
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Normalise `market_share()` rows that resolve to a tracked entity into store
    records carrying `market_share_pct`. One record per entity — the largest share
    if an entity resolves from more than one institution row (e.g. a conglomerate).
    """
    today = today or dt.date.today()
    best: dict[str, dict[str, Any]] = {}
    for row in shares or []:
        name = row.get("institution")
        share = row.get("share_pct")
        if not name or share is None:
            continue
        try:
            ents = resolver({"source": "News", "title": name, "institution": name}) or []
        except Exception:  # pragma: no cover - resolver best-effort
            ents = []
        if not ents:
            continue
        rec_base = {
            "source": "BCB",
            "institution": name,
            "metric": metric,
            "market_share_pct": share,
            "value": row.get("value"),
            "base_date": base_date,
            "url": PUBLIC_URL,
            "date": today.isoformat(),
        }
        for eid in ents:
            prev = best.get(eid)
            if prev is None or share > (prev.get("market_share_pct") or 0.0):
                best[eid] = {"id": f"bcb-ifdata:{eid}", "entity": eid, **rec_base}
    return list(best.values())


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    ranked = sorted(
        records, key=lambda r: r.get("market_share_pct") or 0.0, reverse=True
    )
    return {
        "kind": "ifdata_market_share",
        "source": "BCB",
        "metric": records[0]["metric"] if records else "Ativo Total",
        "total": len(records),
        "base_date": records[0].get("base_date") if records else None,
        "top": [
            {"entity": r["entity"], "market_share_pct": r.get("market_share_pct")}
            for r in ranked[:5]
        ],
    }


def merge(
    existing: dict[str, Any] | None,
    records: list[dict[str, Any]],
    *,
    today: dt.date | None = None,
) -> dict[str, Any]:
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
    recs.sort(key=lambda r: r.get("market_share_pct") or 0.0, reverse=True)
    return recs


def share_by_entity(index: dict[str, Any]) -> dict[str, float]:
    """{entity_id: market_share_pct} projection of the store — the join
    `feed_builder` reads to stamp `entities[].market_share_pct`."""
    out: dict[str, float] = {}
    for eid, r in (index.get("records") or {}).items():
        pct = r.get("market_share_pct")
        if eid and pct is not None:
            out[eid] = pct
    return out


def update_store(records: list[dict[str, Any]], bucket: str, *,
                 s3: Any | None = None, today: dt.date | None = None) -> dict[str, Any]:
    index = load_index(bucket, s3=s3)
    merged = merge(index, records, today=today)
    publish(merged, bucket, s3=s3)
    return {"updated": len(records), "records": merged.get("count", 0)}


def run(bucket: str | None = None, *, today: dt.date | None = None) -> dict[str, Any]:
    """Fetch → market share → resolve → persist. Standalone/scheduled entrypoint."""
    from src.synth.entities import resolve_entities

    base_date = latest_base_date()
    rows = fetch_institutions(base_date=base_date)
    names = fetch_institution_names(base_date)
    shares = market_share(rows, institution_names=names)
    recs = map_to_entities(
        shares, resolver=resolve_entities, base_date=base_date, today=today
    )
    if bucket and recs:
        update_store(recs, bucket, today=today)
    return {"rows": len(rows), "mapped": len(recs), **summarize(recs)}


if __name__ == "__main__":
    date = latest_base_date()
    print(f"Latest IF.data base date: {date}")
    rows = fetch_institutions(date)
    names = fetch_institution_names(date)
    for row in market_share(rows, institution_names=names)[:15]:
        print(f"{row['share_pct']:6.2f}%  {row['institution']}")
