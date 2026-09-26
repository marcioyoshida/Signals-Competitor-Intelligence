"""CVM financial statements (DFP annual / ITR quarterly) → per-issuer key metrics.

Issue #7 / ADR 011 stage 6; extended by #145 / ADR 028. Fetches the CVM Demonstrações
Financeiras open data (dados.cvm.gov.br), parses the consolidated Balanço (BPA/BPP) and
Resultado (DRE) for LISTED tracked issuers (matched by CNPJ root), and reduces each to a
compact record — assets, equity, revenue, net income — for the latest period AND the prior
one, so the downstream store can compute net margin, YoY revenue growth and leverage.

Store shape (one record per entity), written by ``persist`` to ``financials/index.json``:
    {entity_id: {name, doc, period, period_start, months, prior_period, currency, revenue,
                 net_income, assets, equity, prior_revenue, prior_net_income, net_margin,
                 revenue_growth, leverage, source_url, interim?, as_of}}

#145 — three things the package layout makes easy to get wrong, all verified against the
live datasets on 2026-09-19 rather than assumed:

1. **The package year is the fiscal REFERENCE year, not the filing year.**
   ``dfp_cia_aberta_2025.zip`` carries ``DT_REFER = 2025-12-31`` (438 issuers);
   ``dfp_cia_aberta_2026.zip`` exists but holds 8 issuers — only those whose fiscal year
   already ended in 2026. So ``today.year - 1`` happens to be right for most of the year
   and collapses every January–March, when last year's DFPs have not been filed yet.
   ``latest_year`` therefore probes newest-first and picks by CONTENT (issuer count), not
   by arithmetic on today's date.

2. **One ITR package holds several reference dates.** ``itr_cia_aberta_2026.zip`` carries
   both ``DT_REFER = 2026-03-31`` and ``2026-06-30``. Keying only on
   ``(cnpj, ORDEM_EXERC)`` silently mixes Q1 and Q2 for the same issuer, and ``period``
   ends up whichever row the CSV happened to yield first. ``parse_statements`` resolves
   each issuer's newest ``DT_REFER`` first and discards the rest.

3. **One ITR DRE holds several period SPANS.** At ``DT_REFER = 2026-06-30`` the DRE
   carries both the quarter (``2026-04-01 → 06-30``) and the year-to-date
   (``2026-01-01 → 06-30``). Picking "the first row with CD_CONTA 3.01" yields a 3-month
   revenue for one issuer and a 6-month revenue for the next. We take the **longest span**
   — which is the YTD figure for calendar filers and stays correct for the handful of
   non-calendar fiscal years — and record ``months`` on the record so no consumer has to
   guess what the number covers.

**Why ITR does not overwrite DFP.** ``product_intel.market_structure`` sums ``revenue``
across entities for market size / HHI / leader share, and ``bcg.position_from_financials``
uses it as the size axis. A 6-month ITR revenue sitting in the same field as a 12-month
DFP revenue would corrupt both silently. So the top-level fields stay **annual** and the
quarterly read lands in a separate ``interim`` block (``merge_interim``). #145 asked for
ITR to supersede DFP on ``period``; that is the one part of the ticket not followed, and
this is why.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re as _re
import zipfile
from typing import Any

import requests

BASE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC"
DFP_URL = BASE + "/DFP/DADOS/dfp_cia_aberta_{year}.zip"
ITR_URL = BASE + "/ITR/DADOS/itr_cia_aberta_{year}.zip"
DATASET_URL = "https://dados.cvm.gov.br/dataset/cia_aberta-doc-dfp"

# CVM standardized account codes (consolidated statements).
_CD_ASSETS = "1"        # BPA: Ativo Total
_CD_EQUITY = "2.03"     # BPP: Patrimônio Líquido — the NON-FINANCIAL layout only (see below)
_CD_REVENUE = "3.01"    # DRE: top line (Receita de Venda / Receitas da Intermediação /
                        # Receitas das Atividades Seguradoras — 438/438 issuers, FY2025)

# #145 — equity CANNOT be read from a fixed code. CVM uses a different BPP layout for
# financial institutions, and in it 2.03 is *Provisões*, not Patrimônio Líquido. Measured
# over dfp_cia_aberta_2025 (438 consolidated issuers): PL sits at 2.03 for 428, at 2.07 for
# 9 and at 2.08 for 4 — and the 13 exceptions are the banks, i.e. precisely Onça's tracked
# population. Banco do Brasil was being stored with equity R$38.7bn (its provisions) instead
# of R$193.6bn, which made `leverage` ~5x too high and silently wrong on every bank card.
# Matching the top-level group-2 account by LABEL resolves 438/438 with exactly one hit
# each, so that is the rule; the fixed code survives only as a defensive fallback.
_RE_TOP_LIABILITY = _re.compile(r"^2\.\d+$")
_EQUITY_LABEL = "PATRIMONIO LIQUIDO"

# Fund vehicles. They are tracked entities with tickers, but they do not file a cia-aberta
# DFP under their sponsor's name — see the name-fallback guard in ``build_index``.
_VEHICLE_INDUSTRIES = frozenset({"real-estate-funds", "agri-funds"})


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


# #92 — cost-to-income for the BANK DRE layout. Codes cannot be pinned: measured over
# itr_cia_aberta_2026 (12 bank-layout issuers) two layouts coexist and every 3.04.xx line
# shifts by one between them (e.g. pessoal is 3.04.02 in one and 3.04.03 in the other), so
# the lines are matched by LABEL, one level under their parent. Definition, stated rather
# than reconciled to each bank's own "índice de eficiência" (every bank publishes a
# different one): (pessoal + outras administrativas) / (resultado bruto de intermediação
# ANTES da PDD + receitas de serviços). The PDD add-back matters for the layout that books
# the provision inside 3.02 (Itaú, BTG, Mercantil) — without it their margin is post-loss
# and the ratio overstates. Why not COSIF group 7: it is gross of trading/FX flows CVM nets
# (BB H1-2026: R$345.7bn vs R$160.7bn filed), which would halve the ratio.
_RE_BANK_TOP = _re.compile(r"INTERMEDIA")
_RE_PDD = _re.compile(r"PROVIS|PERDA.*CREDITO|CREDITO ESPERADA")
_RE_SERVICES = _re.compile(r"^RECEITAS? (DE|COM) PRESTACAO DE SERVICOS")
_RE_PERSONNEL = _re.compile(r"^DESPESAS? (DE|COM) PESSOAL")
_RE_ADMIN = _re.compile(r"^OUTRAS DESPESAS (DE )?ADMINISTRATIVAS")


def _child(code: str, parent: str) -> bool:
    return code.startswith(parent + ".") and code.count(".") == parent.count(".") + 1


def _bank_efficiency(rows: list[dict[str, Any]]) -> dict[str, float] | None:
    """Cost-to-income from one DRE span of a BANK-layout issuer; None for any other layout
    or when a required line is missing (never a partial ratio)."""
    by_code = {str(r.get("CD_CONTA") or ""): r for r in rows}
    top = by_code.get("3.01")
    if top is None or not _RE_BANK_TOP.search(_fold(top.get("DS_CONTA"))):
        return None

    def _v(r: dict[str, Any]) -> float:
        return (_num(r.get("VL_CONTA")) or 0.0) * _scale(r)

    def _lines(parent: str, rx) -> list[dict[str, Any]]:
        return [r for c, r in by_code.items() if _child(c, parent) and rx.search(_fold(r.get("DS_CONTA")))]

    gross, personnel, admin = by_code.get("3.03"), _lines("3.04", _RE_PERSONNEL), _lines("3.04", _RE_ADMIN)
    if gross is None or len(personnel) != 1 or len(admin) != 1:
        return None
    income = _v(gross) - sum(_v(r) for r in _lines("3.02", _RE_PDD)) \
        + sum(_v(r) for r in _lines("3.04", _RE_SERVICES))
    cost = -(_v(personnel[0]) + _v(admin[0]))
    if income <= 0 or cost <= 0:
        return None
    return {"cost_to_income": round(cost / income, 4), "admin_cost": cost, "operating_income": income}


def _parse_shares(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """#93 — ``composicao_capital``: shares outstanding (paid-in minus treasury) per class,
    newest DT_REFER per issuer. RAW counts: CVM carries no unit column and some issuers file
    in thousands (Itaú: 11,026,869 for ~11bn shares) — the scale is resolved downstream in
    ``src.synth.valuation`` against price and book value, never guessed here."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        cnpj, refer = _root8(r.get("CNPJ_CIA")), _iso(r.get("DT_REFER"))
        if not cnpj or refer < (out.get(cnpj) or {}).get("shares_as_of", ""):
            continue
        on = (_num(r.get("QT_ACAO_ORDIN_CAP_INTEGR")) or 0) - (_num(r.get("QT_ACAO_ORDIN_TESOURO")) or 0)
        pn = (_num(r.get("QT_ACAO_PREF_CAP_INTEGR")) or 0) - (_num(r.get("QT_ACAO_PREF_TESOURO")) or 0)
        if on + pn > 0:
            out[cnpj] = {"shares_on": on, "shares_pn": pn, "shares_as_of": refer}
    return out


