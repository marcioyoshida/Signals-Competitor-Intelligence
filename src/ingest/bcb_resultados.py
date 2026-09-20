"""ADR 022 Tier-3 — operating efficiency + cost of credit from the balancete P&L (doc 4010).

Two ratios, both from the monthly balancete, both YTD flows annualised by ×(12 / month-of-year)
so they compare with a point-in-time stock:

1. **Cost intensity** = `(-) Despesas Administrativas` (COSIF **8170000004**, which rolls up
   pessoal+admin) ÷ `Ativo Realizável` (**1000000009**). The CSO's efficiency read.
2. **Custo de crédito** (#146) = net PDD ÷ `Operações de Crédito` (**1600000007**), where
   net PDD = `(-) DESPESAS DE PROVISÃO PARA RISCO DE CRÉDITO` (**8199200005**) minus
   `REVERSÃO DE PROVISÃO PARA RISCO DE CRÉDITO` (**7199200006**). The CRO's read.

**#146 — what was wrong here before, and how the numbers were checked.**

This docstring used to say a textbook cost-to-income needs the DRE, "that's the DRE, doc 4016",
and that custo de crédito came out "~2× known figures". Measured 2026-09-19 against the real
files, both statements were off:

- **Doc 4016 is not the DRE.** The monthly COSIF file carries docs 4010 *and* 4016, and the
  result accounts (groups 7 receitas / 8 despesas) appear **only under 4010** — 4016 is the
  analytic balance sheet and has strictly *fewer* result accounts. The bytes this module
  already downloads were never missing the P&L.
- **The ~2× was the GROSS figure.** `8199200005` alone is provision *turnover*, not the
  economic charge: for BB it reads R$109.75bn in January alone against a PDD stock of
  R$5.62bn. Netting the group-7 reversão against it is what makes it an economic charge,
  and that nets correctly. Validated against each institution's own **filed** CVM ITR
  figure (`3.04.01 Despesa de Provisão para Perda Esperada para Risco de Crédito`) for the
  identical span, H1-2026:

      institution        COSIF net    CVM filed    ratio
      Banco do Brasil      35.75bn      34.40bn     1.04
      Bradesco             17.78bn      16.92bn     1.05
      Santander            10.36bn      12.73bn     0.81
      Banrisul              0.93bn       0.79bn     1.19

  Four of four comparable institutions inside ±20%, median ~1.05. (Itaú and Mercantil are
  excluded from the check, not failures of it: CVM carries the *holding*'s consolidated DRE
  under a layout where 3.04.01 is absent, so there is nothing to compare against.)

**Cost-to-income is still NOT shipped, and now there is a measurement saying why.** COSIF's
`7100000006 RECEITAS OPERACIONAIS` is gross: for BB H1-2026 it reads R$345.7bn against the
R$160.7bn the same bank filed with CVM as `3.01 Receitas de Intermediação Financeira` for the
identical span — 2.15×, because group 7 accumulates trading and FX flows CVM nets. A
cost-to-income built on that denominator would be wrong by roughly half and would look
entirely plausible. The real inputs exist in CVM's DRE sub-accounts (see #92), which #145 now
ingests; they do not exist in 4010.
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
# #146 — cost of credit. The despesa alone is provision TURNOVER; only the netted pair is an
# economic charge. Never surface `_PDD_DESP` on its own.
_PDD_DESP = "8199200005"  # (-) DESPESAS DE PROVISÃO PARA RISCO DE CRÉDITO, YTD flow
_PDD_REV = "7199200006"   # REVERSÃO DE PROVISÃO PARA RISCO DE CRÉDITO, YTD flow
_CREDITO = "1600000007"   # Operações de Crédito (loan book), stock
_OUTROS = "1800000003"    # OUTROS CRÉDITOS — where card receivables sit, stock
_WANT = frozenset({_OPEX, _ATIVO, _PDD_DESP, _PDD_REV, _CREDITO, _OUTROS})

# #146 — the ratio is only meaningful for an institution that is materially a LENDER.
# Custody, clearing and broker-dealer banks carry a group-level provision flow against a loan
# book of ~zero, and the quotient is noise wearing a percent sign: on the first live run
# Citibank N.A. (credito R$0.0bn) came out at -30.04%, BOFA Merrill Lynch (R$0.7bn against
# R$40.5bn of assets) at +46.84%, Banco BESA at -54.76%.
#
# This is a DENOMINATOR-validity gate: it inspects the loan book, never the computed value,
# so a genuinely extreme cost of credit at a real lender still surfaces (Banco Pan at 10.63%
# and PicPay at 21.38% both survive it).
#
# **What it still cannot do.** There is no threshold that separates BTG (credito/ativo 0.037,
# a plausible 5.53%) from BNP Paribas Brasil (0.036, an implausible 28.23%) — they are
# adjacent on every available measure. The floors below are set to express "materially a
# lender" rather than tuned against the answers, and the cost is real: the investment banks
# (BTG, J.P. Morgan) fall out with them. Losing a metric for an investment bank whose cost of
# credit was never a headline number is the cheaper error.
_MIN_CREDITO = 0.3e9       # R$300m loan book
_MIN_CREDITO_SHARE = 0.05  # and at least 5% of assets

# #149 — and the carteira must actually BE the institution's credit exposure. A card issuer's
# receivables sit in OUTROS CRÉDITOS (1800000003), not Operações de Crédito, so dividing a
# card-sized provision flow by a loan-sized carteira measures the wrong thing. #149's CNPJ
# matching surfaced these institutions for the first time — they had never resolved by name —
# and they arrived reading Afinz 98.08%, Digimais 95.30%, Carrefour/CSF 89.27%, Bradescard
# 47.32%.
#
# The split is clean and it has a reason, so this is a denominator-COMPLETENESS test, still
# never an inspection of the answer. Measured 2026-09-20, outros ÷ crédito:
#
#     implausible:  afinz 1.70  digimais 1.09  csf 3.04  bradescard 4.47
#                   itau-unibanco (the holding) 2.43   original 3.70
#     plausible:    caixa 0.09  pan 0.22  bb 0.44  itau 0.51  inter 0.59  bradesco 0.76
#
# Requiring credit to be the DOMINANT receivable base also retires the holding-vs-institution
# caveat #146 had to document as "stated, not solved".
_MAX_OUTROS_RATIO = 1.0
_DOC = "4010"


def fetch_month(ym: int, *, timeout: int = 120) -> dict[str, dict[str, Any]]:
    """{cnpj8: {name, opex, ativo, pdd_desp, pdd_rev, credito}} in raw R$ for a base month.

    Flows (opex, pdd_*) are absolute magnitudes and year-to-date; stocks (ativo, credito)
    are point-in-time.
    """
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
        if conta not in _WANT:
            continue
        cnpj = row.get("CNPJ")
        v = _num(row.get("SALDO"))
        if not cnpj or v is None:
            continue
        rec = out.setdefault(cnpj, {"name": row.get("NOME_INSTITUICAO")})
        if conta == _ATIVO:
            rec["ativo"] = v
        elif conta == _CREDITO:
            rec["credito"] = v
        elif conta == _OUTROS:
            rec["outros_creditos"] = abs(v)
        else:
            rec[{_OPEX: "opex", _PDD_DESP: "pdd_desp", _PDD_REV: "pdd_rev"}[conta]] = abs(v)
    return out


def map_to_entities(month_data: dict[str, dict[str, Any]], ym: int, *,
                    resolver: Callable[[dict[str, Any]], list[str]],
                    cnpj_resolver: Callable[[Any], str | None] | None = None,
                    today: dt.date | None = None) -> list[dict[str, Any]]:
    """#149 — CNPJ first, name second; see ``bcb_balancete.map_to_entities`` for why."""
    today = today or dt.date.today()
    if cnpj_resolver is None:
        from src.synth import entities as _ent

        cnpj_resolver = _ent.resolve_by_cnpj
    ann = 12.0 / max(1, ym % 100)             # YTD flow → annualised
    best: dict[str, dict[str, Any]] = {}
    for cnpj, rec in month_data.items():
        opex, ativo = rec.get("opex"), rec.get("ativo")
        name = rec.get("name")
        if not name or not ativo:
            continue
        opex_ativo = round(100 * opex * ann / ativo, 2) if opex else None
        # #146 — net PDD only. `pdd_desp` on its own is turnover, so a month that carries
        # the expense but not the reversão account yields None rather than a number 4-25x
        # too large. Both legs or nothing.
        credito, desp, rev = rec.get("credito"), rec.get("pdd_desp"), rec.get("pdd_rev")
        outros = rec.get("outros_creditos") or 0.0
        lends = (
            bool(credito)
            and credito >= _MIN_CREDITO
            and credito >= _MIN_CREDITO_SHARE * ativo
            and outros <= _MAX_OUTROS_RATIO * credito   # credit is the dominant receivable
        )
        custo_credito = (
            round(100 * (desp - rev) * ann / credito, 2)
            if lends and desp is not None and rev is not None else None
        )
        if opex_ativo is None and custo_credito is None:
            continue
        by_cnpj = cnpj_resolver(cnpj)
        if by_cnpj:
            ents = [by_cnpj]
        else:
            try:
                ents = resolver({"source": "News", "title": name, "institution": name}) or []
            except Exception:  # pragma: no cover
                ents = []
        if not ents:
            continue
        row = {"cnpj": cnpj, "name": name, "month": ym,
               "opex_ativo_pct": opex_ativo, "custo_credito_pct": custo_credito,
               "credito_bi": round(credito / 1e9, 1) if credito else None,
               "ativo_bi": round(ativo / 1e9, 1), "date": today.isoformat()}
        for eid in ents:
            prev = best.get(eid)
            if prev is None or (row["ativo_bi"] or 0) > (prev.get("ativo_bi") or 0):
                best[eid] = {"id": f"bcb-resultados:{eid}", "entity": eid, **row}
    return list(best.values())


