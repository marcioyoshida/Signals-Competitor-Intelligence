"""Ingest BCB IF.data prudential **solvency** (ADR 022 Tier A) — the Basileia set.

Companion to `bcb_ifdata` (market share). Where that reads the summary report for *size*, this
reads **Relatório 5 "Informações de Capital"** (`TipoInstituicao=1`) for *soundness*:

    Índice de Basileia, Índice de Capital Nível I (Tier 1), Índice de Capital Principal (CET1),
    Razão de Alavancagem, Índice de Imobilização

Two pilot-confirmed specifics (see `scripts/pilot_basileia_solvency.py`):
  1. The índices are stored as **fractions** in `Saldo` → ×100 for a percentage.
  2. The capital-report `CodInst` under `TipoInstituicao=1` are prudential-conglomerate codes named
     ``'<BRAND> - PRUDENCIAL'`` — resolve the brand (suffix stripped) to a registry entity, exactly
     as `bcb_ifdata` resolves its institution names.

Only institutions that resolve to a tracked entity are kept; the rest stay null (never an invented
number — CLAUDE.md no-unlabeled-proxy rule). A durable `soundness/index.json` (same shape as
`bcb_ifdata/index.json`) is joined by `feed_builder` into `entities[].soundness`.

**Liquidity is NOT here** — IF.data publishes no LCR/NSFR; that is ADR 022 Phase 6 (Pilar 3). The
`Índice de Imobilização` is a solvency-adjacent capital-use metric, not liquidity.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from typing import Any, Callable, Iterable

import requests

from src.ingest.bcb_ifdata import fetch_institution_names  # cadastro is tipo-independent

BASE = "https://olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1/odata"
INDEX_KEY = "soundness/index.json"
PUBLIC_URL = "https://www3.bcb.gov.br/ifdata/"

# Capital report: Relatório 5 "Informações de Capital" under TipoInstituicao=1.
RELATORIO_CAPITAL = "5"
TIPO_INSTITUICAO = 1

# canonical field -> the NomeColuna prefix that carries it (values are fractions → ×100).
METRIC_COLUMNS: dict[str, str] = {
    "indice_basileia": "Índice de Basileia",
    "capital_nivel_i": "Índice de Capital Nível I",
    "capital_principal": "Índice de Capital Principal",
    "razao_alavancagem": "Razão de Alavancagem",
    "indice_imobilizacao": "Índice de Imobilização",
}
_PRUDENCIAL_SUFFIX = re.compile(r"\s*-\s*PRUDENCIAL\s*$", re.I)


def _get(url: str, *, tries: int = 3, timeout: int = 90) -> list[dict[str, Any]]:
    """IF.data GET, fast-failing: the OData service throws transient 500s and sometimes wraps the
    JSON body in ``/* ... */``. Retry a FEW times with a short backoff (a blocking network call the
    per-source budget can't interrupt, so keep the worst case small) and unwrap before parsing."""
    last = ""
    for i in range(tries):
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        except requests.RequestException as exc:  # pragma: no cover - network
            last = str(exc)
            time.sleep(1 + i)
            continue
        if resp.status_code != 200:
            last = f"HTTP {resp.status_code}"
            time.sleep(1 + i)
            continue
        body = resp.text.strip()
        if body.startswith("/*"):
            body = body[2:]
        if body.endswith("*/"):
            body = body[:-2]
        body = body.strip()
        if not body:
            last = "empty body"
            time.sleep(1 + i)
            continue
        return json.loads(body).get("value", [])
    raise requests.RequestException(f"IF.data capital fetch failed after {tries} ({last})")


def _capital_url(base_date: int, *, top: int | None = None) -> str:
    url = (
        f"{BASE}/IfDataValores(AnoMes=@A,TipoInstituicao=@T,Relatorio=@R)"
        f"?@A={base_date}&@T={TIPO_INSTITUICAO}&@R='{RELATORIO_CAPITAL}'&$format=json"
    )
    return f"{url}&$top={top}" if top else url


def fetch_capital(base_date: int) -> list[dict[str, Any]]:
    """Long-format capital rows (CodInst, NomeColuna, Saldo) for a base date."""
    return _get(_capital_url(base_date))


def latest_base_date() -> int:
    """Most recent quarter for which the capital report has data (YYYYMM).

    Probes with ``$top=1`` (cheap) rather than pulling the whole 33k-row report per candidate, so a
    flaky endpoint can't turn base-date discovery into a multi-minute retry storm."""
    for base_date in (202603, 202512, 202509, 202506, 202503, 202412):
        try:
            if _get(_capital_url(base_date, top=1), tries=2):
                return base_date
        except requests.RequestException:
            continue
    raise requests.RequestException("Could not determine a capital-report base date")


def _match_metric(nome: str | None) -> str | None:
    for key, prefix in METRIC_COLUMNS.items():
        if (nome or "").strip().startswith(prefix):
            return key
    return None


def extract_solvency(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """{CodInst: {metric: value_pct}} — índices scaled from fraction to percent (×100)."""
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        code = r.get("CodInst")
        metric = _match_metric(r.get("NomeColuna"))
        if not code or not metric or r.get("Saldo") is None:
            continue
        out.setdefault(code, {})[metric] = round(float(r["Saldo"]) * 100, 2)
    return out


def soundness_band(basileia_pct: float | None) -> str | None:
    """Regulatory floor 8% + 2.5% conservation buffer ≈ 10.5% practical minimum."""
    if basileia_pct is None:
        return None
    if basileia_pct < 10.5:
        return "frágil"
    if basileia_pct < 13.0:
        return "atenção"
    return "sólido"


def map_to_entities(
    solvency: dict[str, dict[str, float]],
    names: dict[str, str],
    *,
    resolver: Callable[[dict[str, Any]], list[str]],
    base_date: int | None = None,
    today: dt.date | None = None,
    conglomerates_only: bool = True,
) -> list[dict[str, Any]]:
    """Resolve each capital-report institution (``'<BRAND> - PRUDENCIAL'``) to a tracked entity and
    emit one solvency record per entity — the highest Basileia if an entity resolves from more than
    one prudential code.

    `conglomerates_only` (default True) resolves ONLY the prudential-conglomerate rows (name ends
    ``- PRUDENCIAL``) — the conglomerate-level solvency view, and it keeps the resolver call count
    bounded (~230 not ~1300+) so the source stays inside its ingest budget."""
    today = today or dt.date.today()
    best: dict[str, dict[str, Any]] = {}
    for code, metrics in solvency.items():
        raw_name = names.get(code)
        if not raw_name or "indice_basileia" not in metrics:
            continue
        if conglomerates_only and not _PRUDENCIAL_SUFFIX.search(raw_name):
            continue
        brand = _PRUDENCIAL_SUFFIX.sub("", raw_name).strip()
        try:
            ents = resolver({"source": "News", "title": brand, "institution": brand}) or []
        except Exception:  # pragma: no cover - resolver best-effort
            ents = []
        if not ents:
            continue
        basileia = metrics.get("indice_basileia")
        rec_base = {
            "source": "BCB",
            "institution": raw_name,
            "cod_inst": code,
            "base_date": base_date,
            "band": soundness_band(basileia),
            "url": PUBLIC_URL,
            "date": today.isoformat(),
            **{m: metrics.get(m) for m in METRIC_COLUMNS},
        }
        for eid in ents:
            prev = best.get(eid)
            if prev is None or (basileia or 0) > (prev.get("indice_basileia") or 0):
                best[eid] = {"id": f"bcb-soundness:{eid}", "entity": eid, **rec_base}
    return list(best.values())


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    ranked = sorted(records, key=lambda r: r.get("indice_basileia") or 0.0)
    return {
        "kind": "prudential_solvency",
        "source": "BCB",
        "metric": "Índice de Basileia",
        "total": len(records),
        "base_date": records[0].get("base_date") if records else None,
        # weakest first — the ones a risk officer looks at
        "weakest": [
            {"entity": r["entity"], "indice_basileia": r.get("indice_basileia"), "band": r.get("band")}
            for r in ranked[:5]
        ],
    }


def merge(existing: dict[str, Any] | None, records: list[dict[str, Any]], *,
          today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.date.today()
    store: dict[str, dict[str, Any]] = dict((existing or {}).get("records") or {})
    for r in records:
        eid = r.get("entity")
        if eid:
            store[eid] = r
    # Carry the base quarter at the top level so the monthly pipeline can no-op cheaply
    # when the published quarter hasn't changed (falls back to the previous value).
    base_date = next((r.get("base_date") for r in records if r.get("base_date")),
                     (existing or {}).get("base_date"))
    return {"as_of": today.isoformat(), "base_date": base_date, "count": len(store), "records": store}


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
    recs.sort(key=lambda r: r.get("indice_basileia") or 0.0)
    return recs


def soundness_by_entity(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{entity_id: {indice_basileia, capital_nivel_i, capital_principal, razao_alavancagem,
    indice_imobilizacao, band, base_date}} — the projection `feed_builder` stamps onto entities."""
    out: dict[str, dict[str, Any]] = {}
    for eid, r in (index.get("records") or {}).items():
        if not eid or r.get("indice_basileia") is None:
            continue
        out[eid] = {**{m: r.get(m) for m in METRIC_COLUMNS},
                    "band": r.get("band"), "base_date": r.get("base_date")}
    return out


def update_store(records: list[dict[str, Any]], bucket: str, *,
                 s3: Any | None = None, today: dt.date | None = None) -> dict[str, Any]:
    merged = merge(load_index(bucket, s3=s3), records, today=today)
    publish(merged, bucket, s3=s3)
    return {"updated": len(records), "records": merged.get("count", 0)}


def run(bucket: str | None = None, *, today: dt.date | None = None,
        force: bool = False, s3: Any | None = None) -> dict[str, Any]:
    """Fetch capital report → extract solvency → resolve → persist. Standalone/scheduled entrypoint.

    **Base-month no-op guard** (ADR 022 §4): if the store already holds the latest published quarter,
    skip the whole fetch/resolve — so the monthly pipeline (and any retry) costs almost nothing when
    the data hasn't changed. `force=True` bypasses it."""
    from src.synth.entities import resolve_entities

    base_date = latest_base_date()
    if bucket and not force:
        idx = load_index(bucket, s3=s3)
        if idx.get("base_date") == base_date and idx.get("count"):
            return {"status": "noop", "base_date": base_date, "reason": "quarter unchanged",
                    "records": idx.get("count")}

    rows = fetch_capital(base_date)
    names = fetch_institution_names(base_date)
    solvency = extract_solvency(rows)
    recs = map_to_entities(solvency, names, resolver=resolve_entities, base_date=base_date, today=today)
    if bucket and recs:
        update_store(recs, bucket, s3=s3, today=today)
    return {"status": "ok", "base_date": base_date, "rows": len(rows),
            "institutions": len(solvency), "mapped": len(recs), **summarize(recs)}


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    """Entry point for the monthly OncaFinancialsPipeline (ADR 022 Phase 3). Reads
    ONCA_DIGESTS_BUCKET, honours ``{"force": true}`` to bypass the base-month no-op guard."""
    bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    result = run(bucket, force=bool((event or {}).get("force")))
    return {"statusCode": 200, "body": json.dumps(result, ensure_ascii=False)}


if __name__ == "__main__":
    date = latest_base_date()
    print(f"Latest capital-report base date: {date}")
    rows = fetch_capital(date)
    names = fetch_institution_names(date)
    solvency = extract_solvency(rows)
    ranked = sorted(
        ((names.get(c, c), m) for c, m in solvency.items() if "indice_basileia" in m),
        key=lambda x: x[1]["indice_basileia"],
    )
    for name, m in ranked[:15]:
        print(f"  Basileia {m['indice_basileia']:6.2f}%  [{soundness_band(m['indice_basileia'])!s:8}] {name}")
