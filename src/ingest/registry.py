"""ADR 019 Phase 1 — declarative source & lens registry (descriptive).

Single source of truth for each ingestion source and each candidates lens. Phase 1 is
**non-breaking**: ``candidates.py`` derives its lens-policy sets (``LENS_WEIGHT``,
``HIGH_VALUE_SOLO_LENSES``, ``STRUCTURED_SUBJECT_LENSES``, ``BACKDROP_LENSES``) and its
section→lens list from here, reproducing the previously hand-maintained frozensets exactly.
Later phases drive the ``lambda_port`` pipeline loop and vertical selection from the same
specs (see ``docs/2026-08-31-adr-source-registry-verticals.md``).

Two specs, deliberately split (a small refinement of the ADR's single-spec sketch):
  - :class:`LensSpec` — the *lens policy* (weight + which lens-sets it belongs to). Lens policy
    is a property of the LENS, and several sources can share a lens (``competitor`` and
    ``fiagro_moves`` both feed ``funds``), so it lives once on the lens, not per source.
  - :class:`SourceSpec` — a *source*: its digest-section key → lens, plus ingestion metadata
    (resolution / delta / integration / cadence / verticals / gating) that Phases 2–4 consume.
    Phase 1 uses only ``section_key`` + ``lens``; the rest is forward-looking, with defaults.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Verticals (ADR 019). Consumed from Phase 3; declared now for readiness.
VERTICAL_FS = "financial-services"
ALL = "all"


@dataclass(frozen=True)
class LensSpec:
    """Scoring/clustering policy for one candidates lens."""
    name: str
    weight: float                    # -> LENS_WEIGHT
    solo: bool = False               # -> HIGH_VALUE_SOLO_LENSES (a lone NEW signal can surface)
    structured_subject: bool = False  # -> STRUCTURED_SUBJECT_LENSES (fuse only on shared entity)
    backdrop: bool = False           # -> BACKDROP_LENSES (never a solo seed)


@dataclass(frozen=True)
class SourceSpec:
    """One ingestion source: its digest section → lens, plus ingestion metadata."""
    id: str                          # digest section key (e.g. "cade", "pix_moves")
    lens: str                        # candidates lens this section feeds
    # --- forward-looking (ADR 019 Phases 2–4; NOT consumed in Phase 1) ---
    resolution: str = "text"         # "cnpj" | "name" | "prebound" | "macro" | "text"
    delta: str = "new"               # "new" | "moves" | "store" | "none"
    integration: str = "lens"        # "lens" (digest section + candidates) | "store" (index.json)
    cadence_days: int = 1
    verticals: frozenset[str] = field(default_factory=lambda: frozenset({VERTICAL_FS}))
    default_on: bool = True
    env_flag: str | None = None      # existing ONCA_* override, when a source is env-gated
    # Phase 2 — the digest section the source emits (consumed by the registry-driven runner):
    label: str | None = None         # human budget label; falls back to id
    state_key: str | None = None      # delta seen-set key (DynamoDbState); falls back to id
    seed_if_empty: bool = True       # suppress the first-run flood (False = report all on seed)
    items_limit: int = 10            # _tag_new(new[:items_limit])
    context_limit: int = 15          # _strip_raw(records[:context_limit])


# --- Lens policy — reproduces candidates.py's LENS_WEIGHT + the three *_LENSES sets ---------
LENSES: dict[str, LensSpec] = {
    "regulatory": LensSpec("regulatory", 0.35, solo=True),
    "antitrust":  LensSpec("antitrust", 0.33, solo=True),                       # #61 CADE
    "sanctions":  LensSpec("sanctions", 0.32, solo=True, structured_subject=True),  # #60
    "fatos":      LensSpec("fatos", 0.30, solo=True, structured_subject=True),
    "dou":        LensSpec("dou", 0.30, solo=True),
    "sec":        LensSpec("sec", 0.25, solo=True, structured_subject=True),
    "ofertas":    LensSpec("ofertas", 0.20, solo=True, structured_subject=True),
    "contracts":  LensSpec("contracts", 0.20, solo=True, structured_subject=True),  # #62 PNCP
    "entrants":   LensSpec("entrants", 0.18, solo=True, structured_subject=True),
    "funds":      LensSpec("funds", 0.15, solo=True, structured_subject=True),
    "inf_diario": LensSpec("inf_diario", 0.15, structured_subject=True),
    "pix":        LensSpec("pix", 0.12, structured_subject=True),
    "juros":      LensSpec("juros", 0.12, structured_subject=True),
    "news":       LensSpec("news", 0.12),
    "market":     LensSpec("market", 0.08, backdrop=True),
}

# --- Sources — reproduces _collect_signals' (section_key, lens) list, IN ORDER --------------
# Order matters: _collect_signals dedups by item id (first section wins), so preserve it.
# `verticals={ALL}` marks the sector-agnostic sources (they also serve the Anteater verticals);
# the rest are the financial-services vertical's current implementations.
SOURCES: list[SourceSpec] = [
    SourceSpec("regulatory", "regulatory", items_limit=8, context_limit=12,
               state_key="bcb_normativos", label="BCB normativos", seed_if_empty=False),
    SourceSpec("competitor", "funds", resolution="cnpj", items_limit=8, context_limit=12,
               state_key="cvm_fundos", label="CVM funds", seed_if_empty=False),
    SourceSpec("new_entrants", "entrants", resolution="cnpj", items_limit=8),
    SourceSpec("ofertas", "ofertas", resolution="cnpj",
               state_key="cvm_ofertas", label="CVM ofertas"),
    SourceSpec("fatos", "fatos", items_limit=12),
    SourceSpec("dou", "dou", label="Diário Oficial"),
    SourceSpec("sanctions", "sanctions", resolution="cnpj", verticals=frozenset({ALL}),     # #60
               integration="store", state_key="ceis_cnep", env_flag="ONCA_CEIS_CNEP",
               label="CEIS/CNEP sanctions"),
    SourceSpec("cade", "antitrust", resolution="name", verticals=frozenset({ALL}),          # #61
               state_key="cade", env_flag="ONCA_CADE", label="CADE antitrust"),
    SourceSpec("contracts", "contracts", resolution="cnpj", integration="store",            # #62
               default_on=False, env_flag="ONCA_PNCP_CONTRATOS", verticals=frozenset({ALL}),
               state_key="pncp_contratos", label="PNCP contracts"),
    SourceSpec("news", "news", resolution="text"),
    SourceSpec("sec_filings", "sec"),
    SourceSpec("pix_moves", "pix", delta="moves"),
    SourceSpec("juros_moves", "juros", delta="moves"),
    SourceSpec("inf_diario_moves", "inf_diario", delta="moves"),
    # FIAGRO agri-funds moves reuse the "funds" lens (deliberately — not a new lens), so one
    # material NEW move scores/alerts like a fresh CVM fund-class filing.
    SourceSpec("fiagro_moves", "funds", delta="moves", resolution="prebound"),
    SourceSpec("market", "market", resolution="macro"),
]

SPECS: dict[str, SourceSpec] = {s.id: s for s in SOURCES}


def by_id(source_id: str) -> SourceSpec:
    return SPECS[source_id]


# --- Derived views (consumed by candidates.py in Phase 1) -----------------------------------
def lens_weight() -> dict[str, float]:
    return {name: spec.weight for name, spec in LENSES.items()}


def solo_lenses() -> frozenset[str]:
    return frozenset(n for n, s in LENSES.items() if s.solo)


def structured_subject_lenses() -> frozenset[str]:
    return frozenset(n for n, s in LENSES.items() if s.structured_subject)


def backdrop_lenses() -> frozenset[str]:
    return frozenset(n for n, s in LENSES.items() if s.backdrop)


def section_lens_pairs() -> list[tuple[str, str]]:
    """(digest section key, lens) in order — the _collect_signals `sections` list."""
    return [(s.id, s.lens) for s in SOURCES]


def active(vertical: str | None = None) -> list[SourceSpec]:
    """Sources applicable to ``vertical`` (Phase 3 gate). ``None`` = all sources."""
    if not vertical:
        return list(SOURCES)
    return [s for s in SOURCES if ALL in s.verticals or vertical in s.verticals]
