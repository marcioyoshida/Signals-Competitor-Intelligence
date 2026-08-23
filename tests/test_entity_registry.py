import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import entity_registry as er


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[Item["pk"]] = Item

    def get_item(self, Key):
        it = self.items.get(Key["pk"])
        return {"Item": it} if it is not None else {}

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


def test_normalize_alias_folds_accents_and_case():
    assert er.normalize_alias("Itaú  Unibanco") == "ITAU UNIBANCO"
    assert er.normalize_alias("nu pagamentos") == "NU PAGAMENTOS"


def test_put_entity_writes_lookup_items_and_resolves():
    t = FakeTable()
    er.put_entity(
        "nubank", "Nubank / Nu Holdings",
        ["NUBANK", "NU PAGAMENTOS", "TICKER-skip-not-used"],
        cnpj_roots=["18236120"], ticker="NU", sector="pagamentos",
        confidence="curated", table=t,
    )
    ent = er.get_entity("nubank", table=t)
    assert ent["display_name"] == "Nubank / Nu Holdings"
    assert "NUBANK" in ent["aliases"] and ent["confidence"] == "curated"
    assert ent["canonical_id"] == "nubank" and ent["cnpj_roots"] == ["18236120"]
    # lookup items
    assert er.resolve_by_alias("nubank", table=t) == "nubank"
    assert er.resolve_by_alias("Nu Pagamentos", table=t) == "nubank"
    assert er.resolve_by_cnpj("18.236.120/0001-58", table=t) == "nubank"  # root extracted
    assert er.resolve_by_alias("unknown", table=t) is None


def test_news_query_term_cleans_and_overrides():
    # cleaned display_name
    assert er.news_query_term("nubank", "Nubank / Nu Holdings") == "Nubank"
    assert er.news_query_term("infinitepay", "InfinitePay (CloudWalk)") == "InfinitePay"
    assert er.news_query_term("c6", "C6 Bank") == "C6 Bank"
    # curated overrides for ambiguous / too-precise brands
    assert er.news_query_term("santander", "Santander") == "Santander Brasil"
    assert er.news_query_term("itau", "Itaú") == "Itaú Unibanco"
    assert er.news_query_term("xp", "XP Inc / XP Investimentos") == "XP Inc"
    assert er.news_query_term("caixa", "Caixa Econômica Federal") == "Caixa Econômica"
    # fallback to id when no display_name
    assert er.news_query_term("weirdco", "") == "weirdco"


def test_news_terms_only_trusted_and_deduped():
    t = FakeTable()
    er.put_entity("c6", "C6 Bank", ["C6 BANK"], confidence="curated", table=t)
    er.put_entity("itau", "Itaú", ["ITAU"], confidence="curated", table=t)
    # auto-created, not vetted -> excluded until news_safe
    er.put_entity("newco", "NewCo Pay", ["NEWCO"], confidence="cnpj", table=t)
    terms = er.news_terms(table=t)
    assert "C6 Bank" in terms
    assert "Itaú Unibanco" in terms      # override applied
    assert "NewCo Pay" not in terms      # untrusted excluded
    # promote it -> now included
    er.set_news_safe("newco", True, table=t)
    assert "NewCo Pay" in er.news_terms(table=t)
    # trusted_only=False includes everything
    assert "NewCo Pay" in er.news_terms(table=t, trusted_only=False)


def test_fatos_terms_only_flagged_and_trusted():
    t = FakeTable()
    er.put_entity("bb_seg", "BB Seguridade", ["BB SEGURIDADE"], confidence="curated",
                  fatos_term="BB SEGURIDADE", table=t)
    er.put_entity("c6", "C6 Bank", ["C6 BANK"], confidence="curated", table=t)  # no fatos_term
    er.put_entity("newco", "NewCo Seg", ["NEWCO SEG"], confidence="cnpj",
                  fatos_term="NEWCO SEG", table=t)  # untrusted
    assert er.fatos_terms(table=t) == ["BB SEGURIDADE"]   # only flagged + trusted
    assert "NEWCO SEG" in er.fatos_terms(table=t, trusted_only=False)


