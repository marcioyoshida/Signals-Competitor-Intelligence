"""ADR 022 Tier-2 — inadimplência / NPL (asset quality) for the CRO.

The credit-quality number the ADR named in §1 and never had. IF.data **Relatório 8** (carteira por
nível de risco AA–H) is NOT exposed via OData (verified — 0 rows every quarter/tipo), but **Rel. 11
(Pessoa Física)** and **Rel. 13 (Pessoa Jurídica)** carry a maturity breakdown that includes a
**"Vencido a Partir de 15 Dias"** overdue bucket + a portfolio Total. So NPL is computed as:

    inadimplência = Σ "Vencido a Partir de 15 Dias" ÷ Σ "Total da Carteira …"   (PF + PJ)

**Honesty:** this is delinquency **15+ dias** — broader than the 90+ "official" inadimplência, so it
runs a little higher — but it is consistent across every institution, and the **PF vs PJ split**
(consumer credit is structurally riskier than corporate) is itself a signal. Quarterly `as-of`;
durable `inadimplencia/index.json`; joined onto the CRO's competitor-soundness rows.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable

from src.ingest.bcb_soundness import (_PRUDENCIAL_SUFFIX, _get, fetch_institution_names,
                                      latest_base_date)

BASE = "https://olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1/odata"
INDEX_KEY = "inadimplencia/index.json"
PUBLIC_URL = "https://www3.bcb.gov.br/ifdata/"
REL_PF, REL_PJ, TIPO = "11", "13", 1
_OVERDUE = "Vencido a Partir de 15 Dias"
_TOTAL_PF = "Total da Carteira de Pessoa Física"
_TOTAL_PJ = "Total da Carteira de Pessoa Jurídica"


def _fetch(base_date: int, rel: str) -> list[dict[str, Any]]:
    return _get(f"{BASE}/IfDataValores(AnoMes=@A,TipoInstituicao=@T,Relatorio=@R)"
                f"?@A={base_date}&@T={TIPO}&@R='{rel}'&$format=json")


def _sum_by_code(rows: list[dict[str, Any]], overdue_col: str, total_col: str) -> dict[str, dict[str, float]]:
    """Per CodInst: summed overdue (15+d) and total carteira across all modalidade rows."""
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        c = r.get("CodInst")
        nome = (r.get("NomeColuna") or "").strip()
        val = r.get("Saldo")
        if not c or val is None:
            continue
        acc = out.setdefault(c, {"overdue": 0.0, "total": 0.0})
        if nome == overdue_col:
            acc["overdue"] += float(val)
        elif nome == total_col:
            acc["total"] += float(val)
    return out


def compute_npl(base_date: int) -> dict[str, dict[str, Any]]:
    """{CodInst: {npl_pf, npl_pj, npl_total, carteira_bi}} (percent), from Rel. 11 + Rel. 13."""
    pf = _sum_by_code(_fetch(base_date, REL_PF), _OVERDUE, _TOTAL_PF)
    pj = _sum_by_code(_fetch(base_date, REL_PJ), _OVERDUE, _TOTAL_PJ)
    out: dict[str, dict[str, Any]] = {}
    for code in set(pf) | set(pj):
        a, b = pf.get(code, {}), pj.get(code, {})
        v_pf, t_pf = a.get("overdue", 0.0), a.get("total", 0.0)
        v_pj, t_pj = b.get("overdue", 0.0), b.get("total", 0.0)
        tot = t_pf + t_pj
        if tot <= 0:
            continue
        def pct(v, t): return round(100 * v / t, 2) if t else None
        out[code] = {"npl_pf": pct(v_pf, t_pf), "npl_pj": pct(v_pj, t_pj),
                     "npl_total": pct(v_pf + v_pj, tot), "carteira_bi": round(tot / 1e9, 2)}
    return out


def npl_band(npl_total: float | None) -> str | None:
    if npl_total is None:
        return None
    if npl_total >= 7.0:
        return "elevada"
    if npl_total >= 4.0:
        return "atenção"
    return "baixa"


def map_to_entities(npl: dict[str, dict[str, Any]], names: dict[str, str], *,
                    resolver: Callable[[dict[str, Any]], list[str]], base_date: int,
                    today: dt.date | None = None) -> list[dict[str, Any]]:
    today = today or dt.date.today()
    best: dict[str, dict[str, Any]] = {}
    for code, m in npl.items():
        raw = names.get(code)
        if not raw or not _PRUDENCIAL_SUFFIX.search(raw) or m.get("npl_total") is None:
            continue
        brand = _PRUDENCIAL_SUFFIX.sub("", raw).strip()
        try:
            ents = resolver({"source": "News", "title": brand, "institution": brand}) or []
        except Exception:  # pragma: no cover
            ents = []
        if not ents:
            continue
        rec = {"cod_inst": code, "band": npl_band(m["npl_total"]), "base_date": base_date,
               "date": today.isoformat(), **m}
        for eid in ents:
            prev = best.get(eid)
            if prev is None or (m.get("carteira_bi") or 0) > (prev.get("carteira_bi") or 0):
                best[eid] = {"id": f"bcb-inadimplencia:{eid}", "entity": eid, **rec}
    return list(best.values())


def merge(existing: dict[str, Any] | None, records: list[dict[str, Any]], *,
          today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.date.today()
    store = dict((existing or {}).get("records") or {})
    for r in records:
        if r.get("entity"):
            store[r["entity"]] = r
    base_date = next((r.get("base_date") for r in records if r.get("base_date")),
                     (existing or {}).get("base_date"))
    return {"as_of": today.isoformat(), "base_date": base_date, "count": len(store), "records": store}


def npl_by_entity(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    _keys = ("npl_total", "npl_pf", "npl_pj", "band", "carteira_bi", "base_date")
    return {eid: {k: r.get(k) for k in _keys}
            for eid, r in ((index or {}).get("records") or {}).items() if r.get("npl_total") is not None}


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
    base_date = latest_base_date()
    npl = compute_npl(base_date)
    names = fetch_institution_names(base_date)
    recs = map_to_entities(npl, names, resolver=resolve_entities, base_date=base_date, today=today)
    if bucket and recs:
        publish(merge(load_index(bucket, s3=s3), recs, today=today), bucket, s3=s3)
    return {"status": "ok", "base_date": base_date, "institutions": len(npl), "mapped": len(recs)}


if __name__ == "__main__":
    d = latest_base_date()
    names = fetch_institution_names(d)
    recs = map_to_entities(compute_npl(d), names, resolver=lambda i: [i["institution"]], base_date=d)
    for r in sorted(recs, key=lambda r: r.get("npl_total") or -1, reverse=True)[:15]:
        print(f"  NPL {r['npl_total']!s:>6}% [{r['band']!s:8}] PF {r['npl_pf']!s:>5} PJ {r['npl_pj']!s:>5}  {r['entity'][:34]}")
