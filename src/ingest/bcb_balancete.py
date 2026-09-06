"""ADR 022 Tier B — monthly COSIF balancete trajectory.

The genuinely *monthly* per-institution series between the quarterly Basileia snapshots (Tier A is
quarterly IF.data). Source: BCB's monthly bulk balancete CSV (doc 4010 Balancete Patrimonial),
`.../cosif/Bancos/{YYYYMM}BANCOS.csv.zip` — one file per month, all banks, semicolon-delimited,
latin-1, decimal comma. We extract a **small, explicit** COSIF account→line map (the ADR's "real
work" — a mis-mapped account silently corrupts the slope, so the map is pinned and cited in-store),
resolve the institution name to a tracked entity, and **append one point per month** to a durable
`balancete/index.json` per-entity `series[]`. Month-over-month deltas on the lines that move first
(crédito, PDD/provisões, depósitos) are the leading indicator Tier A confirms a quarter later.

Values are stored in **R$ mil** (thousands). Only institutions that resolve are kept (null over
invented). Append-only: a month already in an entity's series is a no-op.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import zipfile
from typing import Any, Callable

import requests

BULK_URL = "https://www.bcb.gov.br/content/estabilidadefinanceira/cosif/Bancos/{ym}BANCOS.csv.zip"
INDEX_KEY = "balancete/index.json"
PUBLIC_URL = "https://www.bcb.gov.br/estabilidadefinanceira/balancetesbalancospatrimoniais"

# Explicit, pinned COSIF account→line map (headline aggregates). Cited in the store.
LINE_CODES: dict[str, str] = {
    "credito": "1600000007",          # Operações de Crédito
    "depositos": "4100000009",        # Depósitos
    "patrimonio_liquido": "6000000004",  # Patrimônio Líquido
    "disponibilidades": "1100000002",    # Disponibilidades (liquidity proxy — NOT the LCR)
}
# Loan-loss provision (PDD): asset-side "(-) Provisão para perdas de crédito" (current COSIF),
# summed. Stored as a positive magnitude. A rising PDD is the classic early-warning line.
PDD_CODES = frozenset({"1899600005", "1899900004"})
_WANT = set(LINE_CODES.values()) | PDD_CODES
_DOC = "4010"


def _num(v: str | None) -> float | None:
    if not v:
        return None
    try:
        return float(v.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def fetch_month(ym: int, *, timeout: int = 120) -> dict[str, dict[str, Any]]:
    """{cnpj8: {name, lines:{line: R$mil}}} for a base month YYYYMM (doc 4010)."""
    resp = requests.get(BULK_URL.format(ym=ym), timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    name = next((n for n in zf.namelist() if n.upper().endswith(".CSV")), None)
    if not name:
        raise requests.RequestException(f"no CSV in {ym} balancete zip")
    raw = io.TextIOWrapper(zf.open(name), encoding="latin-1")
    for _ in range(3):          # 3 preamble lines before the header
        raw.readline()
    reader = csv.DictReader(raw, delimiter=";")
    by_cnpj: dict[str, dict[str, Any]] = {}
    for row in reader:
        if row.get("DOCUMENTO") != _DOC:
            continue
        conta = row.get("CONTA")
        if conta not in _WANT:
            continue
        cnpj = row.get("CNPJ")
        val = _num(row.get("SALDO"))
        if not cnpj or val is None:
            continue
        rec = by_cnpj.setdefault(cnpj, {"name": row.get("NOME_INSTITUICAO"), "raw": {}})
        rec["raw"][conta] = rec["raw"].get(conta, 0.0) + val / 1000.0   # R$ → R$ mil
    # collapse raw accounts → named lines
    out: dict[str, dict[str, Any]] = {}
    for cnpj, rec in by_cnpj.items():
        raw = rec["raw"]
        lines = {line: round(raw[code], 2) for line, code in LINE_CODES.items() if code in raw}
        pdd = sum(raw[c] for c in PDD_CODES if c in raw)
        if pdd:
            lines["pdd"] = round(-pdd, 2)   # accounts are negative; store positive magnitude
        if lines:
            out[cnpj] = {"name": rec["name"], "lines": lines}
    return out


def latest_month(today: dt.date | None = None) -> int:
    """Most recent month with a published bulk balancete (probe backwards)."""
    today = today or dt.date.today()
    y, m = today.year, today.month
    for _ in range(6):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        ym = y * 100 + m
        try:
            r = requests.head(BULK_URL.format(ym=ym), timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                return ym
        except requests.RequestException:
            continue
    raise requests.RequestException("Could not determine a balancete month")


def map_to_entities(month_data: dict[str, dict[str, Any]], *,
                    resolver: Callable[[dict[str, Any]], list[str]]) -> dict[str, dict[str, Any]]:
    """Resolve institution names → {entity_id: {name, lines}} (largest crédito wins on collision)."""
    best: dict[str, dict[str, Any]] = {}
    for cnpj, rec in month_data.items():
        name = rec.get("name")
        if not name:
            continue
        try:
            ents = resolver({"source": "News", "title": name, "institution": name}) or []
        except Exception:  # pragma: no cover
            ents = []
        cred = (rec.get("lines") or {}).get("credito") or 0
        for eid in ents:
            prev = best.get(eid)
            if prev is None or cred > ((prev.get("lines") or {}).get("credito") or 0):
                best[eid] = {"entity": eid, "cnpj": cnpj, "name": name, "lines": rec.get("lines") or {}}
    return best


def append_month(index: dict[str, Any] | None, ym: int, per_entity: dict[str, dict[str, Any]], *,
                 today: dt.date | None = None) -> dict[str, Any]:
    """Append one monthly point per entity (append-only — an existing month is a no-op)."""
    today = today or dt.date.today()
    records: dict[str, dict[str, Any]] = dict((index or {}).get("records") or {})
    for eid, rec in per_entity.items():
        entry = records.setdefault(eid, {"entity": eid, "name": rec.get("name"), "series": []})
        if any(p.get("month") == ym for p in entry["series"]):
            continue
        entry["series"].append({"month": ym, **(rec.get("lines") or {})})
        entry["series"].sort(key=lambda p: p["month"])
        entry["name"] = rec.get("name") or entry.get("name")
    return {"as_of": today.isoformat(), "latest_month": ym,
            "line_map": {**LINE_CODES, "pdd": "+".join(sorted(PDD_CODES))},
            "count": len(records), "records": records}


def trajectory(entry: dict[str, Any]) -> dict[str, Any]:
    """Latest value + month-over-month % move per line, from an entity's series[]."""
    s = entry.get("series") or []
    if not s:
        return {}
    last = s[-1]
    prev = s[-2] if len(s) > 1 else None
    out: dict[str, Any] = {"month": last.get("month")}
    for line in (*LINE_CODES, "pdd"):
        cur = last.get(line)
        out[line] = cur
        if prev and prev.get(line) not in (None, 0) and cur is not None:
            out[f"{line}_mom_pct"] = round(100 * (cur - prev[line]) / prev[line], 1)
    return out


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


def run(bucket: str | None = None, *, today: dt.date | None = None,
        force: bool = False, s3: Any | None = None) -> dict[str, Any]:
    """Fetch the latest month → resolve → append. Append-only base-month no-op guard."""
    from src.synth.entities import resolve_entities

    ym = latest_month(today)
    if bucket and not force:
        idx = load_index(bucket, s3=s3)
        if idx.get("latest_month") == ym and idx.get("count"):
            return {"status": "noop", "month": ym, "reason": "month unchanged", "records": idx.get("count")}
    data = fetch_month(ym)
    per_entity = map_to_entities(data, resolver=resolve_entities)
    idx = append_month(load_index(bucket, s3=s3) if bucket else None, ym, per_entity, today=today)
    if bucket and per_entity:
        publish(idx, bucket, s3=s3)
    return {"status": "ok", "month": ym, "institutions": len(data), "mapped": len(per_entity),
            "records": idx.get("count")}


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    """OncaFinancialsPipeline BalanceteTask (ADR 022 Phase 2)."""
    import os

    bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    return {"statusCode": 200,
            "body": json.dumps(run(bucket, force=bool((event or {}).get("force")), ), ensure_ascii=False)}