def test_news_search_false_excludes_from_news_but_keeps_structured():
    t = FakeTable()
    er.put_entity("porto", "Porto Seguro", ["PORTO SEGURO"], confidence="curated",
                  news_term="Porto Seguro", fatos_term="PORTO SEGURO",
                  news_search=False, table=t)
    er.put_entity("c6", "C6 Bank", ["C6 BANK"], confidence="curated", table=t)
    terms = er.news_terms(table=t)
    assert "Porto Seguro" not in terms    # structured-only: out of the news query set
    assert "C6 Bank" in terms
    assert "PORTO SEGURO" in er.fatos_terms(table=t)   # still has the structured lens


def test_patch_fatos_term_and_news_search():
    t = FakeTable()
    er.put_entity("porto", "Porto Seguro", ["PORTO SEGURO"], confidence="curated",
                  news_term="Porto Seguro", table=t)
    er.update_entity("porto", {"fatos_term": "PORTO SEGURO", "news_search": False}, table=t)
    ent = er.get_entity("porto", table=t)
    assert ent["fatos_term"] == "PORTO SEGURO" and ent["news_search"] is False
    assert er.fatos_terms(table=t) == ["PORTO SEGURO"]
    assert "Porto Seguro" not in er.news_terms(table=t)


def test_put_entity_stores_search_curation():
    t = FakeTable()
    er.put_entity("stone", "Stone / StoneCo", ["STONECO", "STONE "],
                  news_term="Stone", ambiguous_tokens=["STONE"],
                  confidence="curated", table=t)
    ent = er.get_entity("stone", table=t)
    assert ent["news_term"] == "Stone"
    assert ent["ambiguous_tokens"] == ["STONE"] and ent["ambiguous"] is True


def test_seed_populates_news_term_and_ambiguous():
    t = FakeTable()
    er.seed(table=t)
    stone = er.get_entity("stone", table=t)
    c6 = er.get_entity("c6", table=t)
    xp = er.get_entity("xp", table=t)
    # ambiguous migrated from AMBIGUOUS_TOKENS: only the common-word token "STONE"
    # (not the distinctive STONECO/STNE); c6 has none.
    assert stone["ambiguous_tokens"] == ["STONE"] and stone["ambiguous"] is True
    assert c6["ambiguous_tokens"] == [] and c6["ambiguous"] is False
    # XP's ambiguous token lives only on its ticker (TICKER:XP) — still captured.
    assert xp["ambiguous_tokens"] == ["XP"] and xp["ambiguous"] is True
    # news_term migrated from overrides/derivation.
    assert xp["news_term"] == "XP Inc" and c6["news_term"] == "C6 Bank"


def test_news_terms_prefers_stored_over_derivation():
    t = FakeTable()
    er.put_entity("c6", "C6 Bank", ["C6 BANK"], confidence="curated",
                  news_term="Banco C6", table=t)   # an operator's API edit
    assert "Banco C6" in er.news_terms(table=t)     # stored wins
    assert "C6 Bank" not in er.news_terms(table=t)


def test_load_ambiguous_tokens_from_registry():
    t = FakeTable()
    er.put_entity("stone", "Stone", ["STONECO", "STONE "], ambiguous_tokens=["STONE"],
                  confidence="curated", table=t)
    er.put_entity("c6", "C6 Bank", ["C6 BANK"], ambiguous_tokens=[],
                  confidence="curated", table=t)
    toks = er.load_ambiguous_tokens(table=t, force=True)
    # only the curated common-word token; STONECO stays distinctive.
    assert "STONE" in toks and "STONECO" not in toks and "C6 BANK" not in toks


def test_backfill_curation_is_nondestructive():
    t = FakeTable()
    # An entity seeded before the migration (no news_term/ambiguous), later vetted.
    er.put_entity("newco", "NewCo Pay", ["NEWCO"], confidence="cnpj", table=t)
    er.set_news_safe("newco", True, table=t)
    n = er.backfill_curation(table=t)
    ent = er.get_entity("newco", table=t)
    assert n == 1
    assert ent["news_term"] == "NewCo Pay" and ent["ambiguous_tokens"] == []
    assert ent["ambiguous"] is False
    assert ent["news_safe"] is True          # preserved — not clobbered
    # idempotent second run
    assert er.backfill_curation(table=t) == 0


