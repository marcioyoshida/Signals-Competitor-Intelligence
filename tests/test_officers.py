"""ADR 020 Phases 2–3 — the four officer personas + chief-of-staff router/hand-off."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import officers


def test_roster_has_four_officers_each_with_a_catalog():
    roster = officers.roster()
    assert {o["role"] for o in roster} == {"strategic", "regulator", "compliance", "product"}
    assert all(o["actions"] for o in roster)


def test_route_matches_domain_language():
    assert officers.route("O que a CVM mudou na resolução e qual o prazo?") == "regulator"
    assert officers.route("Alguma sanção CEIS ou distress na carteira?") == "compliance"
    assert officers.route("Onde estão nossos pontos cegos de cobertura?") == "product"
    assert officers.route("Onde o concorrente ganha terreno? Qual a tese?") == "strategic"


def test_route_falls_back_to_default_when_no_signal():
    assert officers.route("bom dia") == "product"


def test_owner_of_is_exclusive_only():
    # exclusively-owned actions resolve to their officer
    assert officers.owner_of("run_integrity_audit") == "compliance"
    assert officers.owner_of("curate_belief") == "strategic"
    assert officers.owner_of("propose_vertical") == "product"
    assert officers.owner_of("resolve_review") == "product"
    # shared actions have no single owner
    assert officers.owner_of("trigger_run") is None
    assert officers.owner_of("open_watch") is None
    # unknown action → None
    assert officers.owner_of("nonexistent") is None


def test_short_role_maps_to_csuite_id():
    assert officers.short_role("strategic") == "cso"
    assert officers.short_role("cco") == "cco"  # already short
    assert officers.short_role("regulator") == "cro"
    assert officers.short_role("nobody") is None


def test_catalog_and_persona_lookup():
    assert "curate_belief" in officers.catalog("strategic")
    assert officers.brief_persona("compliance").startswith("Você é o Oficial de compliance")
    assert officers.primary_lens("regulator") == "regulacao"
    assert officers.brief_persona("nobody") is None
    assert officers.is_officer("product") and not officers.is_officer("nobody")
