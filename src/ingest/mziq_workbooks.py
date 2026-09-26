"""Issuer-published IR workbooks (MZiQ "Séries Históricas") → per-issuer KPIs (#152).

The narrow residue of the #147 spike: metrics that exist in NEITHER COSIF nor the CVM
package — NIM, the issuer's own efficiency ratio, ROE/ROA as reported, Índice de Basileia,
and asset quality by IFRS-9 / Res. 4.966 stage. #148 (a *general* MZiQ adapter) was killed on
its kill criterion; this is deliberately NOT that:

* **Curated, no discovery.** Five issuers, each with a checked-in company UUID, category slug,
  title pattern and ONE sheet map. No crawling, no slug inference — that was #148's cost.
  Cielo was in the issue's list and is dropped: its workbook is frozen at 2T24 (live probe
  2026-09-24, #152 comment). ``max_age_quarters`` would skip it anyway.
* **Labels, never row indexes.** Row labels drift inside two quarters (ABC ``Balanço`` 34→35
  rows, Porto ``DREs Verticais`` 139→138). A fixed row index is #145's CVM-equity bug again
  (BB stored at R$38.7bn instead of R$193.6bn). Rows are matched on a *folded* label —
  accents, case, whitespace and footnote markers (``(3)``, ``¹``, ``*``) stripped — against a
  per-issuer pattern, optionally scoped under a section anchor. No fuzzy fallback: ``Estágio 2``
  vs ``Estágio 3`` is one character apart, and a fuzzy match there is a silent wrong number.
* **Period header, never the rightmost cell.** Inter appends ``QoQ``/``YoY Variation`` columns
  after the quarters; a rightmost-numeric scan read ROE as 2.63 (the variation). The column is
  the one whose header in the nearest header row above the value reads the target period
  (``2T26`` / ``2Q26`` / ``Jun/26`` / ``30/06/2026`` / an Excel date serial).
* **Granularity is recorded, not assumed.** Banrisul's NIM sheet groups by ``1T26 / 1S26`` with
  three sub-columns and has no ``2T26`` block at all, so its latest NIM is **half-year**. Every
  metric carries the header it was read under and its granularity (``quarter`` / ``period_end``
  / ``half_year`` / ``nine_months`` / ``year``).
* **None beats a plausible lie.** A label that does not match, a blank target cell, or a value
  outside the metric's plausible band (a percentage stored as 14.4 instead of 0.144, a variation
  column) emits ``None`` — the same rule #146/#149's gates follow.
* **Never ``revenue``.** ``product_intel.market_structure`` sums ``revenue`` across entities for
  market size / HHI. This store holds ratios only and lives under its own key
  (``financials/issuer_kpis.json``); it is joined onto feed records as a separate block.
* **Workbook confirmed by Content-Type** (``…spreadsheetml.sheet`` or the macro-enabled
  ``.xlsm`` type), never by link text or file title.
* **CNPJ first** (#149): the configured CNPJ roots resolve the entity; the configured
  ``entity_id`` is only the fallback when the registry does not carry the root.

Parsed with the stdlib (zipfile + ElementTree), like ``previc_efpc`` / ``spa_apostas`` — no
openpyxl in the shared Lambda asset.

Store shape (``financials/issuer_kpis.json``)::

    {"as_of", "records": {entity_id: {entity_id, issuer, binding, cnpj_root, period,
        period_label, file_title, file_published, source_url,
        metrics: {name: {value, unit, granularity, header, sheet, label, scope?} | None}}}}
"""
from __future__ import annotations

import datetime as dt
import io
import json
import re
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from typing import Any, Callable

import requests

CATALOG_URL = ("https://apicatalog.mziq.com/filemanager/company/{uuid}"
               "/filter/categories/year/meta")
INDEX_KEY = "financials/issuer_kpis.json"
_UA = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}

# Confirm the payload is a workbook by what the server SAYS it is, not by the title.
_XLSX_TYPES = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroenabled.12",          # .xlsm (BR Partners, once)
)