def test_seed_from_curated_aliases_populates_registry():
    t = FakeTable()
    n = er.seed(table=t)
    assert n >= 15  # all curated entities
    # a curated entity resolves by alias, and the TICKER: form is indexed by its ticker
    assert er.resolve_by_alias("BTG PACTUAL", table=t) == "btg"
    assert er.resolve_by_alias("STNE", table=t) == "stone"          # from TICKER:STNE
    assert er.resolve_by_alias("Creditas", table=t) == "creditas"
    ent = er.get_entity("infinitepay", table=t)
    assert ent and ent["confidence"] == "curated"
    assert er.normalize_alias("CloudWalk") in ent["aliases"]


def _entrant(**kw):
    base = {
        "id": "bcb-auth:11222333000181",
        "cnpj": "11.222.333/0001-81",
        "name": "ZAPBANK INSTITUICAO DE PAGAMENTO S.A.",
        "trade_name": "ZapBank",
        "legal_name": "ZAPBANK INSTITUICAO DE PAGAMENTO S.A.",
        "controllers": ["ZAP HOLDING LTDA"],
        "license_class": "IP",
        "is_fintech": True,
    }
    base.update(kw)
    return base


def test_auto_create_from_entrant_writes_and_is_idempotent():
    t = FakeTable()
    eid = er.auto_create_from_entrant(_entrant(), table=t)
    assert eid == "zapbank"
    ent = er.get_entity(eid, table=t)
    assert ent["confidence"] == "cnpj" and ent["cnpj_roots"] == ["11222333"]
    assert ent["controllers"] == ["ZAP HOLDING LTDA"] and ent["sector"] == "fintech"
    # resolvable by CNPJ root and by both brand + legal name (future signals)
    assert er.resolve_by_cnpj("11.222.333/0001-81", table=t) == "zapbank"
    assert er.resolve_by_alias("ZapBank", table=t) == "zapbank"
    assert er.resolve_by_alias("ZAPBANK INSTITUICAO DE PAGAMENTO S.A.", table=t) == "zapbank"
    # second pass is a no-op (already mapped by CNPJ)
    assert er.auto_create_from_entrant(_entrant(), table=t) is None


def test_auto_create_skips_without_cnpj():
    t = FakeTable()
    assert er.auto_create_from_entrant(_entrant(cnpj=None), table=t) is None
    assert er.auto_create_from_entrant(_entrant(cnpj="123"), table=t) is None  # too short


def test_auto_create_disambiguates_slug_collision_without_clobbering():
    t = FakeTable()
    # a curated entity already owns the slug for a *different* CNPJ
    er.put_entity("zapbank", "ZapBank (curated)", ["ZAPBANK"],
                  cnpj_roots=["99999999"], confidence="curated", table=t)
    eid = er.auto_create_from_entrant(_entrant(), table=t)
    assert eid == "zapbank_11222333"  # disambiguated, did not overwrite
    assert er.get_entity("zapbank", table=t)["confidence"] == "curated"
    assert er.resolve_by_cnpj("11222333", table=t) == "zapbank_11222333"


def test_accumulate_aliases_adds_data_derived_form_and_indexes_it():
    t = FakeTable()
    # step 3 auto-created a fintech from its CNPJ; only the brand is known so far
    er.put_entity("zapbank_11222333", "ZapBank", ["ZAPBANK"],
                  cnpj_roots=["11222333"], confidence="cnpj", table=t)
    # a later CVM offering names it by razão social — accumulate that alias
    added = er.accumulate_aliases(
        "zapbank_11222333", ["ZAPBANK INSTITUICAO DE PAGAMENTO S.A."], table=t
    )
    assert added == ["ZAPBANK INSTITUICAO DE PAGAMENTO S.A."]
    # now a name-only signal (news/DOU) using the legal name resolves too
    assert er.resolve_by_alias("Zapbank Instituicao de Pagamento S.A.", table=t) == "zapbank_11222333"
    ent = er.get_entity("zapbank_11222333", table=t)
    assert "ZAPBANK INSTITUICAO DE PAGAMENTO S.A." in ent["alias_forms"]


def test_accumulate_aliases_is_idempotent_and_skips_short_forms():
    t = FakeTable()
    er.put_entity("inter", "Banco Inter", ["INTER"], confidence="curated", table=t)
    assert er.accumulate_aliases("inter", ["Banco Inter S.A."], table=t) == ["Banco Inter S.A."]
    # second pass with the same form writes nothing
    assert er.accumulate_aliases("inter", ["Banco Inter S.A."], table=t) == []
    # too-short forms are rejected (unsafe substring keys)
    assert er.accumulate_aliases("inter", ["IB"], table=t) == []


