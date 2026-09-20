"""CVM financials (issue #7, #145): parsing + matching + derived metrics, no network."""
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import cvm_financials as cf

_COLS = ["CNPJ_CIA", "DT_REFER", "DENOM_CIA", "CD_CVM", "ORDEM_EXERC",
         "DT_INI_EXERC", "DT_FIM_EXERC", "CD_CONTA", "DS_CONTA", "VL_CONTA", "ESCALA_MOEDA"]


def _csv(rows):
    out = [";".join(_COLS)]
    for r in rows:
        out.append(";".join(str(r.get(c, "")) for c in _COLS))
    return ("\n".join(out) + "\n").encode("latin-1")


def _zip(*, bpa=(), bpp=(), dre=(), year=2026, doc="itr"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for needle, rows in (("BPA_con", bpa), ("BPP_con", bpp), ("DRE_con", dre)):
            zf.writestr(f"{doc}_cia_aberta_{needle}_{year}.csv", _csv(rows))
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))


def _row(**kw):
    base = {"CNPJ_CIA": "11.111.111/0001-11", "DENOM_CIA": "ACME SA", "CD_CVM": "1",
            "ORDEM_EXERC": "ÚLTIMO", "ESCALA_MOEDA": "UNIDADE"}
    base.update(kw)
    return base


def _period(**kw):
    return {"period": "2024-12-31", "name": kw.pop("name", "X"), **kw}


def _resolver(names):
    def r(item):
        return names.get(item.get("institution"), [])
    return r


def test_build_index_cnpj_match_and_derived_metrics():
    ents = [{"entity_id": "itau", "cnpj_roots": ["60872504"], "ticker": "ITUB4"}]
    stmts = {"60872504": {
        "ÚLTIMO": _period(name="ITAU", revenue=1000.0, net_income=100.0, assets=5000.0, equity=1000.0),
        "PENÚLTIMO": _period(revenue=800.0, net_income=80.0),
    }}
    idx = cf.build_index(ents, stmts)
    r = idx["itau"]
    assert r["net_margin"] == 0.1                       # 100/1000
    assert r["revenue_growth"] == 0.25                  # (1000-800)/800
    assert r["leverage"] == 4.0                         # (5000-1000)/1000


def test_cnpj_beats_name_and_name_picks_largest():
    ents = [{"entity_id": "btg", "cnpj_roots": ["30306294"], "ticker": "BPAC11"},
            {"entity_id": "b3", "cnpj_roots": [], "ticker": "B3SA3"}]
    stmts = {
        "30306294": {"ÚLTIMO": _period(name="BANCO BTG", revenue=500.0, net_income=160.0)},
        "99999999": {"ÚLTIMO": _period(name="BTG HOLDING", revenue=9.0, net_income=1.0)},  # name→btg
        "11111111": {"ÚLTIMO": _period(name="B3 SA", revenue=100.0, net_income=40.0)},
        "22222222": {"ÚLTIMO": _period(name="B3 UNIT", revenue=250.0, net_income=90.0)},   # name→b3, bigger
    }
    resolver = _resolver({"BANCO BTG": ["btg"], "BTG HOLDING": ["btg"],
                          "B3 SA": ["b3"], "B3 UNIT": ["b3"]})
    idx = cf.build_index(ents, stmts, resolver=resolver)
    assert idx["btg"]["revenue"] == 500.0    # CNPJ match wins over the name-only holding
    assert idx["b3"]["revenue"] == 250.0     # among name matches, the largest by revenue


def test_untracked_and_ambiguous_issuers_skipped():
    ents = [{"entity_id": "itau", "cnpj_roots": [], "ticker": "ITUB4"},
            {"entity_id": "bb", "cnpj_roots": [], "ticker": "BBAS3"}]
    stmts = {
        "55555555": {"ÚLTIMO": _period(name="OTHER CO", revenue=10.0)},   # -> 2 tracked = ambiguous
        "66666666": {"ÚLTIMO": _period(name="UNTRACKED", revenue=5.0)},   # -> none
    }
    idx = cf.build_index(ents, stmts, resolver=_resolver({"OTHER CO": ["itau", "bb"], "UNTRACKED": []}))
    assert idx == {}


# --- #145: package parsing ------------------------------------------------------------

