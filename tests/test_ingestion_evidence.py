"""ADR 014 step 3: the evidence-backed ingestion_gap detector."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.synth import coverage as cov
from src.synth import ingestion_evidence as IE
from src.synth import source_coverage

FEED = {
    "entity_attrs": {
        "itau": {"label": "Itaú", "ownership": "private",
                 "certifications": [{"label": "Autorização CVM", "source": "CVM"}],
                 "esg": {"ise_b3": True}},
        "nubank": {"label": "Nubank", "ownership": "private",
                   "certifications": ["BCB · Instituição de Pagamento · em funcionamento"]},
    },
    "entities": [{"entity": "itau", "soundness": {"basel": 15.1}}, {"entity": "nubank"}],
    "reputation": [{"entity": "nubank", "source": "reclame_aqui"}],
    "financials": [{"entity_id": "itau", "revenue": 1}],
    "distress": [],
    "enforcement": [{"entity": "xp", "target": "xp", "kind": "multa"}],
}


def _cls(q, entities=None, feed=FEED):
    return IE.assess(q, feed, entities)


def test_data_the_feed_holds_is_a_retrieval_gap_not_an_ingestion_gap():
    for q in ("quantas reclamações no Reclame Aqui sobre bancos digitais?",
              "qual o faturamento do Itaú?", "quais bancos têm rating ESG?",
              "quais instituições são autorizadas pelo BCB?"):
        assert _cls(q)["class"] == "retrieval_gap", q


def test_what_no_source_carries_is_an_ingestion_gap_with_the_reason():
    a = _cls("quais bancos têm certificação ISO 27001?")
    assert a["class"] == "ingestion_gap"
    fams = {e["family"]: e for e in a["families"]}
    assert fams["third_party_cert"]["verdict"] == "ingestion_gap"          # licences are not ISO
    assert fams["authorization"]["verdict"] == "retrieval_gap"
    assert "autorizações regulatórias" in a["recommendation"] and "AUTO_CODEGEN" in a["recommendation"]
    for q in ("quantos funcionários tem o Nubank?", "qual o rating da Fitch do Bradesco?"):
        assert _cls(q)["class"] == "ingestion_gap", q
    social = _cls("o que dizem no LinkedIn sobre o Inter?")
    assert social["class"] == "ingestion_gap"
    assert social["families"][0]["sources"][0] == {"id": "x_twitter", "label": "X / Twitter", "status": "planned"}


def test_a_live_source_that_misses_the_entity_is_a_coverage_gap():
    a = _cls("qual o índice de Basileia do Nubank?", ["nubank"])
    assert a["class"] == "coverage_gap"
    assert a["families"][0]["entities"] == {"nubank": False}
    assert "sem cobertura para nubank" in a["recommendation"]
    assert _cls("qual o índice de Basileia do Itaú?", ["itau"])["class"] == "retrieval_gap"
    # a live source that holds nothing at all right now (distress after #296)
    d = _cls("quais empresas estão em recuperação judicial?")
    assert d["class"] == "coverage_gap" and d["families"][0]["rows"] == 0


def test_rating_esg_asks_for_esg_not_a_credit_rating():
    assert [e["family"] for e in _cls("quais bancos têm rating ESG?")["families"]] == ["esg"]
    assert [e["family"] for e in _cls("rating de crédito do BB?")["families"]] == ["credit_rating"]


def test_no_family_means_no_verdict_and_no_feed_means_none():
    assert _cls("o que mudou no Pix?") is None
    assert IE.assess("faturamento do Itaú?", None) is None
    assert IE.assess("faturamento do Itaú?", {}) is None


def test_every_family_source_is_on_the_fontes_roadmap():
    ids = {r["id"] for r in source_coverage.ROADMAP}
    for f in IE.FAMILIES:
        assert set(f.sources) <= ids, (f.id, set(f.sources) - ids)


def test_triage_uses_the_evidence_when_it_has_the_feed():
    q = "quantas reclamações no Reclame Aqui sobre bancos digitais?"
    assert cov.triage(q)["class"] == "ingestion_gap"                         # the stale keyword verdict
    t = cov.triage(q, feed=FEED)
    assert t["class"] == "retrieval_gap" and t["auto_fixable"] is False
    assert t["evidence"][0]["family"] == "complaints" and t["evidence"][0]["rows"] == 1
    # a curation gap (known entity + ownership cue) keeps priority: it is auto-fixable
    t = cov.triage("o Itaú é público ou privado?", resolver=lambda item: ["itau"], feed=FEED)
    assert t["class"] == "curation_gap" and t["auto_fixable"] is True
    # no family: the old path still decides
    assert cov.triage("o que mudou no Pix?", feed=FEED)["class"] == "retrieval_gap"
    assert "evidence" not in cov.triage("o que mudou no Pix?", feed=FEED)


def test_scheduled_pass_and_remediate_pass_the_feed_to_triage():
    from tests.test_coverage import FakeS3
    import datetime as dt
    import json
    s3 = FakeS3()
    q = "quantas reclamações no Reclame Aqui sobre bancos digitais?"
    cov.record(q, "b", s3=s3, today=dt.date(2026, 10, 1))
    cov.scheduled_pass("b", feed=FEED, s3=s3, today=dt.date(2026, 11, 4))
    rec = json.loads(s3.store[cov.INDEX_KEY])["records"][cov.gap_id(q)]
    assert rec["triage"]["class"] == "retrieval_gap" and rec["triage"]["evidence"]
    cov.remediate("b", feed=FEED, issuer=lambda r, t: None, s3=s3, today=dt.date(2026, 11, 5))
    rec = json.loads(s3.store[cov.INDEX_KEY])["records"][cov.gap_id(q)]
    assert rec["triage"]["class"] == "retrieval_gap"


def test_gaps_api_hands_the_feed_to_both_paths(monkeypatch):
    from src.dashboard import agent_ask, gaps_api
    monkeypatch.setenv("ONCA_DIGESTS_BUCKET", "b")
    monkeypatch.setenv("ONCA_SITE_BUCKET", "site")
    monkeypatch.setattr(agent_ask, "_load_feed", lambda site: FEED)
    got = {}
    monkeypatch.setattr(cov, "scheduled_pass", lambda bucket, **kw: got.update(kw) or {})
    gaps_api.run_scheduled()
    assert got["feed"] is FEED
