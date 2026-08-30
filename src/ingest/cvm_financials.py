"""CVM financial statements (DFP annual / ITR quarterly) → per-issuer key metrics.

Issue #7 / ADR 011 stage 6. Fetches the CVM Demonstrações Financeiras open data
(dados.cvm.gov.br), parses the consolidated Balanço (BPA/BPP) and Resultado (DRE) for
LISTED tracked issuers (matched by CNPJ root), and reduces each to a compact record —
assets, equity, revenue, net income — for the latest period AND the prior one, so the
downstream store can compute net margin, YoY revenue growth and leverage.

Store shape (one record per entity), written by ``persist`` to ``financials/index.json``:
    {entity_id: {name, period, prior_period, currency, revenue, net_income, assets,
                 equity, prior_revenue, prior_net_income, net_margin, revenue_growth,
                 leverage, source_url, as_of}}
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from typing import Any

import requests

BASE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC"
DFP_URL = BASE + "/DFP/DADOS/dfp_cia_aberta_{year}.zip"
ITR_URL = BASE + "/ITR/DADOS/itr_cia_aberta_{year}.zip"
DATASET_URL = "https://dados.cvm.gov.br/dataset/cia_aberta-doc-dfp"

# CVM standardized account codes (consolidated statements).
_CD_ASSETS = "1"        # BPA: Ativo Total
_CD_EQUITY = "2.03"     # BPP: Patrimônio Líquido (Consolidado)
_CD_REVENUE = "3.01"    # DRE: top line (Receita de Venda / Receitas da Intermediação)


def _root8(cnpj: str | None) -> str:
    return "".join(ch for ch in str(cnpj or "") if ch.isdigit())[:8]


def _num(v: Any) -> float | None:
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _fold(s: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().upper()


def _scale(row: dict[str, Any]) -> float:
    return 1000.0 if _fold(row.get("ESCALA_MOEDA")).startswith("MIL") else 1.0


def _open_csv(zf: zipfile.ZipFile, needle: str):
    name = next((n for n in zf.namelist() if needle in n and n.endswith(".csv")), None)
    if not name:
        return []
    text = io.TextIOWrapper(zf.open(name), encoding="latin-1")
    return list(csv.DictReader(text, delimiter=";"))


def _pick_net_income(dre_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The bottom-line profit row — CD_CONTA 3.11 when present, else the DRE line whose
    label is 'Lucro/Prejuízo … do Período' (highest such code)."""
    exact = [r for r in dre_rows if r.get("CD_CONTA") == "3.11"]
    if exact:
        return exact[0]
    cands = [
        r for r in dre_rows
        if str(r.get("CD_CONTA", "")).startswith("3.")
        and "LUCRO" in _fold(r.get("DS_CONTA")) and "PERIODO" in _fold(r.get("DS_CONTA"))
    ]
    return max(cands, key=lambda r: r.get("CD_CONTA", ""), default=None)


def fetch_statements(year: int, *, doc: str = "DFP", timeout: int = 90) -> dict[str, dict[str, Any]]:
    """Parse one year's consolidated statements into
    ``{cnpj_root: {ordem: {assets, equity, revenue, net_income, period, name, cd_cvm}}}``
    where ordem is 'ÚLTIMO' (current) / 'PENÚLTIMO' (prior). Best-effort ({} on error).
    """
    url = (DFP_URL if doc.upper() == "DFP" else ITR_URL).format(year=year)
    try:
        raw = requests.get(url, timeout=timeout).content
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except Exception:  # pragma: no cover - network/best-effort
        return {}

    out: dict[str, dict[str, Any]] = {}

    def _ensure(cnpj: str, ordem: str, row: dict[str, Any]) -> dict[str, Any]:
        rec = out.setdefault(cnpj, {}).setdefault(ordem, {
            "period": (row.get("DT_FIM_EXERC") or row.get("DT_REFER") or "")[:10],
            "name": row.get("DENOM_CIA"), "cd_cvm": row.get("CD_CVM"),
        })
        return rec

    # Balance sheet — assets (BPA) + equity (BPP)
    for needle, code, field in (("BPA_con", _CD_ASSETS, "assets"),
                                ("BPP_con", _CD_EQUITY, "equity")):
        for row in _open_csv(zf, needle):
            if row.get("CD_CONTA") != code:
                continue
            cnpj, ordem = _root8(row.get("CNPJ_CIA")), (row.get("ORDEM_EXERC") or "").upper()
            val = _num(row.get("VL_CONTA"))
            if cnpj and ordem and val is not None:
                _ensure(cnpj, ordem, row)[field] = val * _scale(row)

    # Income statement — revenue + net income (grouped per issuer×period for net-income pick)
    dre = _open_csv(zf, "DRE_con")
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in dre:
        cnpj, ordem = _root8(row.get("CNPJ_CIA")), (row.get("ORDEM_EXERC") or "").upper()
        if cnpj and ordem:
            by_key.setdefault((cnpj, ordem), []).append(row)
    for (cnpj, ordem), rows in by_key.items():
        rev = next((r for r in rows if r.get("CD_CONTA") == _CD_REVENUE), None)
        ni = _pick_net_income(rows)
        if rev is None and ni is None:
            continue
        rec = _ensure(cnpj, ordem, rows[0])
        if rev is not None and _num(rev.get("VL_CONTA")) is not None:
            rec["revenue"] = _num(rev["VL_CONTA"]) * _scale(rev)
        if ni is not None and _num(ni.get("VL_CONTA")) is not None:
            rec["net_income"] = _num(ni["VL_CONTA"]) * _scale(ni)
    return out