def test_accumulate_aliases_never_hijacks_another_entitys_name():
    t = FakeTable()
    er.put_entity("stoneco", "StoneCo", ["STONECO", "STONE PAGAMENTOS S.A."],
                  confidence="curated", table=t)
    er.put_entity("stonex", "StoneX", ["STONEX"], confidence="curated", table=t)
    # trying to fold StoneCo's exact name onto StoneX must NOT steal the index
    added = er.accumulate_aliases("stonex", ["STONE PAGAMENTOS S.A."], table=t)
    assert added == ["STONE PAGAMENTOS S.A."]           # raw form kept on stonex
    assert er.resolve_by_alias("STONE PAGAMENTOS S.A.", table=t) == "stoneco"  # index untouched
    assert "STONE PAGAMENTOS S.A." not in er.get_entity("stonex", table=t)["aliases"]


def test_accumulate_aliases_unknown_entity_is_noop():
    t = FakeTable()
    assert er.accumulate_aliases("ghost", ["Some Bank S.A."], table=t) == []


def test_review_queue_propose_is_idempotent_and_lists_pending():
    t = FakeTable()
    rid = er.propose_review("group_merge", key="a->b", entity_id="a", target_id="b",
                            reason="shared controller: X", table=t)
    assert rid == "group_merge:a_b"
    # re-proposing the same (kind,key) is a no-op — no duplicate queue entries
    assert er.propose_review("group_merge", key="a->b", entity_id="a", target_id="b", table=t) is None
    pending = er.list_reviews(table=t)
    assert len(pending) == 1 and pending[0]["status"] == "pending"


def test_review_approve_group_merge_links_canonical_id():
    t = FakeTable()
    er.put_entity("stoneco", "StoneCo", ["STONECO"], confidence="curated", table=t)
    er.put_entity("ent_x", "Some Fintech", ["SOME FINTECH"], cnpj_roots=["12345678"],
                  confidence="cnpj", table=t)
    rid = er.propose_review("group_merge", key="ent_x->stoneco",
                            entity_id="ent_x", target_id="stoneco", table=t)
    item = er.resolve_review(rid, "approved", table=t)
    assert item["status"] == "approved" and "decided_at" in item
    # the member is now grouped under the curated leader
    assert er.get_entity("ent_x", table=t)["canonical_id"] == "stoneco"
    # deciding again is a no-op (already resolved)
    assert er.resolve_review(rid, "rejected", table=t) is None


def test_review_reject_records_decision_without_applying():
    t = FakeTable()
    er.put_entity("ent_y", "Y", ["Y BRAND"], confidence="cnpj", table=t)
    rid = er.propose_review("group_merge", key="ent_y->z", entity_id="ent_y",
                            target_id="z", table=t)
    er.resolve_review(rid, "rejected", table=t)
    assert er.get_entity("ent_y", table=t)["canonical_id"] == "ent_y"  # unchanged
    assert er.list_reviews(table=t) == []                              # not pending
    assert er.list_reviews(status="rejected", table=t)[0]["review_id"] == rid


def test_propose_group_merges_from_shared_controller():
    t = FakeTable()
    # two auto-created fintechs share a controller; a curated brand is the leader
    er.put_entity("brandco", "BrandCo", ["BRANDCO"], confidence="curated",
                  controllers=["BRAND HOLDING LTDA"], table=t)
    er.put_entity("ent_a", "Fintech A", ["FINTECH A"], cnpj_roots=["11111111"],
                  confidence="cnpj", controllers=["BRAND HOLDING LTDA"], table=t)
    er.put_entity("ent_b", "Fintech B", ["FINTECH B"], cnpj_roots=["22222222"],
                  confidence="cnpj", controllers=["Brand Holding Ltda"], table=t)  # case/spacing folds
    # a lone entity with a unique controller must NOT be proposed
    er.put_entity("solo", "Solo", ["SOLO"], cnpj_roots=["33333333"],
                  confidence="cnpj", controllers=["OTHER HOLDING"], table=t)
    n = er.propose_group_merges(table=t)
    assert n == 2  # ent_a and ent_b, each -> brandco
    targets = {r["entity_id"]: r["target_id"] for r in er.list_reviews(table=t)}
    assert targets == {"ent_a": "brandco", "ent_b": "brandco"}
    # idempotent: a second pass queues nothing new
    assert er.propose_group_merges(table=t) == 0