# Metric catalogue: unit + plausible band. A value outside the band is a unit slip (14.4 for
# 14.4%) or the wrong column (a variation), so it is dropped, not clamped.
METRICS: dict[str, dict[str, Any]] = {
    "nim":               {"unit": "fraction", "band": (-0.05, 0.60)},
    "efficiency_ratio":  {"unit": "fraction", "band": (0.0, 3.0)},
    "roe":               {"unit": "fraction", "band": (-1.5, 1.5)},
    "roa":               {"unit": "fraction", "band": (-0.5, 0.5)},
    "basel_ratio":       {"unit": "fraction", "band": (0.0, 1.0)},
    "stage2_ratio":      {"unit": "fraction", "band": (0.0, 1.0)},
    "stage3_ratio":      {"unit": "fraction", "band": (0.0, 1.0)},
    "stage3_coverage":   {"unit": "fraction", "band": (0.0, 5.0)},
    "arpac_brl":         {"unit": "BRL/month", "band": (0.0, 10_000.0)},
    "cost_to_serve_brl": {"unit": "BRL/month", "band": (0.0, 10_000.0)},
}


def _m(sheet: str, label: str, *, section: str | None = None, subcol: str | None = None,
       scope: str | None = None) -> dict[str, Any]:
    """One metric spec: a label pattern on one sheet, optionally under a section anchor."""
    return {"sheet": sheet, "label": label, "section": section, "subcol": subcol, "scope": scope}


def _ratio(num: dict[str, Any], den: dict[str, Any]) -> dict[str, Any]:
    """A metric the issuer publishes only as its two parts (Banrisul stage balances)."""
    return {"ratio": (num, den)}


# --- Per-issuer configuration (checked in; #152 comment, live-probed 2026-09-24) -----------
# Patterns match the FOLDED label (see ``_fold``): upper-case ASCII, single spaces, footnote
# markers removed. Each issuer has its own map — the spike found sheet names share nothing
# across issuers, so there is no cross-issuer schema to lean on.
_ABC_SUM = "Sumário - Summary"
_ABC_STG = "Nível de Risco - Stages"
_BRS_IDX = "Índices Financeiros"
_BRS_STG = "Crédito por Estágios"
_PRT_IND = "Indicadores Oper. e Fin."
_PRT_DRE = "DREs Verticais"
_BRP_EFF = "Eficiência & Remuneração"
_BRP_CAP = "Adequação de Capital"
_BRP_DRE = "DRE Gerencial (Trimestral)"

