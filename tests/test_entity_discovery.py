"""Tests for entity discovery pipeline (FIAGRO vertical + keyword harvest)."""
from __future__ import annotations

from typing import Any

from src.synth.entity_discovery import (
    _brand_from_name,
    _profile_from_fiagro,
    discover_fiagro,
    harvest_keyword,
    propose_news_candidates,
)


class _FakeTable:
    """Minimal DynamoDB-table double for discovery unit tests."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def get_item(self, Key: dict[str, Any]) -> dict[str, Any]:
        it = self.items.get(Key["pk"])
        return {"Item": it} if it else {}

    def put_item(self, Item: dict[str, Any]) -> None:
        self.items[Item["pk"]] = dict(Item)

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        return {"Items": list(self.items.values())}


def test_brand_from_name():
    # Distinctive brand kept; generic fund-descriptor words (crédito/agro) are
    # stopwords, so "KINEA CRÉDITO AGRO FIAGRO" reduces to the manager brand.
    b1 = (_brand_from_name("KINEA CRÉDITO AGRO FIAGRO-IMOBILIÁRIO") or "").upper()
    assert "KINEA" in b1
    assert "FIAGRO" not in b1  # generic suffix dropped
    b2 = (_brand_from_name("XP CRÉDITO AGRO - FI NAS CADEIAS PRODUTIVAS") or "").upper()
    assert "XP" in b2
    assert _brand_from_name("FIAGRO") is None  # pure stop


def test_profile_from_fiagro():
    row = {
        "fund_name": "KINEA CRÉDITO AGRO FIAGRO-IMOBILIÁRIO",
        "ticker": "KNCA11",
        "cnpj": "41745701000137",
        "isin": "BRKNCACTF014",
        "admin": "INTRAG",
        "manager": "KINEA",
        "pl": 2_185_411_394.09,
        "as_of": "2026-03-01",
        "url": "https://dados.cvm.gov.br/dataset/fiagro-doc-inf_mensal",
    }
    p = _profile_from_fiagro(row)
    assert p["ticker"] == "KNCA11"
    assert "agri-funds" in p["industries"]
    assert p["cnpj_roots"] == ["41745701"]
    assert p["confidence"] == "cnpj"
    assert "KNCA11" in p["aliases"]


def test_discover_fiagro_creates_and_enriches():
    table = _FakeTable()
    rows = [
        {
            "fund_name": "KINEA CRÉDITO AGRO FIAGRO-IMOBILIÁRIO",
            "ticker": "KNCA11",
            "cnpj": "41745701000137",
            "isin": "BRKNCACTF014",
            "admin": "INTRAG",
            "manager": "KINEA",
            "pl": 2e9,
            "as_of": "2026-03-01",
            "url": "https://example",
        },
        {
            "fund_name": "VALORA CRA FIAGRO",
            "ticker": "VGIA11",
            "cnpj": "41081088000109",
            "isin": "BRVGIACTF004",
            "admin": "VALORA",
            "manager": "VALORA",
            "pl": 1e9,
            "as_of": "2026-03-01",
            "url": "https://example",
        },
    ]
    report = discover_fiagro(
        rows=rows, min_pl=0, auto_create=True, max_new=10, table=table
    )
    assert report["fetched"] == 2
    assert len(report["created"]) == 2
    assert report["already"] == 0

    # Second run: both already present → enrich path (idempotent).
    report2 = discover_fiagro(
        rows=rows, min_pl=0, auto_create=True, max_new=10, table=table
    )
    assert report2["already"] == 2
    assert report2["created"] == []

    # CNPJ lookup works.
    from src.synth import entity_registry

    eid = entity_registry.resolve_by_cnpj("41745701", table=table)
    assert eid is not None
    ent = entity_registry.get_entity(eid, table=table)
    assert ent is not None
    assert ent.get("ticker") == "KNCA11"
    assert "agri-funds" in (ent.get("industries") or [])


def test_discover_fiagro_links_fund_to_tier1_parent():
    # ADR 017: a fund whose brand token matches a tracked institution links to it as
    # `parent`; the parent stays tier-1 (no agri, no parent), the fund carries the line.
    from src.synth import entity_registry

    table = _FakeTable()
    entity_registry.put_entity(
        "btg", "BTG Pactual", ["BTG", "TICKER:BPAC11"],
        industries=["investment-banking"], table=table,
    )
    rows = [{
        "fund_name": "BTG CERES FIAGRO", "ticker": "BTGC11", "cnpj": "55555555000199",
        "pl": 1e9, "as_of": "2026-03-01", "url": "https://x", "admin": "BTG", "manager": "BTG",
    }]
    report = discover_fiagro(rows=rows, min_pl=0, auto_create=True, max_new=5, table=table)
    assert report["created"]  # id is the ticker slug (btgc11); parent comes from the brand
    child = report["created"][0]
    assert entity_registry.get_entity(child, table=table)["parent"] == "btg"
    parent = entity_registry.get_entity("btg", table=table)
    assert "agri-funds" not in (parent.get("industries") or []) and "parent" not in parent
    assert entity_registry.children_of("btg", table=table) == [child]


def test_discover_consorcio_creates_and_nests_conglomerate_arm():
    from src.synth import entity_registry
    from src.synth.entity_discovery import discover_consorcio

    table = _FakeTable()
    entity_registry.put_entity("bradesco", "Bradesco", ["BRADESCO", "TICKER:BBDC4"],
                               industries=["banking"], table=table)
    rows = [
        {"cnpj": "17351180", "name": "BRADESCO ADMINISTRADORA DE CONSÓRCIOS LTDA.",
         "branches": 30, "as_of": "2026-08-27"},
        {"cnpj": "52111111", "name": "ADEMICON ADMINISTRADORA DE CONSÓRCIOS S.A.",
         "branches": 20, "as_of": "2026-08-27"},
    ]
    report = discover_consorcio(rows=rows, min_branches=1, auto_create=True, table=table)
    assert len(report["created"]) == 2
    # the conglomerate arm gets a distinct id nested under the parent (no overwrite).
    arm = entity_registry.get_entity("bradesco-consorcio", table=table)
    assert arm and arm["parent"] == "bradesco" and arm["industries"] == ["consorcio"]
    assert entity_registry.get_entity("bradesco", table=table)["industries"] == ["banking"]
    # an independent administradora is its own top-level entity, no parent.
    adem = entity_registry.get_entity("ademicon", table=table)
    assert adem and "parent" not in adem and adem["industries"] == ["consorcio"]
    # idempotent: second run enriches nothing new.
    r2 = discover_consorcio(rows=rows, min_branches=1, auto_create=True, table=table)
    assert r2["created"] == [] and r2["already"] == 2


def test_fiagro_enrich_never_pollutes_an_institution():
    # #52: a fund that resolves to an INSTITUTION (via a ticker/cnpj left on it by earlier
    # pollution) must NOT enrich it — no agri-funds added, no fund aliases accumulated.
    from src.synth import entity_registry

    table = _FakeTable()
    entity_registry.put_entity("btg", "BTG Pactual", ["BTG", "BTAL11"],
                               industries=["investment-banking"], table=table)
    rows = [{"fund_name": "BTG PACTUAL AGRO FIAGRO", "ticker": "BTAL11",
             "cnpj": "36642244000100", "pl": 1e9, "as_of": "2026-03-01", "url": "x"}]
    discover_fiagro(rows=rows, min_pl=0, auto_create=True, table=table)
    btg = entity_registry.get_entity("btg", table=table)
    assert btg["industries"] == ["investment-banking"]        # NOT polluted with agri-funds
    assert "BTG PACTUAL AGRO FIAGRO" not in (btg.get("alias_forms") or [])  # no fund alias


def test_fiagro_create_never_overwrites_existing_entity():
    # #52: a fund whose brand-slug equals an institution id ("XP" -> "xp") must be proposed,
    # never put_entity'd OVER the institution.
    from src.synth import entity_registry

    table = _FakeTable()
    entity_registry.put_entity("xp", "XP", ["XP"],
                               industries=["asset-management"], table=table)
    rows = [{"fund_name": "XP AGRO RENDA FIAGRO", "ticker": None,
             "cnpj": "63779784000100", "pl": 1e9, "as_of": "2026-03-01", "url": "x"}]
    rep = discover_fiagro(rows=rows, min_pl=0, auto_create=True, table=table)
    xp = entity_registry.get_entity("xp", table=table)
    assert xp["industries"] == ["asset-management"]           # NOT overwritten
    assert rep["proposed"] and "xp" not in rep["created"]


def test_fiagro_brand_collision_with_ticker_auto_links_as_subentity():
    # ADR-017: a fund whose bare BRAND display collides with a parentable institution but has
    # its OWN distinct ticker id is auto-created as that parent's sub-entity — NOT queued for
    # review — and the parent is never polluted.
    from src.synth import entity_registry

    table = _FakeTable()
    entity_registry.put_entity("xp", "XP", ["XP"], industries=["asset-management"], table=table)
    rows = [{"fund_name": "XP", "ticker": "XPAG11", "cnpj": "43741189000100",
             "pl": 1e9, "as_of": "2026-03-01", "url": "x"}]
    rep = discover_fiagro(rows=rows, min_pl=0, auto_create=True, max_new=5, table=table)
    assert rep.get("auto_linked") == ["xpag11"] and not rep["proposed"]
    child = entity_registry.get_entity("xpag11", table=table)
    assert child["parent"] == "xp" and child["industries"] == ["agri-funds"]
    parent = entity_registry.get_entity("xp", table=table)
    assert parent["industries"] == ["asset-management"] and "parent" not in parent
    assert "XPAG11" not in (parent.get("alias_forms") or [])  # no fund ticker on the parent


def test_fiagro_brand_collision_without_parent_still_proposed():
    # A brand collision with a NON-parentable entity (no parent resolvable) stays a review.
    from src.synth import entity_registry

    table = _FakeTable()
    entity_registry.put_entity("somefund", "VALORA", ["VALORA"], industries=["agri-funds"], table=table)
    rows = [{"fund_name": "VALORA", "ticker": "VLRA11", "cnpj": "64221668000100",
             "pl": 1e9, "as_of": "2026-03-01", "url": "x"}]
    rep = discover_fiagro(rows=rows, min_pl=0, auto_create=True, max_new=5, table=table)
    assert rep["proposed"] and not rep.get("auto_linked")  # no parentable owner → review


def test_discover_fiagro_quality_gate():
    """Junk-named funds (no ticker, generic legal name) are proposed, not
    auto-created; clean ticker funds are created."""
    table = _FakeTable()
    rows = [
        {
            "fund_name": "INVESTIMENTO FIAGRO RESPONSABILIDADE LIMITADA",
            "ticker": None,
            "cnpj": "11111111000191",
            "isin": None,
            "admin": "ADM",
            "manager": "GES",
            "pl": 2e8,
            "as_of": "2026-03-01",
            "url": "u",
        },
        {
            "fund_name": "KINEA CRÉDITO AGRO FIAGRO",
            "ticker": "KNCA11",
            "cnpj": "41745701000137",
            "isin": "BRKNCACTF014",
            "admin": "INTRAG",
            "manager": "KINEA",
            "pl": 2e9,
            "as_of": "2026-03-01",
            "url": "u",
        },
    ]
    report = discover_fiagro(rows=rows, min_pl=0, auto_create=True, table=table)
    assert report["created"] == ["knca11"]  # clean ticker fund auto-created
    assert len(report["proposed"]) == 1  # junk-named fund routed to review
    from src.synth import entity_registry

    # the junk fund never entered the registry...
    assert entity_registry.resolve_by_cnpj("11111111", table=table) is None
    # ...but a review proposal exists for it, tagged needs_brand_review.
    reviews = [v for k, v in table.items.items() if k.startswith("REVIEW#")]
    assert any(r.get("reason") == "needs_brand_review" for r in reviews)


def test_profile_auto_ok_flag():
    from src.synth.entity_discovery import _profile_from_fiagro

    junk = _profile_from_fiagro({"fund_name": "INVESTIMENTO RESPONSABILIDADE LIMITADA", "cnpj": "11111111000191"})
    assert junk["auto_ok"] is False
    clean = _profile_from_fiagro({"fund_name": "SUNO AGRO FIAGRO", "ticker": "SNAG11", "cnpj": "28152777000100"})
    assert clean["auto_ok"] is True


def test_fiagro_shared_admin_not_indexed_no_over_resolution():
    """FIXED (was the P0 over-resolution defect): _profile_from_fiagro no longer
    puts the fund's `admin`/`manager` (a shared servicer name) into aliases, so a
    signal about fund A does NOT resolve to unrelated fund B that merely shares
    the same administrator."""
    from src.synth import entities as _entities
    from src.synth.entity_discovery import _profile_from_fiagro

    fund_a = {
        "fund_name": "ALPHA CREDITO AGRO FIAGRO", "ticker": "ALFA11",
        "cnpj": "11111111000100", "admin": "SHARED ADMINISTRADORA DE FUNDOS S.A.",
        "manager": "GESTORA ALPHA", "pl": 100_000_000.0,
    }
    fund_b = {
        "fund_name": "BETA RURAL FIAGRO", "ticker": "BETA11",
        "cnpj": "22222222000100", "admin": "SHARED ADMINISTRADORA DE FUNDOS S.A.",
        "manager": "GESTORA BETA", "pl": 50_000_000.0,
    }
    profile_a = _profile_from_fiagro(fund_a)
    profile_b = _profile_from_fiagro(fund_b)
    # The shared servicer name is NOT an identity alias on either fund anymore.
    assert "SHARED ADMINISTRADORA DE FUNDOS S.A." not in profile_a["aliases"]
    assert "SHARED ADMINISTRADORA DE FUNDOS S.A." not in profile_b["aliases"]

    fake_alias_map = {
        profile_a["entity_id"]: profile_a["aliases"],
        profile_b["entity_id"]: profile_b["aliases"],
    }
    orig_alias_map = _entities._alias_map
    _entities._alias_map = lambda: fake_alias_map
    try:
        move_sig = {
            "_lens": "funds", "event": "pl_move", "fund_name": fund_a["fund_name"],
            "cnpj": fund_a["cnpj"], "admin": fund_a["admin"], "manager": fund_a["manager"],
            "ticker": fund_a["ticker"], "pl": 140_000_000.0, "pct_change": 40.0, "is_new": True,
        }
        resolved = _entities.resolve_entities(move_sig)
    finally:
        _entities._alias_map = orig_alias_map

    # Fund A resolves (via its own ticker); fund B does NOT (no shared-admin leak).
    assert profile_a["entity_id"] in resolved
    assert profile_b["entity_id"] not in resolved, (
        f"fund B should not be pulled in via shared admin; got {resolved}"
    )


def test_propose_review_sanitizes_floats():
    """A payload with floats (e.g. a fund's PL) must be stored as Decimal —
    DynamoDB rejects Python floats, which previously errored the review write."""
    from decimal import Decimal
    from src.synth import entity_registry

    table = _FakeTable()
    entity_registry.propose_review(
        kind="discovery",
        key="k1",
        proposed="X",
        payload={"pl": 1.5, "nested": {"cotistas": 2.0}, "n": 3, "s": "x"},
        table=table,
    )
    item = next(v for k, v in table.items.items() if k.startswith("REVIEW#"))
    assert isinstance(item["payload"]["pl"], Decimal)
    assert isinstance(item["payload"]["nested"]["cotistas"], Decimal)
    assert item["payload"]["n"] == 3  # ints untouched
    assert item["payload"]["s"] == "x"


def test_discover_fiagro_scans_registry_once():
    """Perf: discovery scans the registry ONCE regardless of row count (no
    per-row full-table scan via resolve_by_name)."""

    class _CountingTable(_FakeTable):
        scans = 0

        def scan(self, **kwargs):
            type(self).scans += 1
            return super().scan(**kwargs)

    table = _CountingTable()
    rows = [
        {
            "fund_name": f"BRAND{i} AGRO FIAGRO",
            "ticker": f"AB{i:02d}11",
            "cnpj": f"{10000000 + i:08d}000191",
            "isin": None,
            "admin": "ADM",
            "manager": "GES",
            "pl": 1e8,
            "as_of": "2026-03-01",
            "url": "u",
        }
        for i in range(8)
    ]
    report = discover_fiagro(rows=rows, min_pl=0, auto_create=True, max_new=20, table=table)
    assert len(report["created"]) == 8
    assert _CountingTable.scans == 1  # exactly one scan for 8 rows, not one per row


def test_harvest_keyword_frequency_gate():
    news = [
        {
            "id": "n1",
            "title": "KNCA11 é o maior FIAGRO de crédito do agro",
            "source": "NEWS",
            "kind": "competitor",
        },
        {
            "id": "n2",
            "title": "KNCA11 eleva dividendos do FIAGRO",
            "source": "NEWS",
            "kind": "competitor",
        },
        {
            "id": "n3",
            "title": "RURA11 novo FIAGRO da Itaú Asset",
            "source": "NEWS",
            "kind": "competitor",
        },
        # single-doc noise
        {
            "id": "n4",
            "title": "XYZ99 aparece em um FIAGRO",
            "source": "NEWS",
            "kind": "competitor",
        },
    ]
    # Empty registry → tickers are unresolved candidates.
    table = _FakeTable()
    cands = harvest_keyword("FIAGRO", news, min_docs=2, table=table)
    surfaces = {c["surface"].upper() for c in cands}
    assert "KNCA11" in surfaces
    assert "XYZ99" not in surfaces  # only 1 doc
    # RURA11 appears once → filtered
    assert "RURA11" not in surfaces


def test_resolve_by_name_and_name_owned_by_other():
    from src.synth import entity_registry

    table = _FakeTable()
    entity_registry.put_entity(
        "stoneco", "StoneCo", ["StoneCo", "Stone"], cnpj_roots=["16501555"], table=table
    )
    # Unique hit by display-name / alias.
    assert entity_registry.resolve_by_name("StoneCo", table=table) == ["stoneco"]
    assert entity_registry.resolve_by_name("Stone", table=table) == ["stoneco"]
    assert entity_registry.resolve_by_name("Unknown Corp", table=table) == []
    # Collision guard: the name is owned by another entity, not by StoneX.
    assert entity_registry.name_owned_by_other("StoneCo", exclude_id="stonex", table=table)
    # ...but not "owned by other" when the excluded id IS the owner.
    assert not entity_registry.name_owned_by_other(
        "StoneCo", exclude_id="stoneco", table=table
    )


def test_harvest_generalizes_equity_ticker_and_single_brand():
    """Non-fund industry: equity ticker (XXXX4) + single-token brand surface."""
    table = _FakeTable()
    news = [
        {"id": "b1", "title": "ITUB4 sobe; Itaú comenta banco digital"},
        {"id": "b2", "title": "ITUB4 renova máxima; Itaú fala de banco digital"},
        {"id": "b3", "title": "Neon capta e o banco digital Neon acelera"},
        {"id": "b4", "title": "Neon cresce; banco digital Neon amplia base"},
    ]
    cands = harvest_keyword("BANCO DIGITAL", news, industry="banking", table=table)
    by = {c["surface"]: c for c in cands}
    assert "ITUB4" in by and by["ITUB4"]["kind"] == "ticker"  # equity ticker, not XXXX11
    assert "Neon" in by and by["Neon"]["kind"] == "brand"  # single-token brand
    assert all(c["industry"] == "banking" for c in cands)


def test_harvest_keyword_accent_plural_tolerant():
    """Singular, accented keyword matches plural, accent-varied news."""
    table = _FakeTable()
    news = [
        {"id": "f1", "title": "MXRF11 lidera entre fundos imobiliários"},
        {"id": "f2", "title": "MXRF11 paga dividendo; fundo imobiliário cresce"},
    ]
    cands = harvest_keyword("FUNDO IMOBILIÁRIO", news, table=table)
    surfaces = {c["surface"] for c in cands}
    assert "MXRF11" in surfaces
    assert all(c["industry"] == "real-estate-funds" for c in cands)


def test_propose_news_candidates_queues_review():
    table = _FakeTable()
    cands = [
        {
            "surface": "KNCA11",
            "tickers": ["KNCA11"],
            "count": 3,
            "evidence_ids": ["n1", "n2"],
            "sample_titles": ["KNCA11 FIAGRO"],
            "industry": "agri-funds",
        }
    ]
    queued = propose_news_candidates(cands, table=table)
    assert len(queued) == 1
    assert any(k.startswith("REVIEW#") for k in table.items)


# --- #14 discover_bcb_institutions (relevance-gated BCB registry sync) ---------
def _bcb_row(name, cnpj, license_class):
    return {"name": name, "cnpj": cnpj, "license_class": license_class,
            "is_fintech": False, "registry": "SedesSociedades"}


def test_bcb_relevance_gate_excludes_the_long_tail():
    from src.synth.entity_discovery import discover_bcb_institutions
    table = _FakeTable()
    rows = [
        _bcb_row("BANCO ORIGINAL S.A.", "92894922", "Banco"),
        _bcb_row("COOPERATIVA SICOOB CENTRAL", "01111111", "Cooperativa"),
        _bcb_row("CONSÓRCIO LUIZA ADM", "02222222", "Consórcio"),
        _bcb_row("XPTO ARRENDAMENTO MERCANTIL S.A.", "03333333", "Leasing"),
    ]
    r = discover_bcb_institutions(rows=rows, auto_create=True, table=table)
    assert r["fetched"] == 1                       # only the bank is promotable
    assert r["created"] == ["original"]
    # cooperativas open only behind the explicit flag
    r2 = discover_bcb_institutions(rows=rows, auto_create=True, include_coops=True, table=table)
    assert r2["fetched"] == 2


def test_bcb_creates_with_industry_and_registry_radar():
    from src.synth import entity_registry
    from src.ingest import reg_coverage
    from src.synth.entity_discovery import discover_bcb_institutions
    table = _FakeTable()
    rows = [
        _bcb_row("BANCO ORIGINAL S.A.", "92894922", "Banco"),
        _bcb_row("CREDVERDE SOCIEDADE DE CRÉDITO DIRETO S.A.", "40404040", "Crédito Direto (SCD)"),
    ]
    r = discover_bcb_institutions(rows=rows, auto_create=True, table=table)
    assert set(r["created"]) >= {"original"}
    bank = entity_registry.get_entity("original", table=table)
    assert bank["industries"] == ["banking"]
    # official BCB listing -> radar tier 'registry'
    assert reg_coverage.entity_radar(bank)["tier"] == "registry"


def test_bcb_conglomerate_arm_nests_and_keeps_parent_intact():
    from src.synth import entity_registry
    from src.synth.entity_discovery import discover_bcb_institutions
    table = _FakeTable()
    entity_registry.put_entity("inter", "Inter", ["INTER"], industries=["banking"], table=table)
    rows = [_bcb_row("INTER S.A.", "00416968", "Instituição de Pagamento")]
    r = discover_bcb_institutions(rows=rows, auto_create=True, table=table)
    arm = entity_registry.get_entity("inter-pagamentos", table=table)
    assert arm and arm["parent"] == "inter" and arm["industries"] == ["fintech"]
    # the tier-1 parent is untouched (not demoted to fintech, not overwritten)
    assert entity_registry.get_entity("inter", table=table)["industries"] == ["banking"]


def test_bcb_cnpj_match_enriches_not_duplicates_and_is_idempotent():
    from src.synth import entity_registry
    from src.synth.entity_discovery import discover_bcb_institutions
    table = _FakeTable()
    entity_registry.put_entity("itau", "Itaú", ["ITAU"], industries=["banking"],
                               cnpj_roots=["60701190"], table=table)
    rows = [_bcb_row("ITAU UNIBANCO HOLDING S.A.", "60701190", "Banco")]
    r = discover_bcb_institutions(rows=rows, auto_create=True, table=table)
    assert r["created"] == []                       # matched by CNPJ -> no duplicate
    r2 = discover_bcb_institutions(rows=rows, auto_create=True, table=table)
    assert r2["created"] == [] and r2["already"] >= 1


def test_bcb_foreign_name_collision_is_proposed_not_merged():
    from src.synth import entity_registry
    from src.synth.entity_discovery import discover_bcb_institutions
    table = _FakeTable()
    # an existing entity whose id != its brand slug, so a same-brand newcomer collides
    entity_registry.put_entity("acme_holdings", "Acme", ["ACME"], industries=["banking"], table=table)
    rows = [_bcb_row("ACME S.A.", "70707070", "Banco")]
    r = discover_bcb_institutions(rows=rows, auto_create=True, table=table)
    assert r["created"] == [] and len(r["proposed"]) == 1


# --- #67/#68/#69 discover_bcb_institutions quality fixes ------------------------
def test_bcb_noncompetitor_captive_banks_are_filtered_not_created():
    from src.synth.entity_discovery import discover_bcb_institutions
    table = _FakeTable()
    rows = [
        _bcb_row("BANCO JOHN DEERE S.A.", "91884981", "Banco"),
        _bcb_row("BANCO HONDA S.A.", "03634220", "Banco"),
        _bcb_row("ABN AMRO CLEARING S.A.", "05612884", "Banco"),
        _bcb_row("BANCO NEON S.A.", "30306294", "Banco"),          # a real competitor
    ]
    r = discover_bcb_institutions(rows=rows, auto_create=True, table=table)
    assert r["created"] == ["neon"]                # only the competitor is created
    assert len(r["filtered"]) == 3                 # deere/honda/clearing filtered out
    assert "banco-john-deere" not in "".join(r["created"])


def test_bcb_create_budget_is_shared_across_classes():
    from src.synth.entity_discovery import discover_bcb_institutions
    table = _FakeTable()
    # 3 banks then 3 payment institutions; a budget of 2 must not be eaten by banks alone
    rows = [
        _bcb_row("BANCO ALFA S.A.", "10000001", "Banco"),
        _bcb_row("BANCO BETA S.A.", "10000002", "Banco"),
        _bcb_row("BANCO GAMA S.A.", "10000003", "Banco"),
        _bcb_row("DELTA INSTITUICAO DE PAGAMENTO S.A.", "10000004", "Instituição de Pagamento"),
        _bcb_row("EPSILON INSTITUICAO DE PAGAMENTO S.A.", "10000005", "Instituição de Pagamento"),
        _bcb_row("ZETA INSTITUICAO DE PAGAMENTO S.A.", "10000006", "Instituição de Pagamento"),
    ]
    r = discover_bcb_institutions(rows=rows, auto_create=True, max_new=2, table=table)
    from src.synth import entity_registry
    inds = {i for cid in r["created"]
            for i in (entity_registry.get_entity(cid, table=table)["industries"])}
    assert inds == {"banking", "fintech"}          # one of each class, not two banks


def test_bcb_brand_cleaner_fixes():
    from src.synth.entity_discovery import _clean_institution_brand as clean
    assert clean("BANCO J. SAFRA S.A.") == "safra"           # leading initial dropped
    assert clean("BNY MELLON BANCO S.A.") == "bny mellon"    # embedded 'banco' stripped
    assert clean("BANCO INDUSTRIAL DO BRASIL S.A.") is None  # generic -> propose, not create
    assert clean("BANCO NEON S.A.") == "neon"


def test_bcb_prominence_gate_proposes_microlender_tail():
    from src.synth.entity_discovery import discover_bcb_institutions
    table = _FakeTable()
    rows = [
        _bcb_row("BANCO NEON S.A.", "30306294", "Banco"),                       # auto-create
        _bcb_row("FFCRED SOCIEDADE DE CREDITO DIRETO S.A.", "40404041", "Crédito Direto (SCD)"),
        _bcb_row("RAPIDIUM SCD S.A.", "40404042", "Crédito Direto (SCD)"),
        _bcb_row("ACME FINANCIAMENTOS S.A.", "40404043", "Financeira (SCFI)"),
    ]
    r = discover_bcb_institutions(rows=rows, auto_create=True, table=table)
    assert r["created"] == ["neon"]                      # bank auto-created
    assert len(r["proposed"]) == 3                        # SCD/SCFI tail proposed, not created
    from src.synth import entity_registry
    assert entity_registry.get_entity("ffcred", table=table) is None   # not in the registry

    # opening the tail (empty propose set) auto-creates them instead
    table2 = _FakeTable()
    r2 = discover_bcb_institutions(rows=rows, auto_create=True, table=table2,
                                   propose_only_classes=frozenset())
    assert set(r2["created"]) >= {"neon", "ffcred", "rapidium"}


# --- #14 general NER harvest -----------------------------------------------------
def test_harvest_ner_extracts_new_companies_drops_known_and_gates_frequency():
    from src.synth.entity_discovery import harvest_ner
    narrs = [
        {"id": "n1", "narrative": "A fintech Zignet cresceu; o Banco Fictício S.A. teve lucro."},
        {"id": "n2", "narrative": "A gestora Arqueia Capital avança. O Banco Fictício ampliou crédito."},
        {"id": "n3", "narrative": "A securitizadora Novra emitiu CRIs; a seguradora Prumo Seguros entrou."},
    ]
    cands = harvest_ner(narrs, min_mentions=2, table=None)
    surfaces = {c["surface"] for c in cands}
    # Banco Fictício recurs (2 narratives) -> surfaces; single-mention names are gated out
    assert any("Fictício" in s for s in surfaces)
    assert "Zignet" not in surfaces and "Novra" not in surfaces   # only 1 mention each
    # brand capture stops at the first lowercase word (no verb-phrase bleed)
    all1 = {c["surface"] for c in harvest_ner(narrs, min_mentions=1)}
    assert "Zignet" in all1 and "Arqueia Capital" in all1
    assert not any("levantou" in s or "emitiu" in s or "entrou" in s for s in all1)


def test_harvest_ner_skips_generic_and_regulator_heads():
    from src.synth.entity_discovery import harvest_ner
    narrs = [{"id": "x", "narrative": "O Conselho Monetário Nacional e o Banco Central publicaram. "
                                       "A Comissão de Valores Mobiliários também."}] * 2
    surfaces = {c["surface"] for c in harvest_ner(narrs, min_mentions=1)}
    # regulator/generic heads are stripped -> no "Banco Central"/"Conselho ..." candidate
    assert not any(s.lower().startswith(("banco central", "conselho", "comissao")) for s in surfaces)