def test_classify_industries_safe_case_and_review():
    # clear license -> its industry, no review
    assert er.classify_industries({"license_class": "Banco"}) == (["banking"], False)
    assert er.classify_industries({"license_class": "Instituição de Pagamento"}) == (["fintech"], False)
    assert er.classify_industries({"is_fintech": True}) == (["fintech"], False)
    # ambiguous -> no auto-tag, needs review
    assert er.classify_industries({"license_class": "Corretora/DTVM"}) == ([], True)
    assert er.classify_industries({}) == ([], True)


def test_seed_industries_and_put_entity_industries():
    t = FakeTable()
    n = er.seed_industries(table=t)
    assert n == len(er.INDUSTRIES)
    assert t.items["IND#banking"]["display_name"] == "Banking"
    assert t.items["IND#banking"]["parent"] == "financial-services"
    er.put_entity("x", "X", ["XCO"], industries=["Banking", "FINTECH"], table=t)
    assert er.get_entity("x", table=t)["industries"] == ["banking", "fintech"]  # normalized


def test_auto_create_tags_industry_from_license():
    t = FakeTable()
    eid = er.auto_create_from_entrant(_entrant(license_class="Banco", is_fintech=False), table=t)
    assert er.get_entity(eid, table=t)["industries"] == ["banking"]
    eid2 = er.auto_create_from_entrant(
        _entrant(cnpj="55.666.777/0001-00", is_fintech=True, license_class="Crédito Direto (SCD)"), table=t)
    assert er.get_entity(eid2, table=t)["industries"] == ["fintech"]


def test_industry_rollup_counts_entities_per_industry():
    t = FakeTable()
    er.put_entity("a", "A", ["ACO"], industries=["banking"], table=t)
    er.put_entity("b", "B", ["BCO"], industries=["banking", "fintech"], table=t)
    er.put_entity("c", "C", ["CCO"], table=t)  # untagged
    r = er.industry_rollup(table=t)
    assert r["banking"]["entities"] == 2
    assert r["fintech"]["entities"] == 1
    assert r["_unclassified"]["entities"] == 1
    assert r["banking"]["tier"] == "premium"


def test_set_industries_assigns_and_clears_needs_review():
    t = FakeTable()
    er.put_entity("x", "X", ["XCO"], table=t)
    assert er.set_industries("x", ["Banking", " ", "fintech"], table=t) is True
    ent = er.get_entity("x", table=t)
    assert ent["industries"] == ["banking", "fintech"]  # normalized + sorted
    assert ent["needs_review"] is False
    assert er.set_industries("missing", ["banking"], table=t) is False


def test_entity_industry_map_lists_industries_per_entity():
    t = FakeTable()
    er.put_entity("a", "A", ["ACO"], industries=["banking"], table=t)
    er.put_entity("b", "B", ["BCO"], table=t)  # untagged -> []
    m = er.entity_industry_map(table=t)
    assert m == {"a": ["banking"], "b": []}


def test_industry_review_proposes_and_assigns_chosen_module():
    t = FakeTable()
    er.put_entity("newco", "NewCo", ["NEWCO"], confidence="cnpj", table=t)
    rid = er.propose_industry("newco", "NewCo", table=t)
    assert rid == "industry:newco"
    # idempotent: a second proposal for the same entity is a no-op
    assert er.propose_industry("newco", "NewCo", table=t) is None
    # curator picks a module at approval time -> entity gets it, review closes
    item = er.resolve_review("industry:newco", "approved", table=t,
                             payload={"industries": ["insurance"]})
    assert item["status"] == "approved"
    assert er.get_entity("newco", table=t)["industries"] == ["insurance"]


def test_auto_create_leaves_ambiguous_license_unclassified():
    t = FakeTable()
    # a license not in LICENSE_INDUSTRY -> no industries auto-assigned (review path)
    eid = er.auto_create_from_entrant(
        _entrant(license_class="Corretora/DTVM", is_fintech=False), table=t)
    assert "industries" not in er.get_entity(eid, table=t)


