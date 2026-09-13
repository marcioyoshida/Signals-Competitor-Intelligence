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
import time
from typing import Any, Callable, Iterable

import requests

from src.ingest.budget import SourceBudgetExceeded as _BUDGET_ABORT

BASE ="https://olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1/odata"
INDEX_KEY = "bcb_ifdata/index.json"
PUBLIC_URL = "https://www3.bcb.gov.br/ifdata/"

# TipoInstituicao=2 -> conglomerados prudenciais e instituições independentes
# Relatorio=T -> summary report (assets, credit, deposits, equity)
TIPO_INSTITUICAO = 2
RELATORIO = "T"


def _candidate_base_dates(today: dt.date | None = None, back: int = 6) -> list[int]:
    """Recent quarter-end base dates (YYYYMM), newest first.

    IF.data publishes quarterly (03/06/09/12) with a ~3-month lag, so probing the
    last ~6 quarters always covers the newest published one without a hardcoded
    list that silently goes stale.
    """
    today = today or dt.date.today()
    q_month = ((today.month - 1) // 3) * 3 or 12
    year = today.year if ((today.month - 1) // 3) else today.year - 1
    out: list[int] = []
    for _ in range(back):
        out.append(year * 100 + q_month)
        q_month -= 3
        if q_month <= 0:
            q_month += 12
            year -= 1
    return out


CANDIDATE_BASE_DATES = _candidate_base_dates()


def _valores_url(base_date: int, *, top: int | None = None) -> str:
    url = (
        f"{BASE}/IfDataValores("
        f"AnoMes=@AnoMes,TipoInstituicao=@TipoInstituicao,Relatorio=@Relatorio)"
        f"?@AnoMes={base_date}"
        f"&@TipoInstituicao={TIPO_INSTITUICAO}"
        f"&@Relatorio='{RELATORIO}'"
        f"&$format=json"
    )
    if top:
        url += f"&$top={top}"
    return url


def latest_base_date() -> int:
    """Return the most recent published base date as YYYYMM (e.g. 202603).

    The legacy ListaDeDatas endpoint is unavailable; the working service accepts a
    direct AnoMes filter, so we probe the most recent known-good quarterly values.

    The probe uses ``$top=1`` (a ~120-byte response). It used to call the FULL
    ``fetch_institutions`` per candidate, i.e. it downloaded the whole 58MB /
    153k-row report just to decide the date and then downloaded it a SECOND time
    to actually use it — the single biggest contributor to the structured-ingest
    900s Lambda timeouts observed 2026-09-08..12.
    """
    for base_date in _candidate_base_dates():
        try:
            resp = requests.get(
                _valores_url(base_date, top=1),
                timeout=(10, 30),
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            if resp.json().get("value"):
                return base_date
        except requests.RequestException:
            continue
        except ValueError:  # non-JSON body from an upstream error page
            continue
    raise requests.RequestException("Could not determine an IF.data base date")


def fetch_institutions(base_date: int | None = None) -> list[dict[str, Any]]:
    """Fetch summary financials for all institutions at a base date.

    Rows are keyed by CodInst only — this report has no institution name
    field. Resolve display names separately via fetch_institution_names.

    ~58MB / 153k rows. The read timeout is per-socket-read, so a trickling server
    can still stretch this out; the caller's per-source wall-clock budget
    (``ONCA_SOURCE_TIMEOUT_SEC``) is the real bound.
    """
    base_date = base_date or latest_base_date()
    resp = requests.get(
        _valores_url(base_date),
        timeout=(10, 60),
        headers={"User-Agent": "Mozilla/5.0"},
    )
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
    start: int = 0,
    limit: int | None = None,
    deadline: float | None = None,
    stats: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Normalise `market_share()` rows that resolve to a tracked entity into store
    records carrying `market_share_pct`. One record per entity — the largest share
    if an entity resolves from more than one institution row (e.g. a conglomerate).

    ``start``/``limit`` window which institution names are put through the resolver.
    IF.data returns ~1,422 institutions and `resolve_entities` costs ~200-600ms per
    call (a CPU-bound pass over the whole alias map), so the uncapped loop measured
    ~320-840s — on its own it consumed the ingest's entire 900s Lambda budget and was
    the direct cause of the 2026-09-08..12 structured-ingest timeouts. The caller
    walks the list a window at a time across runs (`resolve_progress`), which keeps
    full coverage without a per-run stall; `shares` is sorted by share DESC so the
    materially-sized institutions are resolved first.
    """
    today = today or dt.date.today()
    best: dict[str, dict[str, Any]] = {}
    rows = list(shares or [])[start:]
    if limit is not None and limit > 0:
        rows = rows[:limit]
    processed = 0
    for row in rows:
        # A soft deadline stops the window cleanly (records kept, cursor advanced by
        # what we actually did) instead of letting the caller's SIGALRM budget kill
        # the whole source and lose the window — which would livelock the walk.
        if deadline is not None and time.monotonic() >= deadline:
            break
        processed += 1
        name = row.get("institution")
        share = row.get("share_pct")
        if not name or share is None:
            continue
        try:
            ents = resolver({"source": "News", "title": name, "institution": name}) or []
        except _BUDGET_ABORT:  # a wall-clock budget kill must NOT be swallowed here
            raise
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
    if stats is not None:
        stats["processed"] = processed
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
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    today = today or dt.date.today()
    idx = dict(existing or {})
    store: dict[str, dict[str, Any]] = dict(idx.get("records") or {})
    for r in records:
        eid = r.get("entity")
        if eid:
            store[eid] = r
    out = {"as_of": today.isoformat(), "count": len(store), "records": store}
    # Carry the resolve cursor forward so the next run resumes instead of redoing
    # the whole 1,422-name pass (see resolve_progress).
    for key in ("base_date", "cursor", "universe", "top"):
        if progress and key in progress:
            out[key] = progress[key]
        elif key in idx:
            out[key] = idx[key]
    return out


def resolve_progress(index: dict[str, Any] | None, base_date: int) -> tuple[int, bool]:
    """(next cursor, already-complete) for ``base_date`` against a stored index.

    IF.data is QUARTERLY, but the pipeline runs 3x/day — ~270 runs per quarter that
    used to redo the identical 58MB download + 1,422-name resolve and produce a
    byte-identical store. Once the whole institution list has been walked for a base
    date, the source is a no-op until BCB publishes the next quarter.
    """
    idx = index or {}
    if idx.get("base_date") != base_date:
        return 0, False  # new quarter -> restart the walk
    cursor = int(idx.get("cursor") or 0)
    universe = int(idx.get("universe") or 0)
    return cursor, bool(universe and cursor >= universe)


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


def system_size(index: dict[str, Any]) -> dict[str, Any]:
    """#75/R3: the SYSTEM-WIDE market size the IF.data metric measures (e.g. Ativo Total = the SFN
    asset base) + who leads it — the "credit/asset stock" size the requirement names, which the
    CVM-revenue market_structure (listed issuers only) cannot give.

    The metric total is system-wide (ALL institutions, not only the ones that resolve to a tracked
    entity): for any stored record, ``total = value / (share_pct/100)``. Returned honestly as the
    whole-system base with `scope` set — NOT attributed to a single sector (that would double-count
    a system-wide number across industries). Returns {} if the store lacks value+share."""
    recs = [r for r in (index.get("records") or {}).values() if isinstance(r, dict)]
    total = None
    for r in recs:
        v, s = r.get("value"), r.get("market_share_pct")
        try:
            if v is not None and s:
                total = float(v) / (float(s) / 100.0)
                break
        except (TypeError, ValueError, ZeroDivisionError):
            continue
    if total is None:
        return {}
    ranked = sorted((r for r in recs if r.get("market_share_pct") is not None),
                    key=lambda r: r["market_share_pct"], reverse=True)
    return {
        "metric": recs[0].get("metric") if recs else "Ativo Total",
        "size_value": round(total, 2),
        "currency": "BRL",
        "base_date": recs[0].get("base_date") if recs else None,
        "scope": "sistema (todas as instituições IF.data, não apenas emissores listados)",
        "source": "BCB IF.data",
        "resolved": len(ranked),
        "top": [{"entity": r["entity"], "share_pct": r.get("market_share_pct"),
                 "value": r.get("value")} for r in ranked[:8]],
    }


def update_store(records: list[dict[str, Any]], bucket: str, *,
                 s3: Any | None = None, today: dt.date | None = None,
                 progress: dict[str, Any] | None = None,
                 index: dict[str, Any] | None = None) -> dict[str, Any]:
    index = load_index(bucket, s3=s3) if index is None else index
    merged = merge(index, records, today=today, progress=progress)
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
