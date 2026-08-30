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