def test_only_the_newest_reference_date_survives():
    """An ITR package carries several DT_REFERs; mixing Q1 into Q2 is the silent failure."""
    dre = [
        _row(DT_REFER="2026-03-31", DT_INI_EXERC="2026-01-01", DT_FIM_EXERC="2026-03-31",
             CD_CONTA="3.01", VL_CONTA="100"),
        _row(DT_REFER="2026-06-30", DT_INI_EXERC="2026-01-01", DT_FIM_EXERC="2026-06-30",
             CD_CONTA="3.01", VL_CONTA="250"),
    ]
    got = cf.parse_statements(_zip(dre=dre), doc="ITR")["11111111"]["ÚLTIMO"]
    assert got["period"] == "2026-06-30"
    assert got["revenue"] == 250.0
    assert got["months"] == 6


def test_longest_dre_span_wins_and_months_is_recorded():
    """At Q2 the DRE carries both the quarter and the YTD. Taking whichever row came first
    yields a 3-month revenue for one issuer and a 6-month one for the next."""
    dre = [
        _row(DT_REFER="2026-06-30", DT_INI_EXERC="2026-04-01", DT_FIM_EXERC="2026-06-30",
             CD_CONTA="3.01", VL_CONTA="40"),          # quarter, listed FIRST
        _row(DT_REFER="2026-06-30", DT_INI_EXERC="2026-01-01", DT_FIM_EXERC="2026-06-30",
             CD_CONTA="3.01", VL_CONTA="90"),          # year-to-date
    ]
    got = cf.parse_statements(_zip(dre=dre), doc="ITR")["11111111"]["ÚLTIMO"]
    assert got["revenue"] == 90.0 and got["months"] == 6
    assert got["period_start"] == "2026-01-01"


def test_equity_is_matched_by_label_not_by_code_for_banks():
    """CVM's financial-institution BPP layout puts *Provisões* at 2.03 and Patrimônio
    Líquido at 2.07. The old fixed `_CD_EQUITY = "2.03"` stored provisions as equity —
    Banco do Brasil came out at R$38.7bn instead of R$193.6bn, ~5x on leverage."""
    bpp = [
        _row(DT_REFER="2025-12-31", CD_CONTA="2.03", DS_CONTA="Provisões", VL_CONTA="38"),
        _row(DT_REFER="2025-12-31", CD_CONTA="2.07",
             DS_CONTA="Patrimônio Líquido Consolidado", VL_CONTA="193"),
    ]
    got = cf.parse_statements(_zip(bpp=bpp, doc="dfp"), doc="DFP")["11111111"]["ÚLTIMO"]
    assert got["equity"] == 193.0


def test_equity_still_reads_the_ordinary_layout():
    bpp = [_row(DT_REFER="2025-12-31", CD_CONTA="2.03",
                DS_CONTA="Patrimônio Líquido Consolidado", VL_CONTA="500")]
    got = cf.parse_statements(_zip(bpp=bpp, doc="dfp"), doc="DFP")["11111111"]["ÚLTIMO"]
    assert got["equity"] == 500.0


def test_escala_mil_is_applied():
    bpa = [_row(DT_REFER="2025-12-31", CD_CONTA="1", DS_CONTA="Ativo Total",
                VL_CONTA="7", ESCALA_MOEDA="MIL")]
    got = cf.parse_statements(_zip(bpa=bpa, doc="dfp"), doc="DFP")["11111111"]["ÚLTIMO"]
    assert got["assets"] == 7000.0


# --- #145: picking the package year by content ----------------------------------------

def test_latest_statements_skips_the_sparse_current_year():
    """dfp_cia_aberta_2026 exists but held 8 issuers against 2025's 438 on 2026-09-19.
    A truthiness check would have taken the 8."""
    packages = {2026: {str(i): {} for i in range(8)},
                2025: {str(i): {} for i in range(438)}}
    seen = []

    def fetcher(year, *, doc="DFP"):
        seen.append(year)
        return packages.get(year, {})

    import datetime as dt
    year, stmts = cf.latest_statements(today=dt.date(2026, 9, 19), fetcher=fetcher)
    assert year == 2025 and len(stmts) == 438
    assert seen == [2026, 2025]          # newest-first, stops as soon as one qualifies


