"""CVM financials (issue #7): matching + derived metrics, no network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import cvm_financials as cf


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
