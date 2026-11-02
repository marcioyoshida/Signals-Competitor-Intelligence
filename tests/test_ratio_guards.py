"""#300 / ADR 022: sanity guards on ROA/ROE (insurer arms, micro-institutions) and NPL (small or
atypical credit books). Guarded on read: the stored index keeps the raw values, every consumer
(feed, CSO/CRO panels, recs, fragility) sees the guarded one."""
import http.server
import threading
from pathlib import Path

import pytest

from src.ingest import bcb_fundamentals, bcb_inadimplencia
from src.synth import executive
from src.synth.ratio_guards import (OUT_OF_RANGE, SMALL, guard_fundamentals, guard_npl)

SITE = Path(__file__).resolve().parents[1] / "src" / "dashboard" / "site"

BANK = {"roe_pct": 20.9, "roa_pct": 1.71, "ativo_bi": 2900.0, "leverage": 12.1}
# An insurer through its bank arm: tiny asset base, plausible ROE, implausible ROA (live 2026-03).
INSURER_ARM = {"roe_pct": 33.4, "roa_pct": 28.26, "ativo_bi": 0.27, "leverage": 1.2}
MICRO = {"roe_pct": -211.7, "roa_pct": -87.63, "ativo_bi": 0.08, "leverage": 2.4}


def test_a_major_bank_is_untouched():
    assert guard_fundamentals(dict(BANK)) == BANK
    npl = {"npl_total": 1.98, "npl_pf": 2.4, "npl_pj": 1.1, "band": "baixa", "carteira_bi": 1100.0}
    assert guard_npl(dict(npl)) == npl


def test_insurer_arm_loses_its_roa_only():
    g = guard_fundamentals(dict(INSURER_ARM))
    assert g["roa_pct"] is None and g["roe_pct"] == 33.4
    assert g["guarded"] == {"roa_pct": OUT_OF_RANGE}


def test_a_micro_institution_loses_both_profit_ratios():
    g = guard_fundamentals(dict(MICRO))
    assert g["roa_pct"] is None and g["roe_pct"] is None
    assert g["guarded"] == {"roa_pct": SMALL, "roe_pct": SMALL}
    assert g["leverage"] == 2.4                      # a balance-sheet ratio, not a profit one


def test_an_implausible_roe_on_a_real_base():
    g = guard_fundamentals({"roe_pct": 101.9, "roa_pct": 9.0, "ativo_bi": 0.6})
    assert g["roe_pct"] is None and g["roa_pct"] == 9.0 and g["guarded"] == {"roe_pct": OUT_OF_RANGE}


def test_suppressed_values_are_not_published():
    """The feed is agent grounding: a raw outlier carried along would be quoted."""
    g = guard_fundamentals(dict(INSURER_ARM))
    assert 28.26 not in [v for v in g.values() if not isinstance(v, dict)]
    assert "raw" not in g


def test_guards_are_idempotent():
    once = guard_fundamentals(dict(MICRO))
    assert guard_fundamentals(once) == once
    n = guard_npl({"npl_total": 40.9, "npl_pf": 40.9, "band": "elevada", "carteira_bi": 0.06})
    assert guard_npl(n) == n


def test_small_credit_book_drops_npl_band_and_segments():
    g = guard_npl({"npl_total": 40.9, "npl_pf": 40.9, "npl_pj": None, "band": "elevada", "carteira_bi": 0.06})
    assert g["npl_total"] is None and g["band"] is None and g["npl_pf"] is None
    assert g["guarded"] == {"npl_total": SMALL}


def test_whole_book_overdue_is_an_artifact():
    g = guard_npl({"npl_total": 100.0, "npl_pj": 100.0, "band": "elevada", "carteira_bi": 0.37})
    assert g["npl_total"] is None and g["band"] is None and g["guarded"] == {"npl_total": OUT_OF_RANGE}


def test_a_high_but_real_npl_is_kept():
    """A subprime lender at 33% (15+ dias) on a R$ 7 bi book is a real signal, not an artifact."""
    rec = {"npl_total": 33.28, "npl_pf": 36.08, "npl_pj": 3.19, "band": "elevada", "carteira_bi": 6.96}
    assert guard_npl(dict(rec)) == rec


