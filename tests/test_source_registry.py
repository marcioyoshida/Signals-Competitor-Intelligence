"""ADR 019 Phase 1 — the declarative source/lens registry.

Locks the descriptive contract: the registry's derived lens sets + section list are what
candidates.py consumes, and every source's lens is a defined lens. These are the invariants
that let Phases 2–4 build on the registry safely.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import registry as r
from src.synth import candidates as c


def test_candidates_lens_sets_are_derived_from_registry():
    assert c.LENS_WEIGHT == r.lens_weight()
    assert c.HIGH_VALUE_SOLO_LENSES == r.solo_lenses()
    assert c.STRUCTURED_SUBJECT_LENSES == r.structured_subject_lenses()
    assert c.BACKDROP_LENSES == r.backdrop_lenses()


def test_collect_signals_sections_come_from_registry():
    # The (section_key, lens) pairs used by _collect_signals equal the registry's, in order.
    assert r.section_lens_pairs() == [(s.id, s.lens) for s in r.SOURCES]


def test_known_lens_policy_snapshot():
    # Guardrail against an accidental policy drift (these are the shipped values).
    assert r.lens_weight()["regulatory"] == 0.35 and r.lens_weight()["market"] == 0.08
    assert r.solo_lenses() == frozenset(
        {"regulatory", "antitrust", "sanctions", "fatos", "dou", "sec", "ofertas",
         "entrants", "funds", "contracts"})
    assert r.backdrop_lenses() == frozenset({"market"})
    assert "news" not in r.solo_lenses()             # news needs corroboration
    assert "news" not in r.structured_subject_lenses()


def test_every_source_lens_is_defined():
    for s in r.SOURCES:
        assert s.lens in r.LENSES, f"source {s.id} → undefined lens {s.lens}"


def test_shared_lens_sources_kept():
    # `funds` is fed by two sources (competitor + fiagro_moves); both must be present.
    funds_sources = [s.id for s in r.SOURCES if s.lens == "funds"]
    assert set(funds_sources) == {"competitor", "fiagro_moves"}


def test_vertical_taxonomy_and_known_verticals():
    # ADR 019 Phase 3b/4 — vertical → industries; FS spans all (no scoping), sectors fail closed.
    assert r.vertical_industries(r.VERTICAL_FS) is None            # None = all industries
    assert r.is_known_vertical(r.VERTICAL_FS)
    for v in r.SECTORIAL_VERTICALS:                                # pharma/health/… recognized
        assert r.is_known_vertical(v)
        assert r.vertical_industries(v) == frozenset()            # not-yet-seeded → empty (fail closed)
    assert not r.is_known_vertical("nope")
    assert r.vertical_industries("nope") == frozenset()           # unknown also fails closed
    assert set(r.KNOWN_VERTICALS) == {r.VERTICAL_FS, *r.SECTORIAL_VERTICALS}


def test_active_vertical_gates_sources():
    # Phase-3 forward check: sector-agnostic sources appear for any vertical; FS-only don't.
    fs = {s.id for s in r.active(r.VERTICAL_FS)}
    agnostic = {s.id for s in r.SOURCES if r.ALL in s.verticals}
    assert {"sanctions", "cade", "contracts"} <= agnostic <= fs   # 'all' sources included in FS
    retail = {s.id for s in r.active("retail")}
    assert agnostic <= retail                                     # agnostic present in retail
    assert "pix_moves" not in retail                              # FS-only excluded elsewhere
    assert r.active(None) == r.SOURCES                            # None = all