def test_latest_statements_returns_nothing_rather_than_a_sparse_package():
    year, stmts = cf.latest_statements(
        today=__import__("datetime").date(2026, 1, 5), back=2,
        fetcher=lambda y, doc="DFP": {"a": {}},
    )
    assert (year, stmts) == (None, {})


# --- #145: derived metrics + the interim block ----------------------------------------

def test_revenue_growth_suppressed_when_the_spans_differ():
    ents = [{"entity_id": "acme", "cnpj_roots": ["11111111"], "ticker": "ACM3"}]
    stmts = {"11111111": {
        "ÚLTIMO": {"period": "2026-06-30", "months": 6, "revenue": 90.0, "name": "ACME"},
        "PENÚLTIMO": {"period": "2025-12-31", "months": 12, "revenue": 150.0},
    }}
    r = cf.build_index(ents, stmts)["acme"]
    assert r["revenue_growth"] is None       # 6 months vs 12 is not growth, it's a calendar
    assert r["months"] == 6


def test_merge_interim_keeps_annual_on_top_and_attaches_the_quarter():
    annual = {"acme": {"entity_id": "acme", "period": "2025-12-31", "months": 12,
                       "revenue": 150.0}}
    interim = {"acme": {"entity_id": "acme", "period": "2026-06-30", "months": 6,
                        "revenue": 90.0, "doc": "ITR"}}
    out = cf.merge_interim(annual, interim)["acme"]
    assert out["revenue"] == 150.0 and out["months"] == 12     # market-size field untouched
    assert out["interim"]["revenue"] == 90.0 and out["interim"]["months"] == 6


def test_merge_interim_drops_an_interim_older_than_the_annual():
    annual = {"acme": {"entity_id": "acme", "period": "2025-12-31", "revenue": 150.0}}
    interim = {"acme": {"entity_id": "acme", "period": "2025-09-30", "revenue": 100.0}}
    assert "interim" not in cf.merge_interim(annual, interim)["acme"]


def test_merge_interim_keeps_an_itr_only_issuer_with_null_annual_fields():
    out = cf.merge_interim({}, {"newco": {"entity_id": "newco", "name": "NEWCO",
                                          "period": "2026-06-30", "revenue": 12.0}})
    rec = out["newco"]
    assert rec["revenue"] is None            # never enters a market-size sum
    assert rec["interim"]["revenue"] == 12.0


# --- #145: the runner ------------------------------------------------------------------

def test_run_does_not_overwrite_a_good_store_with_an_empty_one(monkeypatch):
    from src.synth import entities as _ent
    from src.synth import entity_registry as _er
    monkeypatch.setattr(_er, "list_entities", lambda **kw: [])
    monkeypatch.setattr(_ent, "resolve_entities", lambda item: [])
    calls = []
    monkeypatch.setattr(cf, "persist", lambda *a, **k: calls.append(a))

    out = cf.run("some-bucket", year=2025, fetcher=lambda y, doc="DFP": {})
    assert out["records"] == 0 and out["skipped_persist"] is True
    assert calls == []


def test_name_fallback_never_reaches_a_fund_vehicle():
    """Live on 2026-09-19 the fallback attributed CYRELA BRAZIL REALTY's statements to the
    FII CYCR11, Eldorado Celulose's to ELDO11 and Banrisul's to FISP11 — a fund's alias
    carries its sponsor's brand, so any sponsor name resolves onto the fund."""
    ents = [{"entity_id": "cycr11", "cnpj_roots": [], "ticker": "CYCR11",
             "industries": ["real-estate-funds"]}]
    stmts = {"73178600": {"ÚLTIMO": _period(name="CYRELA BRAZIL REALTY S.A.", revenue=9.4e9)}}
    assert cf.build_index(ents, stmts, resolver=_resolver({"CYRELA BRAZIL REALTY S.A.": ["cycr11"]})) == {}


def test_a_fund_that_files_under_its_own_cnpj_is_still_matched():
    """The guard is on the NAME fallback only — a CNPJ match is direct evidence."""
    ents = [{"entity_id": "cycr11", "cnpj_roots": ["36501233"], "ticker": "CYCR11",
             "industries": ["real-estate-funds"]}]
    stmts = {"36501233": {"ÚLTIMO": _period(name="FII CYCR", revenue=5.0)}}
    assert cf.build_index(ents, stmts)["cycr11"]["revenue"] == 5.0