def _iso(v: Any) -> str:
    return str(v or "")[:10]


def _span_days(start: str, end: str) -> int:
    """Length of an ISO date span in days; 0 when either end is unparseable."""
    try:
        return (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days
    except ValueError:
        return 0


def _months(start: str, end: str) -> int | None:
    """Span rounded to whole months (12 for an annual DFP, 3/6/9/12 for an ITR)."""
    days = _span_days(start, end)
    if days <= 0:
        return None
    return max(1, round(days / 30.44))


def parse_statements(zf: zipfile.ZipFile, *, doc: str = "DFP") -> dict[str, dict[str, Any]]:
    """Parse an open CVM package into
    ``{cnpj_root: {ordem: {assets, equity, revenue, net_income, period, period_start,
    months, doc, name, cd_cvm}}}`` where ordem is 'ÚLTIMO' (current) / 'PENÚLTIMO' (prior).

    Only each issuer's **newest DT_REFER** survives, and within it the DRE's **longest
    period span** — see the module docstring for why both matter (they are the two ways an
    ITR package silently mixes quarters).
    """
    doc = doc.upper()
    members = {n: _open_csv(zf, n) for n in ("BPA_con", "BPP_con", "DRE_con")}
    # An issuer with no consolidated set files ONLY individual statements (Banco ABC
    # Brasil, live 2026-09-26) — then the individual set IS the entity's statement. The
    # fallback is per issuer: anyone with any consolidated row never reads `_ind`.
    has_con = {_root8(r.get("CNPJ_CIA")) for rows in members.values() for r in rows}
    ind_only: set[str] = set()
    for n in ("BPA", "BPP", "DRE"):
        extra = [r for r in _open_csv(zf, f"{n}_ind") if _root8(r.get("CNPJ_CIA")) not in has_con]
        ind_only |= {_root8(r.get("CNPJ_CIA")) for r in extra}
        members[f"{n}_con"] = members[f"{n}_con"] + extra

    # Pass 1 — newest reference date per issuer, across every statement member.
    newest: dict[str, str] = {}
    for rows in members.values():
        for row in rows:
            cnpj, refer = _root8(row.get("CNPJ_CIA")), _iso(row.get("DT_REFER"))
            if cnpj and refer > newest.get(cnpj, ""):
                newest[cnpj] = refer

    out: dict[str, dict[str, Any]] = {}

    def _keep(row: dict[str, Any]) -> tuple[str, str] | None:
        cnpj, ordem = _root8(row.get("CNPJ_CIA")), (row.get("ORDEM_EXERC") or "").upper()
        if not cnpj or not ordem or _iso(row.get("DT_REFER")) != newest.get(cnpj):
            return None
        return cnpj, ordem

    def _ensure(cnpj: str, ordem: str, row: dict[str, Any]) -> dict[str, Any]:
        return out.setdefault(cnpj, {}).setdefault(ordem, {
            "period": _iso(row.get("DT_FIM_EXERC") or row.get("DT_REFER")),
            "name": row.get("DENOM_CIA"), "cd_cvm": row.get("CD_CVM"), "doc": doc,
        })

    # Balance sheet — a stock, so no span to choose.
    for row in members["BPA_con"]:                       # assets: one stable code
        if row.get("CD_CONTA") != _CD_ASSETS:
            continue
        key, val = _keep(row), _num(row.get("VL_CONTA"))
        if key and val is not None:
            _ensure(*key, row)["assets"] = val * _scale(row)

    equity_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in members["BPP_con"]:                       # equity: by label, not by code
        key = _keep(row)
        if key and _RE_TOP_LIABILITY.match(str(row.get("CD_CONTA") or "")):
            equity_rows.setdefault(key, []).append(row)
    for key, rows in equity_rows.items():
        pick = next((r for r in rows if _fold(r.get("DS_CONTA")).startswith(_EQUITY_LABEL)), None)
        pick = pick or next((r for r in rows if r.get("CD_CONTA") == _CD_EQUITY), None)
        val = _num(pick.get("VL_CONTA")) if pick else None
        if val is not None:
            _ensure(*key, pick)["equity"] = val * _scale(pick)

    # Income statement — a flow, so the span is a real choice (see docstring #3).
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in members["DRE_con"]:
        key = _keep(row)
        if key:
            by_key.setdefault(key, []).append(row)

    for (cnpj, ordem), rows in by_key.items():
        spans: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in rows:
            spans.setdefault((_iso(r.get("DT_INI_EXERC")), _iso(r.get("DT_FIM_EXERC"))), []).append(r)
        # Longest span = YTD for calendar filers, and still correct for the handful of
        # non-calendar fiscal years where "since 1 January" is not the accumulation start.
        (ini, fim), rows = max(spans.items(), key=lambda kv: (_span_days(*kv[0]), kv[0][1]))

        rev = next((r for r in rows if r.get("CD_CONTA") == _CD_REVENUE), None)
        ni = _pick_net_income(rows)
        if rev is None and ni is None:
            continue
        rec = _ensure(cnpj, ordem, rows[0])
        rec["period_start"], rec["months"] = ini, _months(ini, fim)
        if fim:
            rec["period"] = fim
        if rev is not None and _num(rev.get("VL_CONTA")) is not None:
            rec["revenue"] = _num(rev["VL_CONTA"]) * _scale(rev)
        if ni is not None and _num(ni.get("VL_CONTA")) is not None:
            rec["net_income"] = _num(ni["VL_CONTA"]) * _scale(ni)
        eff = _bank_efficiency(rows)
        if eff:
            rec.update(eff)

    for cnpj in ind_only & out.keys():
        for rec in out[cnpj].values():
            rec["basis"] = "individual"
    for cnpj, sh in _parse_shares(_open_csv(zf, "composicao_capital")).items():
        if cnpj in out and sh["shares_as_of"] == newest.get(cnpj):
            out[cnpj].setdefault("ÚLTIMO", {"period": sh["shares_as_of"], "doc": doc}).update(sh)
    return out


def fetch_statements(year: int, *, doc: str = "DFP", timeout: int = 90) -> dict[str, dict[str, Any]]:
    """Download and parse one package year. Best-effort ({} on any network/format error)."""
    url = (DFP_URL if doc.upper() == "DFP" else ITR_URL).format(year=year)
    try:
        raw = requests.get(url, timeout=timeout).content
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except Exception:  # pragma: no cover - network/best-effort
        return {}
    return parse_statements(zf, doc=doc)


def latest_statements(
    *, doc: str = "DFP", today: dt.date | None = None, back: int = 3,
    min_issuers: int = 50, fetcher: Any = None,
) -> tuple[int | None, dict[str, dict[str, Any]]]:
    """Newest package year that actually carries filings, chosen by **content**.

    Probes candidate years newest-first and returns the first whose parsed package holds at
    least ``min_issuers`` issuers, as ``(year, statements)`` so the caller never downloads
    twice. ``(None, {})`` when nothing qualifies — the caller must not persist in that case
    or it would overwrite a good store with an empty one.

    The floor exists because a package for a year still in progress is not empty, it is
    *sparse*: on 2026-09-19 ``dfp_cia_aberta_2026.zip`` parsed to 8 issuers against 438 for
    2025. A truthiness check would have happily taken the 8.
    """
    fetcher = fetcher or fetch_statements
    year0 = (today or dt.date.today()).year
    for year in range(year0, year0 - max(1, back), -1):
        stmts = fetcher(year, doc=doc)
        if len(stmts) >= min_issuers:
            return year, stmts
    return None, {}


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
    # #145 — the name fallback must not reach fund vehicles. A FII's alias carries its
    # SPONSOR's brand, so resolving an issuer name lands on the fund: live on 2026-09-19
    # this attributed Cyrela Brazil Realty's R$9.4bn revenue to CYCR11, Eldorado Celulose
    # to ELDO11 and Banrisul's R$163.9bn of assets to FISP11. A fund that genuinely files
    # is unaffected — its own CNPJ matches at priority 2, which this does not touch.
    tickered = {
        e["entity_id"] for e in entities
        if e.get("ticker") and not (_VEHICLE_INDUSTRIES & set(e.get("industries") or []))
    }

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
        # #145: only compare flows that cover the same number of months. CVM normally pairs
        # like with like, but an issuer that changed its fiscal year (or filed a stub
        # period) will hand us a 12-month ÚLTIMO against a 9-month PENÚLTIMO, and the
        # resulting "growth" is an artefact of the calendar. None beats a plausible lie.
        comparable = (
            cur.get("months") is None and prior.get("months") is None
        ) or cur.get("months") == prior.get("months")
        rec = {
            "entity_id": eid, "name": cur.get("name"), "doc": cur.get("doc"),
            "period": cur.get("period"), "period_start": cur.get("period_start"),
            "months": cur.get("months"),
            "prior_period": prior.get("period"), "currency": "BRL",
            "revenue": rev, "net_income": ni, "assets": assets, "equity": equity,
            "prior_revenue": prev_rev, "prior_net_income": prior.get("net_income"),
            "net_margin": round(ni / rev, 4) if rev and ni is not None and rev != 0 else None,
            "revenue_growth": round((rev - prev_rev) / prev_rev, 4)
            if comparable and rev is not None and prev_rev not in (None, 0) else None,
            "leverage": round((assets - equity) / equity, 3)
            if assets is not None and equity not in (None, 0) else None,
            # #92 — bank layout only (None elsewhere); a ratio of two same-span flows, so
            # an ITR's 6-month read is directly comparable with a DFP's 12-month one.
            "cost_to_income": cur.get("cost_to_income"),
            # #93 — raw CVM counts; see _parse_shares for why the unit is not trusted here.
            "shares_on": cur.get("shares_on"), "shares_pn": cur.get("shares_pn"),
            "shares_as_of": cur.get("shares_as_of"),
            "basis": cur.get("basis") or "consolidated",
            "source_url": source_url,
        }
        index[eid] = rec
    return index


_INTERIM_FIELDS = ("doc", "period", "period_start", "months", "prior_period", "revenue",
                   "net_income", "assets", "equity", "prior_revenue", "prior_net_income",
                   "net_margin", "revenue_growth", "leverage", "cost_to_income",
                   "shares_on", "shares_pn", "shares_as_of", "basis")


def merge_interim(
    annual: dict[str, dict[str, Any]], interim: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Attach each entity's ITR read as an ``interim`` block beside the annual figures.

    The top-level fields stay annual on purpose — see the module docstring. An ITR read
    older than the annual one is dropped rather than shown: a Q3-2025 quarter next to an
    FY2025 annual is not "fresher", it is the same year with less of it.

    Entities with an ITR but no DFP are kept with **null** annual fields. That is the
    honest shape: the quarterly figure is real and citable, and the consumers that compare
    revenue across entities (`market_structure`, `bcg`) already skip a falsy revenue, so a
    6-month number can never leak into a market-size sum.
    """
    out = {eid: dict(rec) for eid, rec in annual.items()}
    for eid, rec in interim.items():
        base = out.get(eid)
        if base and str(rec.get("period") or "") <= str(base.get("period") or ""):
            continue
        if base is None:
            base = out[eid] = {
                "entity_id": eid, "name": rec.get("name"), "currency": "BRL",
                "source_url": rec.get("source_url"),
                **{k: None for k in _INTERIM_FIELDS},
            }
        base["interim"] = {k: rec.get(k) for k in _INTERIM_FIELDS}
    return out


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
    return dt.date.today().isoformat()


def run(bucket: str | None, *, year: int | None = None, min_issuers: int = 50,
        fetcher: Any = None) -> dict[str, Any]:
    """#145 — refresh ``financials/index.json`` from the newest published DFP **and** ITR.

    Returns a summary dict; persists only when the merged index is non-empty, so a bad
    fetch degrades to "last month's store" rather than to an empty one.
    """
    from src.synth import entities as _ent
    from src.synth import entity_registry as _er

    fetcher = fetcher or fetch_statements
    ents = list(_er.list_entities(include_inactive=True))

    def _pick(doc: str) -> tuple[int | None, dict[str, dict[str, Any]]]:
        if year is not None:
            return year, fetcher(year, doc=doc)
        return latest_statements(doc=doc, min_issuers=min_issuers, fetcher=fetcher)

    dfp_year, dfp = _pick("DFP")
    itr_year, itr = _pick("ITR")

    annual = build_index(ents, dfp, resolver=_ent.resolve_entities) if dfp else {}
    quarterly = build_index(ents, itr, resolver=_ent.resolve_entities) if itr else {}
    index = merge_interim(annual, quarterly)

    summary = {
        "dfp_year": dfp_year, "dfp_issuers": len(dfp), "annual_matched": len(annual),
        "itr_year": itr_year, "itr_issuers": len(itr), "interim_matched": len(quarterly),
        "records": len(index),
        "periods": sorted({str(r.get("period")) for r in annual.values() if r.get("period")}),
    }
    if index and bucket:
        summary["s3"] = persist(bucket, index)
    else:
        # Loud, not silent: an empty index here means both packages failed or nothing
        # resolved, and overwriting a good store with {} is the one unrecoverable move.
        summary["skipped_persist"] = True
    return summary


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    """OncaFinancialsPipeline CvmStatementsTask (#145). ``{"year": 2025}`` pins a package."""
    import os

    ev = event or {}
    year = ev.get("year") or os.environ.get("ONCA_FINANCIALS_YEAR")
    return {"statusCode": 200, "body": json.dumps(
        run(os.environ.get("ONCA_DIGESTS_BUCKET"), year=int(year) if year else None),
        ensure_ascii=False)}
