"""Ingest federal public contracts from PNCP — the demand signal (issue #62).

"Who wins government business" is a sector-agnostic demand/traction signal for any B2G
supplier. The intended source (Portal da Transparência) is unusable tokenless — its bulk
contract download 500s and its ``/contratos/cpf-cnpj`` API needs a separate token — so we
use **PNCP** (Portal Nacional de Contratações Públicas, Lei 14.133), the official open
contracts portal, whose ``/v1/contratos`` endpoint is tokenless and date-ranged.

Resolution is **CNPJ-root only** (same safe pattern as CEIS/CNEP): a contract binds to a
specific supplier CNPJ; a match against a tracked entity's ``cnpj_roots`` is exact. Records
stamp ``_entities`` so synth binds the card to that entity.

Efficiency note: PNCP has **no supplier filter** — ``/v1/contratos`` returns *all* federal
contracts in a window (~18k/day). We therefore scan a short rolling window, bounded by
``max_pages``, and drop contracts below ``min_valor``. For the FS registry this yields ~0
(banks are not federal suppliers), so it is **default-off** (``ONCA_PNCP_CONTRATOS``); it is
meant for the sectorial deployment, whose tracked universe (retail/logistics/energy/health
suppliers) genuinely appears here. Best-effort: degrades to ``[]``.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Callable, Iterable

BASE = "https://pncp.gov.br/api/consulta/v1/contratos"
PORTAL = "https://pncp.gov.br/app/contratos"
INDEX_KEY = "contracts/index.json"
PAGE_SIZE = 500                      # PNCP hard max
_DIGITS = re.compile(r"\D+")


def _cnpj_root(raw: Any) -> str | None:
    digits = _DIGITS.sub("", str(raw or ""))
    return digits[:8] if len(digits) >= 14 else None


def _default_fetcher(d1: str, d2: str, page: int, size: int) -> dict[str, Any]:
    import requests
    resp = requests.get(
        BASE, timeout=45,
        params={"dataInicial": d1, "dataFinal": d2, "pagina": page, "tamanhoPagina": size},
        headers={"User-Agent": "Onca-CI/1.0 (competitive-intelligence)", "Accept": "application/json"},
    )
    return resp.json() if resp.status_code == 200 else {}


def fetch_contracts(
    days_back: int = 2,
    *,
    today: dt.date | None = None,
    fetcher: Callable[[str, str, int, int], dict[str, Any]] | None = None,
    max_pages: int = 60,
    page_size: int = PAGE_SIZE,
    min_valor: float = 0.0,
) -> list[dict[str, Any]]:
    """Recent PJ federal contracts (bounded scan). One rolling window per run."""
    today = today or dt.date.today()
    fetch = fetcher or _default_fetcher
    d1 = (today - dt.timedelta(days=days_back)).strftime("%Y%m%d")
    d2 = today.strftime("%Y%m%d")
    out: list[dict[str, Any]] = []
    page = 1
    while page <= max_pages:
        try:
            payload = fetch(d1, d2, page, page_size)
        except Exception as exc:  # pragma: no cover - upstream best-effort
            print(f"Warning: PNCP contracts page {page} failed: {exc}")
            break
        rows = (payload or {}).get("data") or []
        if not rows:
            break
        for r in rows:
            if str(r.get("tipoPessoa") or "").upper() != "PJ":
                continue
            root = _cnpj_root(r.get("niFornecedor"))
            if not root:
                continue
            valor = r.get("valorGlobal") or r.get("valorInicial")
            try:
                valor = float(valor) if valor is not None else None
            except (TypeError, ValueError):
                valor = None
            if min_valor and (valor is None or valor < min_valor):
                continue
            control = str(r.get("numeroControlePncpCompra") or "").strip()
            org = r.get("orgaoEntidade") or {}
            out.append({
                "id": f"pncp:{control}" if control else f"pncp:{root}:{r.get('dataAssinatura')}",
                "source": "PNCP",
                "kind": "contract",
                "cnpj_root": root,
                "supplier_cnpj": _DIGITS.sub("", str(r.get("niFornecedor") or "")),
                "company": (r.get("nomeRazaoSocialFornecedor") or "").strip() or None,
                "buyer": (org.get("razaoSocial") or "").strip() or None,
                "buyer_cnpj": org.get("cnpj"),
                "valor": valor,
                "objeto": (str(r.get("objetoContrato") or "").strip() or None),
                "signed": r.get("dataAssinatura"),
                "start": r.get("dataVigenciaInicio"),
                "end": r.get("dataVigenciaFim"),
                "date": r.get("dataAssinatura") or r.get("dataVigenciaInicio"),
                "control": control or None,
                "url": f"{PORTAL}?q={control}" if control else PORTAL,
            })
        total_pages = int((payload or {}).get("totalPaginas") or 0)
        if total_pages and page >= total_pages:
            break
        page += 1
    return out


def build_cnpj_index(entities: Iterable[dict[str, Any]]) -> dict[str, str]:
    """root(8) -> entity_id, from the registry (enables exact CNPJ resolution)."""
    idx: dict[str, str] = {}
    for e in entities or []:
        eid = e.get("entity_id")
        if not eid:
            continue
        for r in e.get("cnpj_roots") or []:
            root = str(r)[:8]
            if root:
                idx.setdefault(root, eid)
    return idx


def map_to_entities(
    rows: Iterable[dict[str, Any]],
    *,
    cnpj_index: dict[str, str] | None = None,
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Keep only contracts whose supplier CNPJ root matches a tracked entity.

    CNPJ-only by design (a contract binds to a legal person; name matching would
    mis-attribute). Each record stamps ``_entities`` (CNPJ-anchored)."""
    today = today or dt.date.today()
    cnpj_index = cnpj_index or {}
    out: list[dict[str, Any]] = []
    for row in rows or []:
        eid = cnpj_index.get(row.get("cnpj_root") or "")
        if not eid:
            continue
        valor = row.get("valor")
        title = f"Contrato federal: {row.get('buyer') or 'órgão público'}" + (
            f" — R$ {valor:,.0f}" if isinstance(valor, (int, float)) else "")
        out.append({
            **row,
            "id": f"contracts:{eid}:{row['id']}",
            "entity": eid,
            "_entities": [eid],
            "title": title,
            "text": " | ".join(x for x in (
                row.get("company"), row.get("objeto"), row.get("buyer"),
                f"R$ {valor:,.2f}" if isinstance(valor, (int, float)) else None,
                f"vigência {row.get('start') or '?'}→{row.get('end') or '?'}") if x),
            "mapped_at": today.isoformat(),
        })
    return out


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_entity: dict[str, dict[str, Any]] = {}
    for r in records:
        e = r["entity"]
        agg = by_entity.setdefault(e, {"entity": e, "count": 0, "total_valor": 0.0})
        agg["count"] += 1
        if isinstance(r.get("valor"), (int, float)):
            agg["total_valor"] += r["valor"]
    return {
        "kind": "federal_contracts",
        "source": "PNCP",
        "total": len(records),
        "entities": len(by_entity),
        "top": sorted(by_entity.values(), key=lambda a: -a["total_valor"])[:5],
    }