def merge(existing: dict[str, Any] | None, records: list[dict[str, Any]], *,
          today: dt.date | None = None) -> dict[str, Any]:
    """Upsert this run's records into the store, keyed by entity_id.

    **#151 — the store is keyed by entity, but the institution is the CNPJ.** When entity
    resolution changes which id a CNPJ lands on, a plain upsert leaves the *previous* id's
    record behind forever, and the same balance sheet is then stored twice under two ids.
    #149 switching this ingester from name-first to CNPJ-first did exactly that: the run
    that day emitted 134 records into a store that grew to 147, the 13 extras being the
    name-resolved ids (`abc` vs `abc_brasil`, `morgan` vs `jpmorgan`, …) — 11 CNPJs held
    by more than one entity, 10.8% of the store's total assets counted twice.

    So an incoming record **evicts any other entity holding the same CNPJ**. The store is
    one row per institution, not one row per id that institution was ever called.
    """
    today = today or dt.date.today()
    store = dict((existing or {}).get("records") or {})
    for r in records:
        eid = r.get("entity")
        if not eid:
            continue
        cnpj = r.get("cnpj")
        if cnpj:
            for other, prev in list(store.items()):
                if other != eid and prev.get("cnpj") == cnpj:
                    store.pop(other)
        store[eid] = r
    month = next((r.get("month") for r in records if r.get("month")), (existing or {}).get("month"))
    return {"as_of": today.isoformat(), "month": month, "count": len(store), "records": store}


def resultados_by_entity(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        eid: {"opex_ativo_pct": r.get("opex_ativo_pct"),
              "custo_credito_pct": r.get("custo_credito_pct"), "month": r.get("month")}
        for eid, r in ((index or {}).get("records") or {}).items()
        if r.get("opex_ativo_pct") is not None or r.get("custo_credito_pct") is not None
    }


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
