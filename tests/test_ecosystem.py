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


def test_contagion_carries_citation_and_card_is_grounded():
    # #37: a dict incident threads the triggering signal's citations/event through
    # to the card — no more uncited "incidente em X" inference.
    hubs = {"ITAU UNIBANCO S.A.": {"hub": "ITAU UNIBANCO S.A.",
            "dependents": ["fundo_a", "fundo_b"], "kind": "fund_admin", "n_dependents": 2}}
    inc = {"ITAU UNIBANCO S.A.": {"severity": 0.72,
           "citations": [{"url": "https://x/y"}], "source_ids": ["news:abc"],
           "event": "Itaú Unibanco enfrenta falha operacional em serviço de custódia."}}
    out = ecosystem.contagion(hubs, inc)
    assert len(out) == 1 and out[0]["citations"] == [{"url": "https://x/y"}]
    card = ecosystem.build_card(out[0])
    assert card["citations"] == [{"url": "https://x/y"}]          # grounded
    assert card["source_ids"] == ["news:abc"]
    assert "incidente em" not in card["narrative"]                # no overclaim
    assert "fundos sob sua administração" in card["narrative"]    # readable label
    assert "falha operacional" in card["narrative"]               # references the real signal
    assert "não fato confirmado" in card["narrative"]             # still labeled inference


def test_derived_axis_registered():
    from src.synth import feature_store
    assert "ecosystem" in feature_store.DERIVED_AXES
