"""Product ingestion enrichment R1–R6 (grounded derivations)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import product_intel as pi


def test_r2_certifications_grounded_in_registry_facts():
    c = pi.derive_certifications("itau", {"industries": ["banking"], "ticker": "ITUB4",
                                          "esg": {"ise_b3": True}, "parent": None})
    labels = [x["label"] for x in c]
    assert "Autorização BCB" in labels and "Listada B3 (ITUB4)" in labels
    assert any("ISE B3" in x for x in labels)
    # a CVM-regulated fund with a parent → CVM authorization + subsidiary
    c2 = pi.derive_certifications("xpag11", {"industries": ["agri-funds"], "ticker": "XPAG11", "parent": "xp"})
    labels2 = [x["label"] for x in c2]
    assert "Autorização CVM" in labels2 and "Subsidiária de xp" in labels2


def test_r2_empty_for_untagged_entity():
    assert pi.derive_certifications("x", {"industries": []}) == []


def test_r6_firmographics_public_vs_private_and_size_band():
    pub = pi.derive_firmographics("itau", {"ticker": "ITUB4"}, 20)
    assert pub["listing"] == "pública" and pub["size_band"] == "grande" and pub["is_inference"]
    priv = pi.derive_firmographics("x", {"ownership": "private"}, 2)
    assert priv["listing"] == "privada" and priv["size_band"] == "pequena"


def test_r3_market_structure_from_financials_real_only():
    fin = [{"entity_id": "itau", "name": "ITAU", "revenue": 300}, {"entity_id": "bb", "name": "BB", "revenue": 100}]
    ea = {"itau": {"industries": ["banking"]}, "bb": {"industries": ["banking"]}}
    ms = pi.market_structure(fin, ea)
    b = ms["banking"]
    assert b["covered"] and b["issuers"] == 2 and b["size_revenue"] == 400
    assert b["leader"]["entity"] == "itau" and b["leader"]["rev_share"] == 0.75
    assert 0 < b["hhi"] <= 1
    # a sector with no listed issuer is simply absent (no fabricated size)
    assert "insurance" not in ms


def test_r4_pricing_proxy_labelled_inference():
    cards = [{"lenses": ["juros"], "industries": ["banking"], "date": "2026-09-04"},
             {"lenses": ["ofertas"], "industries": ["banking"], "date": "2026-09-04"}]
    pr = pi.pricing_signals(cards, {"2026-09-04"})
    assert pr["banking"]["is_inference"] and pr["banking"]["signals"] == 2 and pr["banking"]["recent"] == 2


def test_r5_source_health_freshness_bands():
    cards = [{"lenses": ["news"], "date": "2026-09-04"}, {"lenses": ["news"], "date": "2026-09-01"},
             {"lenses": ["dou"], "date": "2026-08-01"}]
    sh = {s["lens"]: s for s in pi.source_health(cards, "2026-09-04")}
    assert sh["news"]["docs"] == 2 and sh["news"]["staleness_days"] == 0 and sh["news"]["band"] == "ok"
    assert sh["dou"]["band"] == "stale"


def test_enrich_feed_wires_all_blocks():
    feed = {"as_of": "2026-09-04", "financials": [{"entity_id": "itau", "name": "ITAU", "revenue": 100}],
            "entity_attrs": {"itau": {"industries": ["banking"], "ticker": "ITUB4", "ownership": "public"}},
            "feed": [{"entity": "itau", "lenses": ["juros"], "industries": ["banking"], "date": "2026-09-04"}]}
    pi.enrich_feed(feed)
    assert feed["entity_attrs"]["itau"]["certifications"]  # R2
    assert feed["entity_attrs"]["itau"]["firmographics"]["listing"] == "pública"  # R6
    assert feed["source_health"] and "banking" in feed["market_structure"] and "banking" in feed["pricing"]