def test_an_implausible_segment_alone_is_dropped():
    g = guard_npl({"npl_total": 20.0, "npl_pf": 90.0, "npl_pj": 12.0, "band": "elevada", "carteira_bi": 2.0})
    assert g["npl_pf"] is None and g["npl_total"] == 20.0 and g["band"] == "elevada"
    assert g["guarded"] == {"npl_pf": OUT_OF_RANGE}


def test_readers_guard_but_the_stored_index_keeps_raw():
    idx = {"records": {"itau": dict(BANK), "sulamerica": dict(INSURER_ARM), "micro": dict(MICRO)}}
    out = bcb_fundamentals.fundamentals_by_entity(idx)
    assert out["itau"]["roa_pct"] == 1.71 and "guarded" not in out["itau"]
    assert out["sulamerica"]["roa_pct"] is None and out["micro"]["roe_pct"] is None
    assert idx["records"]["sulamerica"]["roa_pct"] == 28.26           # stored value unchanged
    nidx = {"records": {"sumup": {"npl_total": 40.9, "npl_pf": 40.9, "band": "elevada", "carteira_bi": 0.06}}}
    n = bcb_inadimplencia.npl_by_entity(nidx)
    assert n["sumup"]["npl_total"] is None and n["sumup"]["guarded"] == {"npl_total": SMALL}
    assert nidx["records"]["sumup"]["npl_total"] == 40.9


def _feed(fund, npl):
    ents = []
    for eid, f in fund.items():
        ents.append({"entity": eid, "label": eid.title(), "industries": ["bancos"],
                     "fundamentals": f, "inadimplencia": npl.get(eid)})
    return {"entities": ents}


def test_a_suppressed_value_drives_no_rec_and_no_ranking():
    fund = bcb_fundamentals.fundamentals_by_entity({"records": {
        "itau": dict(BANK), "micro": dict(MICRO), "sulamerica": dict(INSURER_ARM)}})
    rows = executive._fundamentals_rows(_feed(fund, {}))
    ids = [r["entity"] for r in rows]
    assert "micro" not in ids                             # its -211% ROE no longer leads any list
    sul = next(r for r in rows if r["entity"] == "sulamerica")
    assert sul["roa_pct"] is None and sul["guarded"] == {"roa_pct": OUT_OF_RANGE}
    npl = bcb_inadimplencia.npl_by_entity({"records": {
        "sumup": {"npl_total": 40.9, "npl_pf": 40.9, "band": "elevada", "carteira_bi": 0.06}}})
    assert npl["sumup"]["band"] is None                   # so no "Inadimplência elevada" rec fires


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(SITE), **k)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    try:
        browser = pw.chromium.launch()
    except Exception as exc:  # noqa: BLE001 - no browser installed here
        pw.stop()
        pytest.skip(f"Chromium not available: {str(exc)[:80]}")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    pg = browser.new_page()
    pg.route("**/api/**", lambda r: r.fulfill(status=503, content_type="application/json", body="{}"))
    pg.goto(f"http://127.0.0.1:{srv.server_address[1]}/v3/index.html")
    pg.wait_for_function("() => typeof rowsFinancials === 'function' && typeof rowsSolvency === 'function'")
    yield pg
    srv.shutdown()
    browser.close()
    pw.stop()


@pytest.mark.network
def test_panels_say_nd_with_the_reason(page):
    fin = page.evaluate("""() => rowsFinancials([{entity: "sulamerica", label: "SulAmérica",
        roe_pct: 33.4, roa_pct: null, guarded: {roa_pct: "fora_da_faixa"}}])""")
    assert "ROA n/d (fora da faixa)" in fin
    sol = page.evaluate("""() => rowsSolvency([{entity: "sumup", label: "SumUp", band: "adequada",
        indice_basileia: 30, npl_total: null, npl_guarded: {npl_total: "base_pequena"}}])""")
    assert "NPL n/d (base pequena)" in sol
    ok = page.evaluate("""() => rowsSolvency([{entity: "itau", label: "Itaú", band: "adequada",
        indice_basileia: 15, npl_total: 1.98, npl_pf: 2.4, npl_pj: null, npl_band: "baixa"}])""")
    assert "NPL 1.98%" in ok and "undefined" not in ok and "PJ null" not in ok
