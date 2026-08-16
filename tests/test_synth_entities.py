import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth.entities import primary_entity, resolve_entities


def test_resolve_sec_ticker_nu():
    item = {"ticker": "NU", "company": "Nu Holdings Ltd.", "form": "6-K"}
    assert "nubank" in resolve_entities(item)


def test_resolve_oferta_btg_leader():
    item = {"leader": "BTG PACTUAL SERVIÇOS FINANCEIROS", "issuer": "ACME"}
    assert primary_entity(item) == "btg"


def test_resolve_regulatory_pix_no_false_entity():
    item = {"subject": "Regulamento geral de Pix", "doc_type": "Resolução"}
    # should not invent a bank entity from generic pix text alone
    assert primary_entity(item) is None or primary_entity(item) not in ("bb",)


def test_known_parents_links_entrant_to_controller():
    from src.synth.entities import known_parents, resolve_entities
    # a new SCD controlled by a known player links to that parent
    e = {"_lens": "entrants", "name": "FFCRED SOCIEDADE DE CRÉDITO DIRETO S.A.",
         "controllers": ["NU HOLDINGS LTDA", "JOAO DA SILVA"]}
    assert known_parents(e) == ["nubank"]
    assert "nubank" in resolve_entities(e)  # clusters into Nubank

    # individual-only controllers -> no known parent
    assert known_parents({"controllers": ["JOAO DA SILVA", "MARIA SOUZA"]}) == []


def test_known_parents_excludes_self_link():
    from src.synth.entities import known_parents
    # controller string matches the entrant's own name -> not a "parent"
    e = {"name": "BTG PACTUAL SERVICOS FINANCEIROS", "controllers": ["BTG PACTUAL HOLDING"]}
    assert known_parents(e) == []