# --- durable store --------------------------------------------------------

def merge(existing: dict[str, Any] | None, records: list[dict[str, Any]], *,
          today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.date.today()
    store: dict[str, dict[str, Any]] = dict((existing or {}).get("records") or {})
    for r in records:
        store[r["id"]] = r
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
    recs.sort(key=lambda r: str(r.get("signed") or ""), reverse=True)
    return recs


def update_store(records: list[dict[str, Any]], bucket: str, *,
                 s3: Any | None = None, today: dt.date | None = None) -> dict[str, Any]:
    index = load_index(bucket, s3=s3)
    merged = merge(index, records, today=today)
    publish(merged, bucket, s3=s3)
    return {"updated": len(records), "records": merged.get("count", 0)}


def run(bucket: str | None = None, *, today: dt.date | None = None,
        days_back: int = 2, min_valor: float = 0.0) -> dict[str, Any]:
    """Fetch → map → persist. Standalone/scheduled entrypoint."""
    from src.synth.entity_registry import list_entities
    rows = fetch_contracts(days_back=days_back, today=today, min_valor=min_valor)
    idx = build_cnpj_index(list_entities())
    recs = map_to_entities(rows, cnpj_index=idx, today=today)
    if bucket and recs:
        update_store(recs, bucket, today=today)
    return {"rows": len(rows), "mapped": len(recs), **summarize(recs)}


if __name__ == "__main__":
    import os
    print(json.dumps(run(os.environ.get("ONCA_DIGESTS_BUCKET")), ensure_ascii=False))