def build_index(
    entities: list[dict[str, Any]], statements: dict[str, dict[str, Any]],
    *, source_url: str = DATASET_URL, resolver: Any = None,
) -> dict[str, dict[str, Any]]:
    """Match parsed statements to tracked issuers and reduce each to a compact record
    with derived metrics. Matches by CNPJ root (strong); for issuers with no CNPJ on the
    entity, falls back to ``resolver`` (resolve_entities) on the company name, accepting
    only a single, LISTED (tickered) tracked entity. Keyed by entity_id."""
    by_root: dict[str, str] = {}
    for e in entities:
        for r in e.get("cnpj_roots") or []:
            by_root[str(r)[:8]] = e["entity_id"]
    tickered = {e["entity_id"] for e in entities if e.get("ticker")}

    # Resolve each issuer to a tracked entity, keeping the STRONGEST claim per entity:
    # CNPJ (priority 2) beats a name match (1); within the same priority the largest by
    # revenue wins (the main operating entity, not a small holding sharing the brand).
    best: dict[str, tuple[int, float, dict[str, Any]]] = {}
    for cnpj, periods in statements.items():
        name = (periods.get("ÚLTIMO") or periods.get("PENÚLTIMO") or {}).get("name")
        eid = by_root.get(cnpj)
        prio = 2 if eid else 0
        if not eid and resolver and name:
            hits = [h for h in resolver({"institution": name}) if h in tickered]
            if len(hits) == 1:
                eid, prio = hits[0], 1
        if not eid:
            continue
        rev = float((periods.get("ÚLTIMO") or {}).get("revenue") or 0)
        cur = best.get(eid)
        if cur is None or (prio, rev) > (cur[0], cur[1]):
            best[eid] = (prio, rev, periods)

    index: dict[str, dict[str, Any]] = {}
    for eid, (_prio, _rev, periods) in best.items():
        cur = periods.get("ÚLTIMO") or {}
        prior = periods.get("PENÚLTIMO") or {}
        if not cur.get("revenue") and not cur.get("net_income") and not cur.get("assets"):
            continue
        rev, ni = cur.get("revenue"), cur.get("net_income")
        assets, equity = cur.get("assets"), cur.get("equity")
        prev_rev = prior.get("revenue")
        rec = {
            "entity_id": eid, "name": cur.get("name"), "period": cur.get("period"),
            "prior_period": prior.get("period"), "currency": "BRL",
            "revenue": rev, "net_income": ni, "assets": assets, "equity": equity,
            "prior_revenue": prev_rev, "prior_net_income": prior.get("net_income"),
            "net_margin": round(ni / rev, 4) if rev and ni is not None and rev != 0 else None,
            "revenue_growth": round((rev - prev_rev) / prev_rev, 4)
            if rev is not None and prev_rev not in (None, 0) else None,
            "leverage": round((assets - equity) / equity, 3)
            if assets is not None and equity not in (None, 0) else None,
            "source_url": source_url,
        }
        index[eid] = rec
    return index


INDEX_KEY = "financials/index.json"


def persist(bucket: str, index: dict[str, dict[str, Any]], *, s3: Any = None) -> str:
    """Write the financials index to the digests bucket. Returns the s3 uri."""
    import boto3

    s3 = s3 or boto3.client("s3")
    body = json.dumps({"as_of": _today(), "records": index}, ensure_ascii=False).encode("utf-8")
    s3.put_object(Bucket=bucket, Key=INDEX_KEY, Body=body, ContentType="application/json")
    return f"s3://{bucket}/{INDEX_KEY}"


def load_index(bucket: str, *, s3: Any = None) -> list[dict[str, Any]]:
    """Read the financials store as a list of records. [] if absent."""
    import boto3

    s3 = s3 or boto3.client("s3")
    try:
        body = s3.get_object(Bucket=bucket, Key=INDEX_KEY)["Body"].read()
    except Exception:  # pragma: no cover - best-effort
        return []
    return list((json.loads(body).get("records") or {}).values())


def _today() -> str:
    import datetime as dt

    return dt.date.today().isoformat()
