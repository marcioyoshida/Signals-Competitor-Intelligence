import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import ecosystem


def test_build_dependency_graph_from_admin_proxy():
    sigs = [
        {"fund_name": "Fundo A", "admin": "BTG DTVM"},
        {"fund_name": "Fundo B", "admin": "BTG DTVM"},
        {"fund_name": "Fundo C", "manager": "Itaú Asset"},
    ]
    hubs = ecosystem.build_dependency_graph(sigs)
    assert hubs["BTG DTVM"]["n_dependents"] == 2
    assert hubs["BTG DTVM"]["dependents"] == ["Fundo A", "Fundo B"]
    assert hubs["Itaú Asset"]["kind"] == "fund_manager"


def test_external_edges_merge():
    ext = [{"hub": "AWS", "kind": "cloud", "dependents": ["nubank", "inter"]}]
    hubs = ecosystem.build_dependency_graph([], ext)
    assert hubs["AWS"]["dependents"] == ["inter", "nubank"]
    assert hubs["AWS"]["kind"] == "cloud"


def test_contagion_requires_incident_and_severity():
    hubs = {"AWS": {"hub": "AWS", "dependents": ["nubank", "inter"], "kind": "cloud",
                    "n_dependents": 2}}
    # no incident -> nothing propagates (source/incident gated)
    assert ecosystem.contagion(hubs, {}) == []
    # low severity -> ignored
    assert ecosystem.contagion(hubs, {"AWS": 0.2}) == []
    # real incident -> exposure finding
    out = ecosystem.contagion(hubs, {"AWS": 0.8})
    assert len(out) == 1 and out[0]["n_dependents"] == 2
    assert set(out[0]["dependents"]) == {"inter", "nubank"}


def test_contagion_skips_hub_without_dependents():
    hubs = {"X": {"hub": "X", "dependents": [], "kind": "cloud", "n_dependents": 0}}
    assert ecosystem.contagion(hubs, {"X": 0.9}) == []


def test_build_card_labeled_inference():
    card = ecosystem.build_card({"hub": "AWS", "severity": 0.8,
                                 "dependents": ["nubank", "inter"], "n_dependents": 2,
                                 "kind": "cloud"})
    assert card["axis"] == "ecosystem" and card["is_inference"] is True
    assert card["subject_type"] == "hub"
    assert "não fato confirmado" in card["narrative"]


def test_derived_axis_registered():
    from src.synth import feature_store
    assert "ecosystem" in feature_store.DERIVED_AXES
