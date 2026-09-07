"""ADR 018 Phase 3 — integrity audit detectors."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import integrity


def test_institution_leaf_pollution_flagged_only_from_automated_source():
    ents = [
        {"entity_id": "btg", "industries": ["investment-banking", "agri-funds"],
         "_prov": {"industries": {"source": "enrich"}}},                       # polluted
        {"entity_id": "bb", "industries": ["banking", "agri-funds"],
         "_prov": {"industries": {"source": "curated"}}},                      # curated -> NOT a finding
        {"entity_id": "fund", "industries": ["agri-funds"]},                   # pure fund -> ok
    ]
    kinds = {f["kind"]: f for f in integrity.audit_registry(ents)}
    assert kinds["institution_leaf_pollution"]["entity_id"] == "btg"
    assert kinds["institution_leaf_pollution"]["safe_fix"] is True
    assert not any(f["entity_id"] == "bb" for f in integrity.audit_registry(ents))


def test_fund_alias_and_unbacked_and_inversion():
    ents = [
        {"entity_id": "cap", "industries": ["asset-management"],
         "ticker": "CPAC11", "aliases": ["CAP", "CPTA11"]},                    # fund alias
        {"entity_id": "x", "industries": ["banking"], "confidence": "cnpj", "cnpj_roots": []},
        {"entity_id": "kinea", "industries": ["agri-funds"]},
        {"entity_id": "kfund", "industries": ["agri-funds"], "parent": "kinea"},  # parent is leaf
    ]
    kinds = {f["kind"] for f in integrity.audit_registry(ents)}
    assert {"fund_alias_on_institution", "unbacked_cnpj", "parent_inversion"} <= kinds


def test_card_primary_absent():
    ents = [{"entity_id": "sportingbet", "aliases": ["SPORTINGBET"], "display_name": "Sportingbet"},
            {"entity_id": "stone", "aliases": ["STONECO", "STONE"], "display_name": "Stone"}]
    feed = {"feed": [
        {"id": "c1", "kind": "competitor:news", "entity": "sportingbet",
         "narrative": "A STONECO divulgou resultados no SEC."},               # primary absent, names stone
        {"id": "c2", "kind": "competitor:news", "entity": "stone",
         "narrative": "A Stone reportou lucro."},                             # primary present -> ok
    ]}
    findings = integrity.audit_feed(feed, ents)
    ids = {f["card_id"] for f in findings}
    assert "c1" in ids and "c2" not in ids


def test_audit_sorts_by_severity_and_counts():
    ents = [{"entity_id": "btg", "industries": ["investment-banking", "agri-funds"],
             "_prov": {"industries": {"source": "discovery"}}}]
    rep = integrity.audit({"feed": []}, ents)
    assert rep["total"] == 1 and rep["counts"]["institution_leaf_pollution"] == 1
    assert rep["findings"][0]["severity"] == "high"


# --- #106 (#14 Stage 5): ingestion follow-up probe -------------------------------
def _iso_days_ago(n):
    import datetime as dt
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=n)).isoformat(timespec="seconds")


def test_surfacing_structural_no_path():
    """news off + no CNPJ + no filing term ⇒ an entity that can never surface."""
    ents = [
        {"entity_id": "ghost", "confidence": "structured", "news_search": False,
         "cnpj_roots": [], "industries": ["insurance"]},
        # has a CNPJ path → not structural
        {"entity_id": "ok", "confidence": "structured", "news_search": False,
         "cnpj_roots": ["12345678"], "industries": ["insurance"]},
    ]
    finds = integrity.audit_surfacing({"feed": [], "entities": []}, ents)
    kinds = {f["entity_id"]: f["kind"] for f in finds}
    assert kinds.get("ghost") == "entity_no_surface_path"
    assert "ok" not in kinds  # a CNPJ join is a viable path


def test_surfacing_coverage_gap_cohort_and_fund_exclusion():
    """A big aged non-fund cohort that never surfaces → ONE industry coverage-gap finding;
    a quiet fund cohort is not flagged; too-new entities don't count."""
    ents = [{"entity_id": f"ins{i}", "confidence": "structured", "industries": ["insurance"],
             "cnpj_roots": [str(i)], "created_at": _iso_days_ago(40)} for i in range(10)]
    ents += [{"entity_id": f"fii{i}", "confidence": "cnpj", "industries": ["real-estate-funds"],
              "cnpj_roots": [str(100 + i)], "created_at": _iso_days_ago(40)} for i in range(10)]
    ents += [{"entity_id": f"new{i}", "confidence": "structured", "industries": ["insurance"],
              "cnpj_roots": [str(200 + i)], "created_at": _iso_days_ago(3)} for i in range(5)]
    finds = integrity.audit_surfacing({"feed": [], "entities": []}, ents)
    gaps = [f for f in finds if f["kind"] == "industry_not_surfacing"]
    assert len(gaps) == 1 and gaps[0]["entity_id"] == "industry:insurance"
    assert gaps[0]["severity"] == "med"  # 10/10 quiet


def test_surfacing_uses_durable_created_at_over_prov():
    """created_at (set-once) wins over provenance set_at (which moves on re-put)."""
    e = {"entity_id": "x", "confidence": "cnpj", "created_at": _iso_days_ago(40),
         "_prov": {"industries": {"set_at": _iso_days_ago(1)}}}  # freshly re-stamped
    import datetime as dt
    got = integrity._created_at(e)
    assert (dt.datetime.now(dt.timezone.utc) - got).days >= 39


def test_surfacing_excludes_surfaced_and_curated():
    ents = [{"entity_id": f"seen{i}", "confidence": "cnpj", "industries": ["banking"],
             "cnpj_roots": [str(i)], "created_at": _iso_days_ago(40)} for i in range(10)]
    ents.append({"entity_id": "curated", "confidence": "curated", "news_search": False,
                 "cnpj_roots": [], "industries": ["banking"]})  # curated → excluded entirely
    feed = {"feed": [{"entity": f"seen{i}", "entities": []} for i in range(10)], "entities": []}
    finds = integrity.audit_surfacing(feed, ents)
    assert finds == []  # all seen surfaced; curated is not auto-discovered
