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


def test_private_fintech_aliases_resolve():
    from src.synth.entities import primary_entity
    cases = {
        "CREDITAS SOCIEDADE DE CRÉDITO DIRETO S.A.": "creditas",
        "RECARGAPAY INSTITUICAO DE PAGAMENTO LTDA.": "recargapay",
        "CLOUDWALK INSTITUIÇÃO DE PAGAMENTO E SERVICOS LTDA": "infinitepay",
        "NEON PAGAMENTOS S.A. - INSTITUIÇÃO DE PAGAMENTO": "neon",
        "STONE INSTITUIÇÃO DE PAGAMENTO S.A.": "stone",
    }
    for name, expected in cases.items():
        assert primary_entity({"name": name}) == expected, name


def test_stone_alias_excludes_unrelated_stonex():
    from src.synth.entities import resolve_entities
    # StoneX Group is a different company — must not resolve to StoneCo
    assert "stone" not in resolve_entities({"name": "BANCO STONEX S.A."})
    assert "stone" not in resolve_entities({"institution": "STONEX DISTRIBUIDORA DE TVM LTDA."})


# --- Deeper fix regression harness: anchored, context-gated resolution ---

def test_word_boundary_kills_substring_false_positives():
    from src.synth.entities import resolve_entities
    # STONE must not match inside STONEX / LIMESTONE (right/left boundary)
    assert "stone" not in resolve_entities({"institution": "STONEX DTVM LTDA."})
    assert "stone" not in resolve_entities({"subject": "estudo sobre limestone mining"})


def test_ambiguous_token_dropped_in_free_text_news():
    from src.synth.entities import resolve_entities
    # the live bug: "Rolling Stone" music headline (source=News) must NOT cluster
    music = {"source": "News", "kind": "competitor", "company": "Stone", "name": "Stone",
             "title": "Blue Note SP recebe 'Rolling Stone Sessions' e 60 anos do MPB4"}
    assert "stone" not in resolve_entities(music)
    # bare common words in news never resolve their brand
    assert "caixa" not in resolve_entities({"source": "News", "title": "como organizar o fluxo de caixa"})
    assert "nubank" not in resolve_entities({"source": "News", "title": "homem nu detido em protesto"})


def test_strong_and_distinct_aliases_resolve_even_in_news():
    from src.synth.entities import resolve_entities
    # a ticker mention is authoritative
    assert "stone" in resolve_entities({"source": "News", "title": "StoneCo (STNE) reporta lucro"})
    # a distinctive alias resolves anywhere
    assert "nubank" in resolve_entities({"source": "News", "title": "Nubank lança conta global"})
    assert "caixa" in resolve_entities({"source": "News", "title": "Caixa Econômica anuncia crédito"})
    assert "stone" in resolve_entities({"source": "News", "title": "StoneCo expande maquininhas"})


def test_ambiguous_token_accepted_in_structured_identity_source():
    from src.synth.entities import resolve_entities, primary_entity
    # a bare Stone in a BCB/entrant institution field IS an identity assertion
    assert primary_entity({"name": "STONE INSTITUIÇÃO DE PAGAMENTO S.A."}) == "stone"
    # SEC ticker field is strong regardless of source
    assert "stone" in resolve_entities({"ticker": "STNE", "company": "StoneCo Ltd.", "form": "6-K"})


def test_parent_link_via_cloudwalk_controller():
    from src.synth.entities import known_parents
    e = {"name": "NOVA SCD S.A.", "controllers": ["CLOUDWALK FINANCEIRA S.A."]}
    assert known_parents(e) == ["infinitepay"]


def test_resolve_uses_registry_when_configured(monkeypatch):
    from src.synth import entities, entity_registry
    monkeypatch.setenv("ONCA_ENTITIES_TABLE", "t")
    monkeypatch.setattr(entity_registry, "load_alias_map", lambda *a, **k: {"acme": ["ACME BANK"]})
    # a registry-only entity resolves with NO change to ENTITY_ALIASES / code
    assert entities.resolve_entities({"name": "ACME BANK S.A."}) == ["acme"]
    # and the registry map is authoritative when present
    assert entities.resolve_entities({"name": "NUBANK"}) == []


def test_resolve_falls_back_to_builtin_without_registry(monkeypatch):
    from src.synth import entities
    monkeypatch.delenv("ONCA_ENTITIES_TABLE", raising=False)
    assert "nubank" in entities.resolve_entities({"name": "NUBANK PAGAMENTOS"})
