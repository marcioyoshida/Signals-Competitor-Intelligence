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