ISSUERS: dict[str, dict[str, Any]] = {
    "abc_brasil": {
        "company_uuid": "6298ef6f-2b75-43f8-b2ab-99e3fe33e809",
        "categories": ["central-resultados-series-historicas"],
        "title": r"^SERIES HISTORICAS \dT\d\d PT$",
        "cnpj_roots": ["28195667"],
        "metrics": {
            "nim": _m(_ABC_SUM, r"^NIM \(MARGEM FINANCEIRA\)"),
            "roe": _m(_ABC_SUM, r"^ROAE CONTABIL"),
            "roa": _m(_ABC_SUM, r"^ROAA RECORRENTE"),
            "efficiency_ratio": _m(_ABC_SUM, r"^INDICE DE EFICIENCIA$"),
            "basel_ratio": _m(_ABC_SUM, r"^INDICE DE BASILEIA$"),
            "stage2_ratio": _m(_ABC_STG, r"^ESTAGIO 2$", section=r"^% DA CARTEIRA EXPANDIDA"),
            "stage3_ratio": _m(_ABC_STG, r"^ESTAGIO 3$", section=r"^% DA CARTEIRA EXPANDIDA"),
        },
    },
    "banrisul": {
        "company_uuid": "fafdeaf3-7820-4ec2-9477-ce501c563c96",
        # NOT ``cr-fact-sheet`` as the issue body said — that returns the "BanRI360°" informativo.
        "categories": ["cr-series-historicas"],
        "title": r"^SERIES HISTORICAS \dT\d\d$",
        "cnpj_roots": ["92702067"],
        "metrics": {
            # Grouped by 1T26 / 1S26 with Balanço / Receita / Taxa sub-columns: no 2T26 block,
            # so this resolves to the half-year and says so.
            "nim": _m("Margem Financeira", r"^MARGEM FINANCEIRA ANUALIZADA$", subcol=r"^TAXA"),
            "roe": _m(_BRS_IDX, r"^ROAE ANUALIZADO$"),
            "roa": _m(_BRS_IDX, r"^ROAA ANUALIZADO$"),
            "efficiency_ratio": _m(_BRS_IDX, r"^INDICE DE EFICIENCIA"),
            "basel_ratio": _m(_BRS_IDX, r"^INDICE DE BASILEIA"),
            "stage2_ratio": _ratio(_m(_BRS_STG, r"^ESTAGIO 2$"), _m(_BRS_STG, r"^TOTAL$")),
            "stage3_ratio": _ratio(_m(_BRS_STG, r"^ESTAGIO 3$"), _m(_BRS_STG, r"^TOTAL$")),
        },
    },
    "porto_seguro": {
        "company_uuid": "b77a3922-d280-4451-b3ee-0afec4577834",
        "categories": ["central-resultados-planilha"],
        "title": r"^PLANILHA \dT\d\d$",
        # Porto Seguro S.A. (holding), then the Cia. de Seguros Gerais the registry seeds.
        "cnpj_roots": ["02149205", "61198164"],
        # An insurer: no group NIM, no Basileia. The bank-style ratios Porto publishes are for
        # its Porto Bank vertical and are stored with ``scope`` so no card reads them as the
        # group's. The consolidated ROAE is the only group-level ratio here.
        "metrics": {
            "roe": _m(_PRT_IND, r"^RENTABILIDADE SOBRE O PATRIMONIO \(ROAE\)$",
                      section=r"^RENTABILIDADE$"),
            "nim": _m(_PRT_IND, r"^NIM$", section=r"^CARTAO DE CREDITO E FINANCIAMENTO$",
                      scope="porto_bank"),
            "stage3_coverage": _m(_PRT_IND, r"^INDICE DE COBERTURA ESTAGIO 3$",
                                  section=r"^CARTAO DE CREDITO E FINANCIAMENTO$",
                                  scope="porto_bank"),
            "efficiency_ratio": _m(_PRT_DRE, r"^INDICE DE EFICIENCIA",
                                   section=r"^DRE GERENCIAL - PORTO BANK", scope="porto_bank"),
        },
    },
    "br_partners": {
        "company_uuid": "f4c9c74c-60bd-49b4-9fdf-007fb8f0f351",
        "categories": ["central_de_resultados_series_historicas"],
        "title": r"^SERIES HISTORICAS \dT\d\d$",
        "cnpj_roots": ["10739356"],            # BRBI BR Partners S.A. (CVM cadastro)
        "metrics": {
            "efficiency_ratio": _m(_BRP_EFF, r"^INDICE DE EFICIENCIA$"),
            "roe": _m(_BRP_DRE, r"^ROAE$"),
            # Header row is Excel date serials (46203 = 2026-06-30), resolved as period_end.
            "basel_ratio": _m(_BRP_CAP, r"^BASILEIA$"),
        },
    },
    "inter": {
        "company_uuid": "b4dc0b14-a83a-40a9-9545-d9e4f18ed7af",
        "categories": ["series_historicas"],
        "title": r"^INTER&CO - SERIES HISTORICAS \dT\d\d$",
        "cnpj_roots": ["00416968"],            # Banco Inter S.A. (Inter&Co is a foreign BDR)
        # Every sheet ends in QoQ / YoY Variation columns — the reason for header resolution.
        # No IFRS-9 stages: Inter publishes NPL buckets, so the stage metrics stay absent.
        "metrics": {
            "nim": _m("9.2 NIM & Yields", r"^NIM 2\.0 \(%\)$"),
            "efficiency_ratio": _m("9.4 Efficiency | Eficiência", r"^EFFICIENCY RATIO \(%\)$"),
            "roe": _m("9.8 ROE & ROA", r"^ROE \(%\)$"),
            "roa": _m("9.8 ROE & ROA", r"^ROA \(%\)$"),
            "basel_ratio": _m("9.9 Capital | Basileia", r"^BASEL RATIO \(RE/RWA\)$"),
            "arpac_brl": _m("9.6 ARPAC", r"^GROSS ARPAC \(R\$\)$"),
            "cost_to_serve_brl": _m("9.5 CTS | Custo de servir", r"^COST-TO-SERVE \(R\$\)$"),
        },
    },
}


# --- Text helpers -----------------------------------------------------------------------------
_FOOTNOTE = re.compile(r"(\(\d{1,2}\)|[¹²³⁴⁵⁶⁷⁸⁹⁰]+|\*+)")
_TRAIL_DIGIT = re.compile(r"(?<=[A-Z)])\d$")      # "Clientes Corporativos4" -> footnote 4