def test_seed_assigns_curated_industries():
    t = FakeTable()
    er.seed(table=t)
    assert er.get_entity("itau", table=t)["industries"] == ["banking"]
    assert set(er.get_entity("nubank", table=t)["industries"]) == {"banking", "fintech"}
    assert t.items.get("IND#fintech") is not None  # taxonomy seeded too


def test_load_trust_map_reflects_confidence_and_news_safe():
    t = FakeTable()
    er.put_entity("cur", "Curated", ["CURATED"], confidence="curated", table=t)
    er.put_entity("auto", "Auto", ["AUTO"], cnpj_roots=["11112222"], confidence="cnpj", table=t)
    er.clear_cache()
    tm = er.load_trust_map(table=t)
    assert tm == {"cur": True, "auto": False}         # curated trusted, auto not
    # promoting the auto entity flips its trust
    assert er.set_news_safe("auto", True, table=t) is True
    er.clear_cache()
    assert er.load_trust_map(table=t)["auto"] is True
    er.clear_cache()


def test_news_safe_review_promotes_entity_on_approve():
    t = FakeTable()
    er.put_entity("zapbank", "ZapBank", ["ZAPBANK"], cnpj_roots=["11222333"],
                  confidence="cnpj", table=t)
    rid = er.propose_news_safe("zapbank", "ZapBank", table=t)
    assert er.get_entity("zapbank", table=t).get("news_safe") in (None, False)
    er.resolve_review(rid, "approved", table=t)
    assert er.get_entity("zapbank", table=t)["news_safe"] is True
    # rejecting a different proposal must NOT promote
    er.put_entity("other", "Other", ["OTHER"], confidence="cnpj", table=t)
    rid2 = er.propose_news_safe("other", "Other", table=t)
    er.resolve_review(rid2, "rejected", table=t)
    assert er.get_entity("other", table=t).get("news_safe") in (None, False)


def test_set_news_safe_missing_entity_is_noop():
    t = FakeTable()
    assert er.set_news_safe("ghost", True, table=t) is False


def test_load_alias_map_returns_raw_forms():
    t = FakeTable()
    er.put_entity("nubank", "Nubank", ["NUBANK", "NU"],
                  alias_forms=["NUBANK", " NU ", "TICKER:NU"], table=t)
    er.clear_cache()
    m = er.load_alias_map(table=t)
    assert m["nubank"] == ["NUBANK", " NU ", "TICKER:NU"]  # raw curated forms preserved
    er.clear_cache()


def test_add_cnpj_roots_writes_lookup_and_is_idempotent():
    t = FakeTable()
    er.put_entity("nubank", "Nubank", ["NUBANK"], confidence="curated", table=t)
    added = er.add_cnpj_roots("nubank", ["18.236.120/0001-58"], table=t)
    assert added == ["18236120"]
    assert er.get_entity("nubank", table=t)["cnpj_roots"] == ["18236120"]
    assert er.resolve_by_cnpj("18236120", table=t) == "nubank"
    # idempotent: adding the same root again writes nothing new
    assert er.add_cnpj_roots("nubank", ["18236120"], table=t) == []


def test_add_cnpj_roots_never_steals_another_entitys_root():
    t = FakeTable()
    er.put_entity("a", "A", ["A CO"], cnpj_roots=["12345678"], confidence="curated", table=t)
    er.put_entity("b", "B", ["B CO"], confidence="curated", table=t)
    assert er.add_cnpj_roots("b", ["12345678"], table=t) == []      # owned by a
    assert er.resolve_by_cnpj("12345678", table=t) == "a"
    assert er.get_entity("b", table=t).get("cnpj_roots", []) == []


def test_add_cnpj_roots_preserves_other_fields():
    t = FakeTable()
    er.put_entity("inter", "Banco Inter", ["INTER"], confidence="curated",
                  news_term="Banco Inter", table=t)
    er.set_news_safe("inter", True, table=t)
    er.add_cnpj_roots("inter", ["00416968"], table=t)
    ent = er.get_entity("inter", table=t)
    assert ent["cnpj_roots"] == ["00416968"]
    assert ent["news_safe"] is True and ent["news_term"] == "Banco Inter"  # not clobbered
