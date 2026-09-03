"""#2 CVM/BCB coverage scan + #14 radar-score."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import reg_coverage as C
from src.ingest import registry as R


def test_every_segment_maps_to_real_taxonomy_and_real_sources():
    src_ids = {s.id for s in R.SOURCES}
    for seg in C.SEGMENTS:
        assert seg.regulator in ("BCB", "CVM")
        assert seg.industries, f"{seg.key} has no industry"
        for ind in seg.industries:
            assert ind in C.INDUSTRY_SLUGS, f"{seg.key}: unknown industry {ind}"
        for sid in seg.signal_sources:
            assert sid in src_ids, f"{seg.key}: unknown source {sid}"
        if seg.entity_sync is not None:
            assert seg.entity_sync in src_ids, f"{seg.key}: unknown sync {seg.entity_sync}"


def test_coverage_report_classifies_and_surfaces_sync_roadmap():
    # a controlled 'active' set so the test is deterministic (not tied to live gating)
    active = {"regulatory", "dou", "news", "fatos", "new_entrants", "pix_moves",
              "competitor", "ofertas", "fiagro_moves"}
    r = C.coverage_report(active)
    keys = lambda rows: {x["key"] for x in rows}
    # the four roster-synced segments
    assert keys(r["entity_covered"]) == {"consorcio", "fundos", "fii", "fiagro"}
    # a signal-only segment we see but don't roster-sync
    assert "bancos" in keys(r["signal_only"])
    assert r["summary"]["segments"] == len(C.SEGMENTS)
    # the sync roadmap = everything not entity-covered (the #14 work)
    assert set(r["summary"]["sync_roadmap"]) == keys(r["signal_only"]) | keys(r["gap"])


def test_a_segment_with_no_live_source_is_a_gap():
    # if NOTHING runs, every segment is a gap (proves the gap branch)
    r = C.coverage_report(active_source_ids=set())
    assert r["summary"]["gap"] == len(C.SEGMENTS)
    assert r["summary"]["entity_covered"] == 0


def test_radar_score_orders_official_above_news():
    assert C.radar_score("fixture")["score"] > C.radar_score("discovery")["score"]
    assert C.radar_score("discovery")["score"] > C.radar_score("cnpj")["score"]
    assert C.radar_score("cnpj")["score"] > C.radar_score("enrich")["score"]
    assert C.radar_score(None)["tier"] == "unknown"
    assert C.radar_score("fixture")["tier"] == "official"


def test_entity_radar_takes_strongest_provenance():
    ent = {"confidence": "cnpj", "_prov": {
        "industries": {"source": "seed", "confidence": "fixture"},
        "ticker": {"source": "enrich", "confidence": "enrich"}}}
    assert C.entity_radar(ent)["tier"] == "official"   # fixture beats cnpj/enrich
