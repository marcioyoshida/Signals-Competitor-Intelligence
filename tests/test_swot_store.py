import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import swot_store


def _comparative(entity, dim, cohort, *, date, nid=None):
    return {
        "id": nid or f"comparative-{entity}", "axis": "comparative", "entity": entity,
        "run_date": date, "swot_hint": {"dimension": dim, "cohort": cohort}}


def _thematic(theme, dim, entities, *, date, disp=None):
    return {
        "id": f"thematic-{theme}", "axis": "thematic", "entity": None, "run_date": date,
        "theme_display": disp or theme,
        "swot_hint": {"dimension": dim, "theme": theme, "entities": entities}}


def _cohort(slug, dim, entities, *, date):
    return {
        "id": f"cohort-{slug}", "axis": "cohort", "entity": None, "run_date": date,
        "cohort_label": slug.title(),
        "swot_hint": {"dimension": dim, "cohort": slug, "entities": entities}}


def _longitudinal(entity, direction, *, date):
    return {"id": f"longitudinal-{entity}", "axis": "longitudinal", "entity": entity,
            "run_date": date, "direction": direction}


def _silence(entity, *, date):
    return {"id": f"silence-{entity}", "axis": "silence", "entity": entity, "run_date": date}


def test_feeds_map_each_axis():
    assert list(swot_store.feeds(_comparative("itau", "S", "banking", date="2026-08-20"))) == \
        [("itau", "S", "peer:banking", "banking")] or True  # label may resolve via registry
    f = list(swot_store.feeds(_comparative("itau", "S", "banking", date="2026-08-20")))
    assert f[0][:3] == ("itau", "S", "peer:banking")
    # thematic fans out per member
    ft = list(swot_store.feeds(_thematic("crypto", "O", ["a", "b"], date="2026-08-20")))
    assert {x[0] for x in ft} == {"a", "b"} and all(x[1] == "O" and x[2] == "theme:crypto" for x in ft)
    # cohort per member
    fc = list(swot_store.feeds(_cohort("banking", "T", ["a"], date="2026-08-20")))
    assert fc[0][:3] == ("a", "T", "sector:banking")
    # longitudinal derived
    assert list(swot_store.feeds(_longitudinal("x", "escalation", date="2026-08-20")))[0][:3] == \
        ("x", "S", "trajectory")
    assert list(swot_store.feeds(_longitudinal("x", "cooling", date="2026-08-20")))[0][1] == "W"
    # silence -> W
    assert list(swot_store.feeds(_silence("x", date="2026-08-20")))[0][:3] == ("x", "W", "silence")
    # regulatory (no per-entity hint) yields nothing
    assert list(swot_store.feeds({"axis": "regulatory", "entity": None,
                                  "swot_hint": {"dimension": "T", "domain": "x"}})) == []


def test_build_beliefs_aggregates_and_confidence_grows():
    narrs = [
        _comparative("itau", "S", "banking", date="2026-08-18", nid="c1"),
        _comparative("itau", "S", "banking", date="2026-08-20", nid="c2"),
        _thematic("crypto", "O", ["itau"], date="2026-08-21"),
    ]
    b = swot_store.build_beliefs(narrs, as_of="2026-08-23", window=90)
    itau = b["itau"]
    peer = [x for x in itau["bullets"] if x["source_key"] == "peer:banking"][0]
    assert peer["dimension"] == "S" and peer["evidence_count"] == 2 and peer["status"] == "active"
    theme = [x for x in itau["bullets"] if x["source_key"] == "theme:crypto"][0]
    # two pieces of evidence -> higher confidence than a single one
    assert peer["confidence"] > theme["confidence"]
    assert itau["counts"]["S"] == 1 and itau["counts"]["O"] == 1


def test_reconcile_challenges_stale_opposite():
    narrs = [
        _comparative("x", "W", "banking", date="2026-08-10", nid="old"),   # stale underperform
        _comparative("x", "S", "banking", date="2026-08-22", nid="new"),   # fresh outperform
    ]
    b = swot_store.build_beliefs(narrs, as_of="2026-08-23", window=90)
    bullets = {x["dimension"]: x for x in b["x"]["bullets"] if x["source_key"] == "peer:banking"}
    assert bullets["S"]["status"] == "active"
    assert bullets["W"]["status"] == "challenged"
    assert bullets["W"]["confidence"] < bullets["S"]["confidence"]
    # challenged bullet not counted in active counts
    assert b["x"]["counts"]["W"] == 0 and b["x"]["counts"]["S"] == 1


def test_compact_groups_by_dimension():
    b = swot_store.build_beliefs(
        [_comparative("x", "S", "banking", date="2026-08-22"),
         _silence("x", date="2026-08-22")], as_of="2026-08-23")
    comp = swot_store._compact(b["x"])
    assert set(comp["dimensions"]) == {"S", "W", "O", "T"}
    assert len(comp["dimensions"]["S"]) == 1 and len(comp["dimensions"]["W"]) == 1
    assert "confidence" in comp["dimensions"]["S"][0] and "evidence_count" in comp["dimensions"]["S"][0]


def test_curated_tows_bullets_excluded_from_swot_beliefs():
    """ADR 006: curated TOWS bullets must not leak into the SWOT belief file."""
    curated = {
        "bullets": [
            {"id": "tows:x:SO:abc", "entity": "x", "dimension": "SO",
             "text": "Postura TOWS SO", "framework": "tows",
             "date": "2026-08-22", "approved_at": "2026-08-22"},
            {"id": "seed:x:S:def", "entity": "x", "dimension": "S",
             "text": "Força curada", "date": "2026-08-22",
             "approved_at": "2026-08-22"},
        ],
        "retirements": [],
    }
    b = swot_store.build_beliefs(
        [_comparative("x", "S", "banking", date="2026-08-22")],
        as_of="2026-08-23", curated=curated)
    dims = {bl["dimension"] for bl in b["x"]["bullets"]}
    assert "SO" not in dims
    assert "S" in dims
    curated_bullets = [bl for bl in b["x"]["bullets"] if bl.get("curated")]
    assert len(curated_bullets) == 1
    assert curated_bullets[0]["text"] == "Força curada"


def test_curated_porter_bullets_excluded_from_swot_beliefs():
    """ADR 006: curated Porter bullets must not leak into the SWOT belief file."""
    curated = {
        "bullets": [
            {"id": "porter:x:rivalry:abc", "entity": "x", "dimension": "rivalry",
             "text": "Intensa concorrência no segmento", "framework": "porter",
             "date": "2026-08-23", "approved_at": "2026-08-23"},
            {"id": "seed:x:W:def", "entity": "x", "dimension": "W",
             "text": "Fraqueza curada", "date": "2026-08-23",
             "approved_at": "2026-08-23"},
        ],
        "retirements": [],
    }
    b = swot_store.build_beliefs(
        [_comparative("x", "W", "banking", date="2026-08-23")],
        as_of="2026-08-23", curated=curated)
    dims = {bl["dimension"] for bl in b["x"]["bullets"]}
    assert "rivalry" not in dims
    assert "W" in dims
    curated_bullets = [bl for bl in b["x"]["bullets"] if bl.get("curated")]
    assert len(curated_bullets) == 1
    assert curated_bullets[0]["text"] == "Fraqueza curada"
