"""ADR 022 Tier-1 (addendum) — competitor financial fundamentals: profitability, leverage, funding.

The CSO's financial-strength axis (today CSO is narrative/momentum only). One IF.data fetch —
**Relatório 1 "Resumo" under TipoInstituicao=1** — carries every input: Ativo Total, Carteira de
Crédito, Captações, **Lucro Líquido**, Patrimônio Líquido, Índice de Basileia. From these we derive
per tracked entity (all labelled **inference**):

    ROE / ROA (annualised, YTD-aware) · alavancagem (Ativo/PL) · crédito÷captações (funding
    intensity) · headroom de capital (Basileia − 10.5pp mínimo) · share de crédito / de lucro.

**YTD caveat.** IF.data `Lucro Líquido` is the profit ACCUMULATED in the year to the base date, so
ROE/ROA are annualised by ×(12 / month-of-quarter) and labelled as annualised inference.

Durable `fundamentals/index.json`; joined by `feed_builder` into `entities[].fundamentals`.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable

import requests

from src.ingest.bcb_soundness import (_PRUDENCIAL_SUFFIX, _get, fetch_institution_names,
                                      latest_base_date)

BASE = "https://olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1/odata"
INDEX_KEY = "fundamentals/index.json"
PUBLIC_URL = "https://www3.bcb.gov.br/ifdata/"
RELATORIO_RESUMO = "1"
TIPO_INSTITUICAO = 1

# canonical line -> NomeColuna prefix (exact enough to avoid 'Patrimônio de Referência' collision).
LINE_COLUMNS: dict[str, str] = {
    "ativo": "Ativo Total",
    "carteira": "Carteira de Crédito",
    "captacoes": "Captações",
    "lucro": "Lucro Líquido",
    "pl": "Patrimônio Líquido",
    "basileia": "Índice de Basileia",
}


def _resumo_url(base_date: int) -> str:
    return (f"{BASE}/IfDataValores(AnoMes=@A,TipoInstituicao=@T,Relatorio=@R)"
            f"?@A={base_date}&@T={TIPO_INSTITUICAO}&@R='{RELATORIO_RESUMO}'&$format=json")


def fetch_resumo(base_date: int) -> list[dict[str, Any]]:
    return _get(_resumo_url(base_date))


def _match(nome: str | None) -> str | None:
    n = (nome or "").strip()
    for key, pref in LINE_COLUMNS.items():
        if n.startswith(pref):
            return key
    return None


def extract_lines(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """{CodInst: {line: value}} in raw R$ (índice de basileia stays a fraction)."""
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        code = r.get("CodInst")
        line = _match(r.get("NomeColuna"))
        if not code or not line or r.get("Saldo") is None:
            continue
        # first (shortest-name) hit wins for a line
        if line not in out.setdefault(code, {}):
            out[code][line] = float(r["Saldo"])
    return out


def _ratios(v: dict[str, float], base_date: int, *, carteira_total: float, lucro_total: float) -> dict[str, Any]:
    ytd_months = max(1, base_date % 100)              # 202603 -> 3 (YTD months in the year)
    ann = 12.0 / ytd_months
    ativo, carteira, capt = v.get("ativo"), v.get("carteira"), v.get("captacoes")
    lucro, pl, bas = v.get("lucro"), v.get("pl"), v.get("basileia")
    def pct(a, b): return round(100 * a / b, 2) if a is not None and b else None
    return {
        "ativo_bi": round(ativo / 1e9, 2) if ativo is not None else None,
        "carteira_bi": round(carteira / 1e9, 2) if carteira is not None else None,
        "lucro_bi": round(lucro / 1e9, 3) if lucro is not None else None,
        "pl_bi": round(pl / 1e9, 2) if pl is not None else None,
        "roe_pct": round(100 * lucro / pl * ann, 1) if lucro is not None and pl else None,     # annualised
        "roa_pct": round(100 * lucro / ativo * ann, 2) if lucro is not None and ativo else None,
        "leverage": round(ativo / pl, 1) if ativo is not None and pl else None,
        "credito_captacoes_pct": pct(carteira, capt),
        "basileia_headroom_pp": round(bas * 100 - 10.5, 2) if bas is not None else None,
        "carteira_share_pct": pct(carteira, carteira_total),
        "lucro_share_pct": pct(lucro, lucro_total),
        "base_date": base_date,
    }


def map_to_entities(lines: dict[str, dict[str, float]], names: dict[str, str], *,
                    resolver: Callable[[dict[str, Any]], list[str]], base_date: int,
                    today: dt.date | None = None) -> list[dict[str, Any]]:
    today = today or dt.date.today()
    carteira_total = sum(v["carteira"] for v in lines.values() if v.get("carteira", 0) > 0) or 1.0
    lucro_total = sum(v["lucro"] for v in lines.values() if v.get("lucro", 0) > 0) or 1.0
    best: dict[str, dict[str, Any]] = {}
    for code, v in lines.items():
        raw = names.get(code)
        if not raw or not _PRUDENCIAL_SUFFIX.search(raw) or v.get("pl") is None:
            continue
        brand = _PRUDENCIAL_SUFFIX.sub("", raw).strip()
        try:
            ents = resolver({"source": "News", "title": brand, "institution": brand}) or []
        except Exception:  # pragma: no cover
            ents = []
        if not ents:
            continue
        rec = _ratios(v, base_date, carteira_total=carteira_total, lucro_total=lucro_total)
        for eid in ents:
            prev = best.get(eid)
            if prev is None or (rec.get("ativo_bi") or 0) > (prev.get("ativo_bi") or 0):
                best[eid] = {"id": f"bcb-fundamentals:{eid}", "entity": eid,
                             "cod_inst": code, "date": today.isoformat(), **rec}
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


def fundamentals_by_entity(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    _keys = ("roe_pct", "roa_pct", "leverage", "credito_captacoes_pct", "basileia_headroom_pp",
             "carteira_share_pct", "lucro_share_pct", "ativo_bi", "lucro_bi", "base_date")
    return {eid: {k: r.get(k) for k in _keys}
            for eid, r in ((index or {}).get("records") or {}).items() if r.get("roe_pct") is not None}


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
    rows = fetch_resumo(base_date)
    names = fetch_institution_names(base_date)
    recs = map_to_entities(extract_lines(rows), names, resolver=resolve_entities,
                           base_date=base_date, today=today)
    if bucket and recs:
        publish(merge(load_index(bucket, s3=s3), recs, today=today), bucket, s3=s3)
    return {"status": "ok", "base_date": base_date, "rows": len(rows), "mapped": len(recs)}


if __name__ == "__main__":
    d = latest_base_date()
    names = fetch_institution_names(d)
    recs = map_to_entities(extract_lines(fetch_resumo(d)), names, resolver=lambda i: [i["institution"]],
                           base_date=d)
    for r in sorted(recs, key=lambda r: r.get("roe_pct") or -999, reverse=True)[:12]:
        print(f"  ROE {r.get('roe_pct')!s:>6}%  ROA {r.get('roa_pct')!s:>5}  alav {r.get('leverage')!s:>5}x  "
              f"headroom {r.get('basileia_headroom_pp')!s:>5}pp  {r['entity'][:34]}")