def _fold(s: Any) -> str:
    """Label normal form: footnotes out, accents out, upper-case, single spaces."""
    t = _FOOTNOTE.sub(" ", str(s or "").replace("\xa0", " "))
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().upper()
    t = re.sub(r"\s+", " ", t).strip()
    return _TRAIL_DIGIT.sub("", t).strip()


def _num(v: Any) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _col_idx(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


# --- Minimal stdlib XLSX reader ---------------------------------------------------------------
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_RNS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_REF = re.compile(r"^([A-Z]+)(\d+)$")

Grid = list[tuple[int, dict[int, Any]]]          # [(row_no, {col_idx: str|float})]


class Workbook:
    """Sheets by name → rows of {column index: value}. Strings stay str, numbers become float.
    Works for .xlsx and .xlsm alike (same OOXML package; the macros are simply ignored)."""

    def __init__(self, content: bytes):
        self._z = zipfile.ZipFile(io.BytesIO(content))
        names = set(self._z.namelist())
        self._ss: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(self._z.read("xl/sharedStrings.xml"))
            self._ss = ["".join(t.text or "" for t in si.iter(f"{_NS}t"))
                        for si in root.findall(f"{_NS}si")]
        rels = ET.fromstring(self._z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") or "" for r in rels.iter(f"{_PKG_RNS}Relationship")}
        self.sheets: dict[str, str] = {}
        for s in ET.fromstring(self._z.read("xl/workbook.xml")).iter(f"{_NS}sheet"):
            t = targets.get(s.get(f"{_RNS}id"), "").lstrip("/")
            self.sheets[s.get("name") or ""] = t if t.startswith("xl/") else "xl/" + t
        self._cache: dict[str, Grid] = {}

    def sheet(self, name: str) -> Grid | None:
        """Rows of one sheet, looked up by EXACT name first, then by folded name (a trailing
        space was measured on 3 of Inter's and ABC's sheet names)."""
        path = self.sheets.get(name)
        if path is None:
            want = _fold(name)
            path = next((p for n, p in self.sheets.items() if _fold(n) == want), None)
        if path is None:
            return None
        if path not in self._cache:
            self._cache[path] = self._rows(path)
        return self._cache[path]

    def _rows(self, path: str) -> Grid:
        out: Grid = []
        for r in ET.fromstring(self._z.read(path)).iter(f"{_NS}row"):
            cells: dict[int, Any] = {}
            for c in r.findall(f"{_NS}c"):
                m = _REF.match(c.get("r") or "")
                if not m:
                    continue
                kind, v = c.get("t"), c.find(f"{_NS}v")
                if kind == "inlineStr":
                    val: Any = "".join(t.text or "" for t in c.iter(f"{_NS}t"))
                elif v is None or v.text is None:
                    continue
                elif kind == "s":
                    val = self._ss[int(v.text)]
                elif kind in ("str", "e"):
                    val = v.text
                else:
                    val = _num(v.text)
                    if val is None:
                        val = v.text
                if isinstance(val, str) and not val.strip():
                    continue
                cells[_col_idx(m.group(1))] = val
            out.append((int(r.get("r") or len(out) + 1), cells))
        return out


# --- Period headers ----------------------------------------------------------------------------
_MONTHS_PT = {3: ("MAR",), 6: ("JUN",), 9: ("SET", "SEP"), 12: ("DEZ", "DEC")}
_QEND = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
# A header-like string: 2T26, 2Q26, 1S26, 1H26, 9M25, Jun/26, 30/06/2026, 2026-06-30.
_PERIODISH = re.compile(
    r"^(\d[TQSH]\d{2}(\d{2})?|\d{1,2}M\d{2}|[A-Z]{3}[/\-]\d{2}(\d{2})?|\d{2}/\d{2}/\d{4}"
    r"|\d{4}-\d{2}-\d{2})$")
_EXCEL_EPOCH = dt.date(1899, 12, 30)


def period_tokens(year: int, quarter: int) -> list[tuple[str, str]]:
    """Header spellings for a quarter, most specific first, each with its granularity."""
    yy, yyyy = f"{year % 100:02d}", str(year)
    m, d = _QEND[quarter]
    toks = [(f"{quarter}{p}{y}", "quarter") for p in ("T", "Q") for y in (yy, yyyy)]
    for mon in _MONTHS_PT[m]:
        toks += [(f"{mon}/{yy}", "period_end"), (f"{mon}/{yyyy}", "period_end"),
                 (f"{mon}-{yy}", "period_end"), (f"{mon}-{yyyy}", "period_end")]
    toks += [(f"{d:02d}/{m:02d}/{yyyy}", "period_end"), (f"{yyyy}-{m:02d}-{d:02d}", "period_end")]
    if quarter == 2:
        toks += [(f"1{p}{yy}", "half_year") for p in ("S", "H")] + [(f"6M{yy}", "half_year")]
    elif quarter == 3:
        toks += [(f"9M{yy}", "nine_months")]
    elif quarter == 4:
        toks += [(f"2{p}{yy}", "half_year") for p in ("S", "H")]
        toks += [(f"12M{yy}", "year"), (f"FY{yy}", "year"), (yyyy, "year")]
    return toks


def _header_text(v: Any) -> str | None:
    """A header cell as a comparable token, or None. Integer Excel date serials (BR Partners'
    capital sheet) become dd/mm/yyyy; other numbers are not headers."""
    if isinstance(v, float):
        if v.is_integer() and 20000 <= v <= 80000:
            day = _EXCEL_EPOCH + dt.timedelta(days=int(v))
            if (day + dt.timedelta(days=1)).day == 1:          # month-end dates only
                return day.strftime("%d/%m/%Y")
        if v.is_integer() and 1990 <= v <= 2100:
            return str(int(v))                                  # "2025" year-total column
        return None
    t = _fold(v).replace(" ", "")
    return t or None


def _is_header_row(cells: dict[int, Any]) -> bool:
    hits = sum(1 for v in cells.values()
               if (h := _header_text(v)) and _PERIODISH.match(h))
    return hits >= 2


def resolve_column(grid: Grid, row_pos: int, year: int, quarter: int,
                   subcol: str | None = None) -> tuple[int, str, str] | None:
    """Column holding (year, quarter) for the value at ``grid[row_pos]``.

    Uses the NEAREST header row above the value — sheets stack several blocks, each with its
    own header — and matches the target period there, most specific spelling first. Returns
    ``(col, header_text, granularity)`` or None when that header row does not carry the period.
    Never falls back to "the last numeric column": that is where the QoQ/YoY columns live.
    """
    hdr_pos = next((p for p in range(row_pos - 1, -1, -1) if _is_header_row(grid[p][1])), None)
    if hdr_pos is None:
        return None
    header = {c: _header_text(v) for c, v in grid[hdr_pos][1].items()}
    for tok, gran in period_tokens(year, quarter):
        col = next((c for c, h in sorted(header.items()) if h == tok), None)
        if col is None:
            continue
        if subcol:
            # Merged group header (Banrisul: "1S26" over Balanço | Receita | Taxa): the value
            # lives in the sub-column whose sub-header matches, before the next group starts.
            nxt = min((c for c, h in header.items() if c > col and h and _PERIODISH.match(h)),
                      default=10**6)
            rx = re.compile(subcol)
            sub = next((c for p in range(hdr_pos + 1, row_pos)
                        for c, v in sorted(grid[p][1].items())
                        if col <= c < nxt and isinstance(v, str) and rx.search(_fold(v))), None)
            if sub is None:
                return None
            col = sub
        return col, tok, gran
    return None


# --- Label matching ----------------------------------------------------------------------------
def _labels(cells: dict[int, Any]) -> list[str]:
    return [_fold(v) for v in cells.values() if isinstance(v, str) and _fold(v)]


def _has_numbers(cells: dict[int, Any]) -> bool:
    return any(isinstance(v, float) for v in cells.values())


def find_row(grid: Grid, label: str, section: str | None = None) -> int | None:
    """Position of the first VALUE row whose folded label matches ``label`` (after the first
    ``section`` anchor, when given). Label-only rows — Inter repeats the metric name as a block
    title with no numbers — are skipped, so the match is the row that carries the figure."""
    start = 0
    if section:
        sx = re.compile(section)
        start = next((i + 1 for i, (_, c) in enumerate(grid) if any(sx.search(t) for t in _labels(c))),
                     None)
        if start is None:
            return None
    lx = re.compile(label)
    for i in range(start, len(grid)):
        cells = grid[i][1]
        if any(lx.search(t) for t in _labels(cells)) and _has_numbers(cells):
            return i
    return None


def read_metric(wb: Workbook, spec: dict[str, Any], year: int, quarter: int) -> dict[str, Any] | None:
    """One spec → ``{value, granularity, header, sheet, label, scope?}`` or None. None whenever the
    sheet, the label, the period column or a numeric cell is missing — never a neighbour."""
    grid = wb.sheet(spec["sheet"])
    if not grid:
        return None
    pos = find_row(grid, spec["label"], spec.get("section"))
    if pos is None:
        return None
    col = resolve_column(grid, pos, year, quarter, spec.get("subcol"))
    if col is None:
        return None
    c, header, gran = col
    val = grid[pos][1].get(c)
    if not isinstance(val, float):
        return None
    out = {"value": val, "granularity": gran, "header": header, "sheet": spec["sheet"],
           "label": next(iter(_labels(grid[pos][1])), "")}
    if spec.get("scope"):
        out["scope"] = spec["scope"]
    return out


def extract(content: bytes, issuer: str, year: int, quarter: int, *,
            config: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any] | None]:
    """Every configured metric for one issuer's workbook. Keys are always present; a value that
    could not be read cleanly is ``None``."""
    cfg = (config or ISSUERS)[issuer]
    wb = Workbook(content)
    out: dict[str, dict[str, Any] | None] = {}
    for name, spec in cfg["metrics"].items():
        if "ratio" in spec:
            num, den = (read_metric(wb, s, year, quarter) for s in spec["ratio"])
            if not num or not den or not den["value"] or num["granularity"] != den["granularity"]:
                out[name] = None
                continue
            rec = dict(num, value=num["value"] / den["value"],
                       label=f"{num['label']} / {den['label']}")
        else:
            rec = read_metric(wb, spec, year, quarter)
        meta = METRICS[name]
        if rec is None or not (meta["band"][0] <= rec["value"] <= meta["band"][1]):
            out[name] = None
            continue
        rec["value"] = round(rec["value"], 6)
        rec["unit"] = meta["unit"]
        out[name] = rec
    return out


# --- Catalog + download ------------------------------------------------------------------------
def list_workbooks(issuer: str, year: int, *, post: Callable[..., Any] | None = None,
                   config: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Catalog entries for one issuer-year whose title matches the issuer's workbook pattern."""
    cfg = (config or ISSUERS)[issuer]
    post = post or requests.post
    r = post(CATALOG_URL.format(uuid=cfg["company_uuid"]), timeout=30, headers=_UA,
             data=json.dumps({"year": year, "categories": cfg["categories"],
                              "language": "pt_BR", "published": True}))
    metas = ((r.json() or {}).get("data") or {}).get("document_metas") or []
    rx = re.compile(cfg["title"])
    return [m for m in metas
            if rx.match(_fold(m.get("file_title") or "")) and m.get("file_quarter") and m.get("file_url")]


def latest_workbook(issuer: str, *, today: dt.date | None = None, post: Any = None,
                    max_age_quarters: int = 2,
                    config: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Newest workbook in this year or last, or None when the newest is older than
    ``max_age_quarters`` — the staleness gate Cielo (frozen at 2T24) would have tripped."""
    today = today or dt.date.today()
    metas: list[dict[str, Any]] = []
    for y in (today.year, today.year - 1):
        metas = list_workbooks(issuer, y, post=post, config=config)
        if metas:
            break
    if not metas:
        return None
    best = max(metas, key=lambda m: (int(m["file_year"]), int(m["file_quarter"])))
    age = (today.year * 4 + (today.month - 1) // 3) - (int(best["file_year"]) * 4 + int(best["file_quarter"]) - 1)
    return best if age <= max_age_quarters else None


def download(url: str, *, get: Callable[..., Any] | None = None) -> bytes | None:
    """Workbook bytes, or None unless the server labels it a spreadsheet (xlsx/xlsm)."""
    get = get or requests.get
    r = get(url, timeout=90, headers={"User-Agent": _UA["User-Agent"]})
    ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if getattr(r, "status_code", 200) != 200 or ctype not in _XLSX_TYPES:
        return None
    return r.content


# --- Entity binding + store ----------------------------------------------------------------------
def bind_entity(issuer: str, *, cnpj_resolver: Callable[[Any], str | None] | None = None,
                config: dict[str, dict[str, Any]] | None = None) -> tuple[str, str, str | None]:
    """#149 — CNPJ first. ``(entity_id, binding, cnpj_root)``; the configured issuer key is the
    fallback only when no configured root resolves in the registry."""
    cfg = (config or ISSUERS)[issuer]
    for root in cfg.get("cnpj_roots") or []:
        try:
            eid = cnpj_resolver(root) if cnpj_resolver else None
        except Exception:  # pragma: no cover - registry outage degrades to the config id
            eid = None
        if eid:
            return eid, "cnpj", root
    return issuer, "config", None


def harvest_issuer(issuer: str, *, today: dt.date | None = None, post: Any = None, get: Any = None,
                   cnpj_resolver: Any = None) -> dict[str, Any]:
    """Fetch + extract one issuer. Always returns a dict; ``status`` says what happened."""
    meta = latest_workbook(issuer, today=today, post=post)
    if meta is None:
        return {"issuer": issuer, "status": "no_recent_workbook"}
    content = download(meta["file_url"], get=get)
    if content is None:
        return {"issuer": issuer, "status": "not_a_workbook", "source_url": meta["file_url"]}
    year, quarter = int(meta["file_year"]), int(meta["file_quarter"])
    metrics = extract(content, issuer, year, quarter)
    eid, binding, root = bind_entity(issuer, cnpj_resolver=cnpj_resolver)
    m, d = _QEND[quarter]
    return {
        "status": "ok", "entity_id": eid, "issuer": issuer, "binding": binding, "cnpj_root": root,
        "period": f"{year}-{m:02d}-{d:02d}", "period_label": f"{quarter}T{year % 100:02d}",
        "file_title": meta.get("file_title"), "file_published": (meta.get("file_published_date") or "")[:10],
        "source_url": meta["file_url"], "metrics": metrics,
        "clean": sum(1 for v in metrics.values() if v is not None),
    }


def kpis_by_entity(index: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return dict((index or {}).get("records") or {})


def load_index(bucket: str, *, s3: Any = None) -> dict[str, Any]:
    import boto3

    s3 = s3 or boto3.client("s3")
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=INDEX_KEY)["Body"].read())
    except Exception:  # pragma: no cover - absent store
        return {}


def persist(bucket: str, index: dict[str, Any], *, s3: Any = None) -> str:
    import boto3

    s3 = s3 or boto3.client("s3")
    s3.put_object(Bucket=bucket, Key=INDEX_KEY, ContentType="application/json",
                  Body=json.dumps(index, ensure_ascii=False).encode("utf-8"))
    return f"s3://{bucket}/{INDEX_KEY}"


def run(bucket: str | None, *, today: dt.date | None = None, post: Any = None, get: Any = None,
        cnpj_resolver: Any = None, s3: Any = None) -> dict[str, Any]:
    """Refresh ``financials/issuer_kpis.json``. An issuer that fails keeps its PREVIOUS record —
    a workbook outage degrades to last quarter's figures, never to an empty store."""
    if cnpj_resolver is None:
        from src.synth import entities as _ent

        cnpj_resolver = _ent.resolve_by_cnpj
    prev = kpis_by_entity(load_index(bucket, s3=s3)) if bucket else {}
    records = dict(prev)
    summary: dict[str, Any] = {"issuers": {}}
    for issuer in ISSUERS:
        try:
            rec = harvest_issuer(issuer, today=today, post=post, get=get, cnpj_resolver=cnpj_resolver)
        except Exception as exc:  # pragma: no cover - network; one issuer never sinks the rest
            rec = {"issuer": issuer, "status": f"error: {exc.__class__.__name__}"}
        summary["issuers"][issuer] = {k: rec.get(k) for k in ("status", "entity_id", "period_label", "clean")}
        if rec.get("status") == "ok" and rec.get("clean"):
            rec.pop("status", None)
            records[rec["entity_id"]] = rec
    index = {"as_of": (today or dt.date.today()).isoformat(), "records": records}
    if records and bucket:
        summary["s3"] = persist(bucket, index, s3=s3)
    else:
        summary["skipped_persist"] = True
    return summary


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    """OncaFinancialsPipeline IssuerKpisTask (#152)."""
    import os

    return {"statusCode": 200, "body": json.dumps(run(os.environ.get("ONCA_DIGESTS_BUCKET")),
                                                  ensure_ascii=False)}
