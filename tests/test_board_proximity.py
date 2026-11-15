"""#385 competitor proximity: deterministic, per-dimension, missing never imputed."""
import json
import math

import pytest

from src.board import proximity as P

CFG = P.load_config()


def bank(eid, assets=None, inds=("banking",), basel=None, lcr=None, npl=None, cc=None, cost=None, share=None):
    e = {"entity": eid, "label": eid.title(), "industries": list(inds), "market_share_pct": share}
    if assets is not None or cc is not None:
        e["fundamentals"] = {"ativo_bi": assets, "credito_captacoes_pct": cc, "roa_pct": 1.0}
    if basel is not None:
        e["soundness"] = {"indice_basileia": basel, "razao_alavancagem": 7.0}
    if lcr is not None:
        e["pilar3_km1"] = {"lcr_pct": lcr}
    if npl is not None:
        e["inadimplencia"] = {"npl_total": npl}
    if cost is not None:
        e["resultados"] = {"custo_credito_pct": cost, "opex_ativo_pct": 2.0}
    return e


FOCAL = bank("aurora", 200, basel=12.0, lcr=120, npl=5.0, cc=60, cost=7.0)
TWIN = bank("twin", 210, basel=12.5, lcr=125, npl=5.2, cc=62, cost=7.2)
GIANT = bank("giant", 20000, basel=16.0, lcr=190, npl=2.0, cc=95, cost=3.0)
ODD = bank("odd", 200, inds=("insurance",), basel=12.0, lcr=120, npl=5.0, cc=60, cost=7.0)
BARE = bank("bare", inds=("banking",))


def test_config_is_versioned_and_weights_cover_every_dimension():
    assert CFG["version"] and set(CFG["weights"]) == set(P.DIMENSIONS)
    assert math.isclose(sum(CFG["weights"].values()), 1.0)


def test_a_near_twin_is_closer_than_a_giant_on_every_likeness_dimension():
    t, g = P.score(FOCAL, TWIN), P.score(FOCAL, GIANT)
    assert t["score"] > g["score"]
    for d in ("size", "business_model", "prudential"):
        assert t["components"][d]["sim"] > g["components"][d]["sim"], d
    assert g["components"]["size"]["sim"] == 0.0          # two orders of magnitude apart
    assert g["components"]["size"]["ratio"] == 100.0


def test_market_overlap_is_industry_jaccard():
    assert P.score(FOCAL, ODD)["components"]["market"]["sim"] == 0.0
    both = bank("both", 200, inds=("banking", "insurance"))
    assert P.score(FOCAL, both)["components"]["market"]["sim"] == 0.5


def test_missing_dimensions_are_reported_not_imputed():
    r = P.score(FOCAL, BARE)
    assert set(r["missing"]) == {"size", "business_model", "prudential", "pressure"}
    assert r["coverage"] == CFG["weights"]["market"] and r["low_coverage"]
    assert r["score"] == 1.0                              # only market (identical) was measurable


def test_pressure_comes_from_the_threat_score():
    r = P.score(FOCAL, TWIN, peer_attrs={"threat_w3": {"mean": 80, "from": "a", "to": "b", "n": 3}})
    assert r["components"]["pressure"]["sim"] == 0.8 and "pressure" not in r["missing"]


def test_components_cite_the_pack_facts_they_used():
    r = P.score(FOCAL, TWIN)
    assert set(r["components"]["prudential"]["facts"]) >= {"f-soundness-aurora", "f-soundness-twin",
                                                            "f-km1-aurora", "f-km1-twin", "f-npl-twin"}
    assert "f-fundamentals-twin" in r["components"]["size"]["facts"]


def test_size_falls_back_to_market_share():
    a, b = bank("a", share=10.0), bank("b", share=1.0)
    s = P.score(a, b)["components"]["size"]
    assert s["sim"] == 0.5 and s["facts"] == []


def test_rank_orders_by_coverage_then_score_and_skips_unknown_ids():
    feed = {"entities": [FOCAL, TWIN, GIANT, ODD, BARE]}
    order = [r["peer"] for r in P.rank(feed, "aurora")]
    assert order[:2] == ["twin", "giant"] and order[-1] == "bare"   # low coverage last
    assert "odd" not in order                              # no shared industry
    assert [r["peer"] for r in P.rank(feed, "aurora", candidates=["odd", "ghost"])] == ["odd"]
    with pytest.raises(ValueError):
        P.rank(feed, "ghost")


def test_deterministic():
    feed = {"entities": [FOCAL, TWIN, GIANT, BARE]}
    assert json.dumps(P.rank(feed, "aurora")) == json.dumps(P.rank(feed, "aurora"))


def test_fact_text_names_components_missing_dimensions_and_low_coverage():
    f = P.fact(P.score(FOCAL, TWIN), "Aurora", "2026-11-15")
    assert f["id"] == "f-proximity-twin" and f["kind"] == "proximity"
    assert "porte" in f["text"] and "1.05× o porte de Aurora" in f["text"]
    assert f["source"].startswith("Onça (cálculo de proximidade") and f["data"]["inputs_facts"]
    low = P.fact(P.score(FOCAL, BARE), "Aurora")
    assert "Sem dados para: porte" in low["text"] and "Cobertura baixa" in low["text"]
