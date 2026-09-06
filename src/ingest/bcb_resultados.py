"""ADR 022 Tier-3 — operating efficiency (custo operacional / ativo) from the balancete P&L.

Cost intensity = administrative+personnel opex ÷ total assets — a clean, validated efficiency signal
for the CSO. From the monthly balancete (doc 4010): `(-) Despesas Administrativas` (COSIF **8170000004**,
which rolls up pessoal+admin — a YTD flow) over `Ativo Realizável` (**1000000009**, the asset total).
The YTD flow is annualised by ×(12 / month-of-year) to compare with the point-in-time asset stock.

**Why only this ratio (honesty).** A textbook cost-to-income needs a *net* operating-income denominator
the 4010 gross accounts don't cleanly give (that's the DRE, doc 4016). And **custo de crédito** from
4010 came out ~2× known figures (the gross despesa/reversão gross-up overstates net PDD), so it is
deferred to the DRE — and the CRO already reads credit cost via NPL (Tier-2) + the PDD slope (Tier-B).
`opex ÷ ativo` validated against reality (e.g. BB ≈1.56%), so it is the one Tier-3 metric shipped.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable

from src.ingest.bcb_balancete import BULK_URL, _num, latest_month  # reuse the monthly-CSV client

INDEX_KEY = "resultados/index.json"
PUBLIC_URL = "https://www.bcb.gov.br/estabilidadefinanceira/balancetesbalancospatrimoniais"
_OPEX = "8170000004"      # (-) Despesas Administrativas (pessoal + admin), YTD flow
_ATIVO = "1000000009"     # Ativo Realizável (asset total), stock
_DOC = "4010"


def fetch_month(ym: int, *, timeout: int = 120) -> dict[str, dict[str, Any]]:
    """{cnpj8: {name, opex, ativo}} in raw R$ for a base month (opex is |value|, YTD)."""
    import csv
    import io
    import zipfile

    import requests

    resp = requests.get(BULK_URL.format(ym=ym), timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    name = next((n for n in zf.namelist() if n.upper().endswith(".CSV")), None)
    if not name:
        raise requests.RequestException(f"no CSV in {ym} balancete zip")
    raw = io.TextIOWrapper(zf.open(name), encoding="latin-1")
    for _ in range(3):
        raw.readline()
    out: dict[str, dict[str, Any]] = {}
    for row in csv.DictReader(raw, delimiter=";"):
        if row.get("DOCUMENTO") != _DOC:
            continue
        conta = row.get("CONTA")
        if conta not in (_OPEX, _ATIVO):
            continue
        cnpj = row.get("CNPJ")
        v = _num(row.get("SALDO"))
        if not cnpj or v is None:
            continue
        rec = out.setdefault(cnpj, {"name": row.get("NOME_INSTITUICAO")})
        if conta == _OPEX:
            rec["opex"] = abs(v)
        else:
            rec["ativo"] = v
    return out


def map_to_entities(month_data: dict[str, dict[str, Any]], ym: int, *,
                    resolver: Callable[[dict[str, Any]], list[str]],
                    today: dt.date | None = None) -> list[dict[str, Any]]:
    today = today or dt.date.today()
    ann = 12.0 / max(1, ym % 100)             # YTD flow → annualised
    best: dict[str, dict[str, Any]] = {}
    for cnpj, rec in month_data.items():
        opex, ativo = rec.get("opex"), rec.get("ativo")
        name = rec.get("name")
        if not name or not opex or not ativo:
            continue
        try:
            ents = resolver({"source": "News", "title": name, "institution": name}) or []
        except Exception:  # pragma: no cover
            ents = []
        if not ents:
            continue
        opex_ativo = round(100 * opex * ann / ativo, 2)
        row = {"cnpj": cnpj, "name": name, "month": ym,
               "opex_ativo_pct": opex_ativo, "ativo_bi": round(ativo / 1e9, 1),
               "date": today.isoformat()}
        for eid in ents:
            prev = best.get(eid)
            if prev is None or (row["ativo_bi"] or 0) > (prev.get("ativo_bi") or 0):
                best[eid] = {"id": f"bcb-resultados:{eid}", "entity": eid, **row}
    return list(best.values())


def merge(existing: dict[str, Any] | None, records: list[dict[str, Any]], *,
          today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.date.today()
    store = dict((existing or {}).get("records") or {})
    for r in records:
        if r.get("entity"):
            store[r["entity"]] = r
    month = next((r.get("month") for r in records if r.get("month")), (existing or {}).get("month"))
    return {"as_of": today.isoformat(), "month": month, "count": len(store), "records": store}


def resultados_by_entity(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {eid: {"opex_ativo_pct": r.get("opex_ativo_pct"), "month": r.get("month")}
            for eid, r in ((index or {}).get("records") or {}).items() if r.get("opex_ativo_pct") is not None}


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
    ym = latest_month(today)
    recs = map_to_entities(fetch_month(ym), ym, resolver=resolve_entities, today=today)
    if bucket and recs:
        publish(merge(load_index(bucket, s3=s3), recs, today=today), bucket, s3=s3)
    return {"status": "ok", "month": ym, "mapped": len(recs)}


if __name__ == "__main__":
    ym = latest_month()
    recs = map_to_entities(fetch_month(ym), ym, resolver=lambda i: [i["institution"]])
    for r in sorted(recs, key=lambda r: r["opex_ativo_pct"])[:15]:
        print(f"  opex/ativo {r['opex_ativo_pct']:>5}%  {r['name'][:44]}")
