import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import operatives


def test_institutional_names_are_not_persons():
    assert operatives._is_person_name("BTG PACTUAL SERVIÇOS FINANCEIROS S/A DTVM") is False
    assert operatives._is_person_name("ITAU UNIBANCO ASSET MANAGEMENT LTDA.") is False
    assert operatives._is_person_name("Mauá Capital Real Estate Ltda") is False
    assert operatives._is_person_name("Nubank") is False           # single token
    assert operatives._is_person_name("Maria da Silva Souza") is True
    assert operatives._is_person_name("João Pereira") is True


def test_norm_name_folds_accents_no_cpf():
    assert operatives._norm_name("José  Antônio") == "JOSE ANTONIO"


def test_person_needs_name_role_and_document():
    # missing document -> skipped
    sigs = [{"controllers": ["Maria da Silva Souza"], "entity": "acme"}]  # no id/url/source
    assert operatives.resolve_persons(sigs, run_date="2026-08-23")["persons"] == []
    # complete -> one pending person node
    sigs = [{"controllers": ["Maria da Silva Souza"], "entity": "acme", "id": "doc-1"}]
    r = operatives.resolve_persons(sigs, run_date="2026-08-23")
    assert len(r["persons"]) == 1
    p = r["persons"][0]
    assert p["status"] == "pending" and p["lgpd_scope"] == "public_professional_role"
    assert p["roles"] == ["controlador"] and p["entities"] == ["acme"]


def test_common_control_edge_when_person_bridges_two_entities():
    sigs = [
        {"controllers": ["Carlos Eduardo Lima"], "entity": "fintech_a", "id": "d1"},
        {"socios": ["Carlos Eduardo Lima"], "entity": "fintech_b", "id": "d2"},
    ]
    r = operatives.resolve_persons(sigs, run_date="2026-08-23")
    assert len(r["common_control"]) == 1
    edge = r["common_control"][0]
    assert set(edge["entities"]) == {"fintech_a", "fintech_b"}
    assert edge["via_person"] == "Carlos Eduardo Lima"
    assert edge["status"] == "pending"


def test_homonym_across_documents_is_flagged_ambiguous():
    sigs = [
        {"directors": ["João Pereira"], "entity": "a", "id": "d1"},
        {"directors": ["João Pereira"], "entity": "b", "id": "d2"},
    ]
    # bridges two entities but as 'diretor' (not a control role) -> ambiguous person, no control edge
    r = operatives.resolve_persons(sigs, run_date="2026-08-23")
    assert r["persons"][0]["ambiguous"] is True
    assert r["common_control"] == []


def test_review_queue_gated_to_control_relevant_people():
    # a lone statutory director of ONE entity is resolved but NOT queued; a control-role
    # sócio IS queued; the full persons graph still knows everyone.
    sigs = [
        {"directors": ["Ana Lima Costa"], "entity": "a", "id": "d1"},   # lone director
        {"socios": ["Bruno Alves Reis"], "entity": "a", "id": "d1"},    # control role
    ]
    r = operatives.resolve_persons(sigs, run_date="2026-08-23")
    assert len(r["persons"]) == 2                        # both resolved
    queued = {p["person"] for p in r["proposals"]}
    assert queued == {"Bruno Alves Reis"}               # only the control-role one


def test_bridging_person_is_queued_even_if_not_control_role():
    sigs = [
        {"directors": [{"name": "Ana Lima Costa", "doc_mask": "***265018**"}], "entity": "a", "id": "d1"},
        {"directors": [{"name": "Ana Lima Costa", "doc_mask": "***265018**"}], "entity": "b", "id": "d2"},
    ]
    r = operatives.resolve_persons(sigs, run_date="2026-08-23")
    assert {p["person"] for p in r["proposals"]} == {"Ana Lima Costa"}  # bridges -> queued


def test_masked_cpf_separates_homonyms():
    # same name, DIFFERENT masked CPFs -> two distinct people, NO false control edge
    sigs = [
        {"socios": [{"name": "João Pereira", "role": "sócio", "doc_mask": "***111111**"}],
         "entity": "a", "id": "d1"},
        {"socios": [{"name": "João Pereira", "role": "sócio", "doc_mask": "***222222**"}],
         "entity": "b", "id": "d2"},
    ]
    r = operatives.resolve_persons(sigs, run_date="2026-08-23")
    assert len(r["persons"]) == 2
    assert r["common_control"] == []                       # not the same person


def test_shared_masked_cpf_grounds_control_cohort():
    # same name AND same masked CPF across two entities -> resolved cohort of control
    sigs = [
        {"socios": [{"name": "João Pereira", "role": "sócio", "doc_mask": "***265018**"}],
         "entity": "a", "id": "d1"},
        {"socios": [{"name": "João Pereira", "role": "sócio", "doc_mask": "***265018**"}],
         "entity": "b", "id": "d2"},
    ]
    r = operatives.resolve_persons(sigs, run_date="2026-08-23")
    assert len(r["persons"]) == 1
    p = r["persons"][0]
    assert p["doc_mask"] == "***265018**" and p["ambiguous"] is False
    assert len(r["common_control"]) == 1
    edge = r["common_control"][0]
    assert edge["grounded"] is True and edge["doc_mask"] == "***265018**"
    assert "CPF" in edge["text"]


def test_person_node_never_carries_a_full_cpf():
    sigs = [{"socios": [{"name": "Maria Silva", "role": "sócio", "doc_mask": "***265018**"}],
             "entity": "a", "id": "d1"}]
    node = operatives.resolve_persons(sigs, run_date="2026-08-23")["persons"][0]
    import json
    blob = json.dumps(node)
    assert node["doc_mask"] == "***265018**" and blob.count(str(2)) >= 0  # masked only
    assert "*" in node["doc_mask"]                          # never a full 11-digit CPF


def test_merge_drops_stale_pending_keeps_decided():
    existing = [
        {"id": "person:a", "status": "pending"},      # no longer generated -> drop
        {"id": "person:b", "status": "approved"},     # decided -> keep for audit
        {"id": "person:c", "status": "pending"},       # regenerated -> keep
    ]
    fresh = [{"id": "person:c", "status": "pending"}, {"id": "person:d", "status": "pending"}]
    ids = {p["id"] for p in operatives._merge(existing, fresh)}
    assert ids == {"person:b", "person:c", "person:d"}


def test_institutional_fields_never_yield_persons():
    # admin/manager/leader are NOT scanned (institutional in this pipeline)
    sigs = [{"admin": "ITAU UNIBANCO S.A.", "manager": "BTG GESTORA LTDA", "id": "d1",
             "entity": "x"}]
    r = operatives.resolve_persons(sigs, run_date="2026-08-23")
    assert r["persons"] == []


def test_collect_signals_flattens_slices():
    digest = {"fatos": [{"id": "f1"}], "new_entrants": {"items": [{"id": "e1"}]},
              "macro": {"selic": 15.0}, "source": "x"}
    got = {s.get("id") for s in operatives._collect_signals(digest)}
    assert got == {"f1", "e1"}
