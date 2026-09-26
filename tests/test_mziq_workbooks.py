"""#152 — issuer IR-workbook KPI adapter: label drift, period-header column resolution, the
QoQ/YoY-column trap, None-on-no-match, staleness + Content-Type gates. No network.

Workbooks are synthesised with a tiny stdlib OOXML writer (the production reader is stdlib too,
and openpyxl is not a dependency of this repo or its Lambda asset)."""
import datetime as dt
import io
import json
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import mziq_workbooks as mz


# --- synthetic workbook writer -------------------------------------------------------------------
def _xlsx(sheets, *, inline=False):
    """sheets: {name: {row_no: {col_letter: value}}} → xlsx bytes. Strings go to sharedStrings
    (or inline, to exercise both reader paths); numbers are plain <v>."""
    ss, ss_idx = [], {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        wb_sheets, rels = [], []
        for i, (name, rows) in enumerate(sheets.items(), start=1):
            xml_rows = []
            for rn in sorted(rows):
                cells = []
                for col, v in rows[rn].items():
                    ref = f"{col}{rn}"
                    if isinstance(v, str):
                        if inline:
                            cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(v)}</t></is></c>')
                        else:
                            if v not in ss_idx:
                                ss_idx[v] = len(ss)
                                ss.append(v)
                            cells.append(f'<c r="{ref}" t="s"><v>{ss_idx[v]}</v></c>')
                    else:
                        cells.append(f'<c r="{ref}"><v>{v}</v></c>')
                xml_rows.append(f'<row r="{rn}">{"".join(cells)}</row>')
            z.writestr(f"xl/worksheets/sheet{i}.xml",
                       '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                       f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>')
            wb_sheets.append(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>')
            rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/'
                        f'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
        z.writestr("xl/workbook.xml",
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   f'<sheets>{"".join(wb_sheets)}</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'{"".join(rels)}</Relationships>')
        if ss:
            z.writestr("xl/sharedStrings.xml",
                       '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                       + "".join(f"<si><t>{escape(s)}</t></si>" for s in ss) + "</sst>")
    return buf.getvalue()


def _cfg(metrics, **kw):
    return {"t": {"company_uuid": "u", "categories": ["c"], "title": r"^SERIES HISTORICAS \dT\d\d$",
                  "cnpj_roots": ["12345678"], "metrics": metrics, **kw}}


def _abc_like(nim_label="NIM (Margem Financeira) (a.a.)", extra_rows=0, value=0.0408):
    rows = {
        8: {"B": "Português", "F": "4Q25", "G": "1Q26", "H": "2Q26"},
        9: {"F": "4T25", "G": "1T26", "H": "2T26"},
        10: {"B": "R$ milhões", "F": "Res. 4.966", "G": "Res. 4.966", "H": "Res. 4.966"},
        12: {"B": "ÍNDICES DE DESEMPENHO (%)"},
    }
    r = 13
    for k in range(extra_rows):                       # drift: new lines land above the target
        rows[r] = {"C": f"Nova linha {k}", "F": 1.0, "G": 2.0, "H": 3.0}
        r += 1
    rows[r] = {"C": nim_label, "E": "NIM - Net Interest Margin", "F": 0.045, "G": 0.041, "H": value}
    rows[r + 1] = {"C": "Índice de Eficiência", "F": 0.38, "G": 0.41, "H": 0.412}
    return _xlsx({"Sumário - Summary": rows})


_NIM = {"nim": mz._m("Sumário - Summary", r"^NIM \(MARGEM FINANCEIRA\)")}


# --- label drift -------------------------------------------------------------------------------
def test_label_match_survives_row_drift():
    base = mz.extract(_abc_like(), "t", 2026, 2, config=_cfg(_NIM))["nim"]
    drifted = mz.extract(_abc_like(extra_rows=3), "t", 2026, 2, config=_cfg(_NIM))["nim"]
    assert base["value"] == drifted["value"] == 0.0408
    assert base["granularity"] == "quarter" and base["header"] == "2T26"


def test_label_match_tolerates_accent_case_and_footnote_drift():
    wb = _abc_like(nim_label="nim  (margem financeira)(1) ¹")
    assert mz.extract(wb, "t", 2026, 2, config=_cfg(_NIM))["nim"]["value"] == 0.0408


def test_fold_keeps_stage_numbers_distinct():
    assert mz._fold("Estágio 2") == "ESTAGIO 2"
    assert mz._fold("Índice de Eficiência Ajustado (3)") == "INDICE DE EFICIENCIA AJUSTADO"
    assert mz._fold("Clientes Corporativos4") == "CLIENTES CORPORATIVOS"


def test_renamed_label_emits_none_not_a_neighbour():
    wb = _abc_like(nim_label="Margem Líquida de Juros")
    assert mz.extract(wb, "t", 2026, 2, config=_cfg(_NIM))["nim"] is None


def test_section_anchor_scopes_repeated_labels():
    wb = _xlsx({"S": {
        1: {"A": "x", "B": "1T26", "C": "2T26"},
        2: {"A": "SALDO"},
        3: {"A": "Estágio 3", "B": 1000.0, "C": 1100.0},
        4: {"A": "% da Carteira"},
        5: {"A": "Estágio 3", "B": 0.028, "C": 0.029},
    }})
    spec = {"stage3_ratio": mz._m("S", r"^ESTAGIO 3$", section=r"^% DA CARTEIRA")}
    assert mz.extract(wb, "t", 2026, 2, config=_cfg(spec))["stage3_ratio"]["value"] == 0.029


# --- period-header column resolution -----------------------------------------------------------
def test_column_resolved_by_header_not_position():
    # 2T26 deliberately NOT the rightmost column.
    wb = _xlsx({"S": {1: {"A": "x", "B": "2T26", "C": "1T26", "D": "4T25"},
                      2: {"A": "ROE", "B": 0.15, "C": 0.12, "D": 0.11}}})
    spec = {"roe": mz._m("S", r"^ROE$")}
    assert mz.extract(wb, "t", 2026, 2, config=_cfg(spec))["roe"]["value"] == 0.15


def test_qoq_yoy_columns_are_never_read():
    """Inter's trap: a rightmost scan read ROE as 2.63 (the YoY variation)."""
    wb = _xlsx({"9.8 ROE & ROA": {
        2: {"B": "ROE (IFRS)", "K": "4Q25", "L": "1Q26", "M": "2Q26", "O": "QoQ Variation", "P": "YoY Variation"},
        4: {"B": "ROE (%)"},                                  # block title, no figures
        9: {"B": "ROE (%)", "K": 0.15, "L": 0.158, "M": 0.1626, "O": 0.75, "P": 2.63},
    }})
    spec = {"roe": mz._m("9.8 ROE & ROA", r"^ROE \(%\)$")}
    got = mz.extract(wb, "t", 2026, 2, config=_cfg(spec))["roe"]
    assert got["value"] == 0.1626 and got["header"] == "2Q26"


def test_missing_period_emits_none_even_with_older_columns():
    wb = _xlsx({"S": {1: {"A": "x", "B": "4T25", "C": "1T26"}, 2: {"A": "ROE", "B": 0.1, "C": 0.12}}})
    assert mz.extract(wb, "t", 2026, 2, config=_cfg({"roe": mz._m("S", r"^ROE$")}))["roe"] is None


def test_nearest_header_block_wins():
    """Stacked blocks: the value belongs to the block header right above it. A block that stops
    at 1T26 must not borrow the 2T26 column of the block above."""
    wb = _xlsx({"S": {1: {"A": "Bloco A", "B": "1T26", "C": "2T26"},
                      2: {"A": "ROE", "B": 0.1, "C": 0.2},
                      4: {"A": "Bloco B", "B": "4T25", "C": "1T26"},
                      5: {"A": "ROA", "B": 0.01, "C": 0.02}}})
    spec = {"roe": mz._m("S", r"^ROE$"), "roa": mz._m("S", r"^ROA$")}
    got = mz.extract(wb, "t", 2026, 2, config=_cfg(spec))
    assert got["roe"]["value"] == 0.2 and got["roa"] is None


def test_blank_or_dash_target_cell_is_none():
    wb = _xlsx({"S": {1: {"A": "x", "B": "1T26", "C": "2T26"},
                      2: {"A": "Índice de Cobertura Estágio 3", "B": 0.61, "C": "-"}}})
    spec = {"stage3_coverage": mz._m("S", r"^INDICE DE COBERTURA ESTAGIO 3$")}
    assert mz.extract(wb, "t", 2026, 2, config=_cfg(spec))["stage3_coverage"] is None


def test_half_year_subcolumn_records_granularity():
    """Banrisul NIM: 1T26 / 1S26 groups × (Balanço | Receita | Taxa); no 2T26 block."""
    wb = _xlsx({"Margem Financeira": {
        2: {"A": "Margem Financeira", "B": "1T26", "E": "1S26"},
        3: {"B": "Balanço Médio", "C": "Receita", "D": "Taxa Média",
            "E": "Balanço Médio", "F": "Receita", "G": "Taxa Média"},
        4: {"A": "Margem Financeira Anualizada", "D": 0.0464, "G": 0.0443},
    }})
    spec = {"nim": mz._m("Margem Financeira", r"^MARGEM FINANCEIRA ANUALIZADA$", subcol=r"^TAXA")}
    got = mz.extract(wb, "t", 2026, 2, config=_cfg(spec))["nim"]
    assert got["value"] == 0.0443 and got["granularity"] == "half_year" and got["header"] == "1S26"


def test_quarter_preferred_over_half_year_when_both_exist():
    wb = _xlsx({"S": {1: {"A": "x", "B": "2T26", "C": "1S26"}, 2: {"A": "ROE", "B": 0.2, "C": 0.25}}})
    got = mz.extract(wb, "t", 2026, 2, config=_cfg({"roe": mz._m("S", r"^ROE$")}))["roe"]
    assert got["value"] == 0.2 and got["granularity"] == "quarter"


def test_excel_date_serial_and_month_headers():
    serial = (dt.date(2026, 6, 30) - dt.date(1899, 12, 30)).days
    wb = _xlsx({"Cap": {10: {"E": "31/12/2025", "F": float(serial - 91), "G": float(serial)},
                        17: {"A": "Basileia", "E": 0.22, "F": 0.224, "G": 0.2159}},
                "Stg": {2: {"A": "Composição", "B": "Mar/26", "C": "Jun/26"},
                        3: {"A": "Estágio 3", "B": 4410.1, "C": 4440.7},
                        4: {"A": "Total", "B": 64309.1, "C": 64444.4}}})
    spec = {"basel_ratio": mz._m("Cap", r"^BASILEIA$"),
            "stage3_ratio": mz._ratio(mz._m("Stg", r"^ESTAGIO 3$"), mz._m("Stg", r"^TOTAL$"))}
    got = mz.extract(wb, "t", 2026, 2, config=_cfg(spec))
    assert got["basel_ratio"]["value"] == 0.2159 and got["basel_ratio"]["granularity"] == "period_end"
    assert abs(got["stage3_ratio"]["value"] - 4440.7 / 64444.4) < 1e-6


def test_out_of_band_value_is_dropped():
    """A percentage stored as 14.4 (points) instead of 0.144 is a unit slip, not a Basileia."""
    wb = _xlsx({"S": {1: {"A": "x", "B": "1T26", "C": "2T26"}, 2: {"A": "Basel ratio", "B": 0.14, "C": 14.4}}})
    assert mz.extract(wb, "t", 2026, 2,
                      config=_cfg({"basel_ratio": mz._m("S", r"^BASEL RATIO$")}))["basel_ratio"] is None


def test_inline_strings_and_trailing_space_sheet_names():
    wb = _xlsx({"9.5 CTS | Custo de servir ": {1: {"B": "x", "C": "1Q26", "D": "2Q26"},
                                               2: {"B": "Cost-to-serve (R$)", "C": 13.0, "D": 13.2}}},
               inline=True)
    spec = {"cost_to_serve_brl": mz._m("9.5 CTS | Custo de servir", r"^COST-TO-SERVE \(R\$\)$")}
    got = mz.extract(wb, "t", 2026, 2, config=_cfg(spec))["cost_to_serve_brl"]
    assert got["value"] == 13.2 and got["unit"] == "BRL/month"


def test_every_configured_metric_key_is_always_present():
    got = mz.extract(_xlsx({"Other": {1: {"A": "x"}}}), "abc_brasil", 2026, 2)
    assert set(got) == set(mz.ISSUERS["abc_brasil"]["metrics"]) and all(v is None for v in got.values())


def test_config_is_curated_and_never_emits_revenue():
    assert set(mz.ISSUERS) == {"abc_brasil", "banrisul", "porto_seguro", "br_partners", "inter"}
    assert mz.ISSUERS["banrisul"]["categories"] == ["cr-series-historicas"]
    for cfg in mz.ISSUERS.values():
        assert set(cfg["metrics"]) <= set(mz.METRICS)
    assert "revenue" not in mz.METRICS


# --- catalog / download gates ------------------------------------------------------------------
class _Resp:
    def __init__(self, payload=None, content=b"", ctype="", status=200):
        self._p, self.content, self.status_code = payload, content, status
        self.headers = {"Content-Type": ctype}

    def json(self):
        return self._p


def _catalog(metas_by_year):
    def post(url, **kw):
        year = json.loads(kw["data"])["year"]
        return _Resp({"success": True, "data": {"document_metas": metas_by_year.get(year, [])}})
    return post


def _meta(title, y, q):
    return {"file_title": title, "file_year": y, "file_quarter": q, "file_url": f"https://x/{y}{q}",
            "file_published_date": f"{y}-0{q * 3}-10T00:00:00.000Z"}


def test_latest_workbook_filters_title_and_picks_newest():
    post = _catalog({2026: [_meta("Séries Históricas 1T26", 2026, 1), _meta("Séries Históricas 2T26", 2026, 2),
                            _meta("Release 2T26", 2026, 2)]})
    got = mz.latest_workbook("t", today=dt.date(2026, 9, 26), post=post, config=_cfg({}))
    assert got["file_title"] == "Séries Históricas 2T26"


def test_stale_workbook_is_skipped():
    """Cielo's shape: newest workbook 2T24, nothing since."""
    post = _catalog({2025: [], 2026: []})
    assert mz.latest_workbook("t", today=dt.date(2026, 9, 26), post=post, config=_cfg({})) is None
    post = _catalog({2025: [_meta("Séries Históricas 1T25", 2025, 1)]})
    assert mz.latest_workbook("t", today=dt.date(2026, 9, 26), post=post, config=_cfg({})) is None


def test_download_confirms_content_type():
    x = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    xm = "application/vnd.ms-excel.sheet.macroEnabled.12"
    assert mz.download("u", get=lambda *a, **k: _Resp(content=b"PK", ctype=x)) == b"PK"
    assert mz.download("u", get=lambda *a, **k: _Resp(content=b"PK", ctype=xm)) == b"PK"
    assert mz.download("u", get=lambda *a, **k: _Resp(content=b"<html>", ctype="text/html")) is None
    assert mz.download("u", get=lambda *a, **k: _Resp(content=b"PK", ctype=x, status=404)) is None


def test_bind_entity_is_cnpj_first():
    assert mz.bind_entity("porto_seguro", cnpj_resolver={"61198164": "porto_seguro"}.get) == \
        ("porto_seguro", "cnpj", "61198164")
    assert mz.bind_entity("inter", cnpj_resolver={"00416968": "banco_inter"}.get) == \
        ("banco_inter", "cnpj", "00416968")
    assert mz.bind_entity("inter", cnpj_resolver=lambda _r: None) == ("inter", "config", None)


class _S3:
    def __init__(self, body=None):
        self.store = {} if body is None else {mz.INDEX_KEY: body}

    def get_object(self, Bucket, Key):
        if Key not in self.store:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.store[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.store[Key] = Body


def test_run_keeps_previous_record_when_an_issuer_fails():
    prev = {"as_of": "2026-06-01", "records": {"banrisul": {"entity_id": "banrisul", "period": "2026-03-31"}}}
    s3 = _S3(json.dumps(prev).encode())
    post = _catalog({})                                      # nothing anywhere → every issuer skipped
    out = mz.run("b", today=dt.date(2026, 9, 26), post=post, get=None,
                 cnpj_resolver=lambda _r: None, s3=s3)
    assert all(v["status"] == "no_recent_workbook" for v in out["issuers"].values())
    stored = json.loads(s3.store[mz.INDEX_KEY])
    assert stored["records"]["banrisul"]["period"] == "2026-03-31"


def test_run_end_to_end_with_a_synthetic_workbook():
    wb = _abc_like()
    post = _catalog({2026: [_meta("Séries Históricas 2T26 PT", 2026, 2)]})
    get = lambda *a, **k: _Resp(content=wb, ctype=mz._XLSX_TYPES[0])
    s3 = _S3()
    mz.run("b", today=dt.date(2026, 9, 26), post=post, get=get,
           cnpj_resolver={"28195667": "abc_brasil"}.get, s3=s3)
    rec = json.loads(s3.store[mz.INDEX_KEY])["records"]["abc_brasil"]
    assert rec["binding"] == "cnpj" and rec["period"] == "2026-06-30" and rec["period_label"] == "2T26"
    assert rec["metrics"]["nim"]["value"] == 0.0408 and rec["metrics"]["efficiency_ratio"]["value"] == 0.412
    assert "revenue" not in rec


# --- surfacing ---------------------------------------------------------------------------------
def test_attach_issuer_kpis_is_a_separate_block_and_attach_only():
    from src.dashboard import feed_builder as fb

    recs = [{"entity_id": "banrisul", "revenue": 1.0e10}, {"entity_id": "itau", "revenue": 2.0e11}]
    kpis = {"banrisul": {"period": "2026-06-30", "period_label": "2T26", "source_url": "u",
                         "metrics": {"efficiency_ratio": {"value": 0.576}}},
            "br_partners": {"period_label": "2T26", "metrics": {}}}
    fb.attach_issuer_kpis(recs, kpis)
    assert recs[0]["revenue"] == 1.0e10 and recs[0]["issuer_kpis"]["period_label"] == "2T26"
    assert "issuer_kpis" not in recs[1] and len(recs) == 2          # no synthetic br_partners card


def test_executive_rows_carry_group_level_issuer_ratios_only():
    from src.synth import executive as ex

    feed = {"entities": [{"entity": "porto_seguro", "label": "Porto", "fundamentals": {"roe_pct": 20.0}},
                         {"entity": "banrisul", "label": "Banrisul", "fundamentals": {"roe_pct": 11.0}}],
            "financials": [
                {"entity_id": "porto_seguro", "issuer_kpis": {"period_label": "2T26", "metrics": {
                    "efficiency_ratio": {"value": 0.2544, "scope": "porto_bank"}}}},
                {"entity_id": "banrisul", "issuer_kpis": {"period_label": "2T26", "metrics": {
                    "efficiency_ratio": {"value": 0.576}, "nim": {"value": 0.0443, "granularity": "half_year"},
                    "stage3_ratio": {"value": 0.0689}}}}]}
    rows = {r["entity"]: r for r in ex._fundamentals_rows(feed)}
    assert rows["porto_seguro"]["issuer_efficiency_pct"] is None      # Porto Bank ≠ Porto Seguro
    assert rows["banrisul"]["issuer_efficiency_pct"] == 57.6
    assert rows["banrisul"]["nim_pct"] == 4.4 and rows["banrisul"]["stage3_pct"] == 6.9
