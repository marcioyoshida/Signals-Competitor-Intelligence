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
