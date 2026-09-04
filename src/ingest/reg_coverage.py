"""#2 CVM/BCB coverage scan + #14 radar-score — the regulated-segment coverage map.

Issue #2 asks: *scan all industries regulated by CVM and BCB, add them to the
ingestion.* This module makes that scan a **durable, testable artifact** instead of a
one-off: a declarative map of every segment the two market regulators (BCB, CVM)
license/supervise, each pinned to our `INDUSTRIES` taxonomy and to the ingestion
`SOURCE`(s) that feed it — signals vs. entity-registry sync. `coverage_report()`
reconciles the map against `src/ingest/registry.SOURCES` (what actually runs) so the
**gaps** — regulated segments with no entity-registry sync — fall out explicitly. Those
gaps ARE the #14 "Official Registry Sync" roadmap.

Scope note: #2 names CVM + BCB. SUSEP (seguros) and SPA/MF (apostas) are their own
regulators — tracked elsewhere and intentionally out of this map. CADE (antitrust) is a
cross-industry lens (#61), not a licensor of a segment.

Radar-score (#14): `radar_score()` grades how strong an entity's provenance is — an
official registry listing outranks a CNPJ-only or news-only sighting — so discovery can
rank a candidate's reliability. Pure function over the ADR-018 provenance confidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Canonical industry slugs (entity_registry.INDUSTRIES). Kept as a frozenset here to
# validate the map without importing the registry at module load.
INDUSTRY_SLUGS = frozenset({
    "acquiring", "advisory", "agri-funds", "asset-management", "banking", "betting",
    "consorcio", "crypto", "financial-data-analytics", "fintech", "insurance",
    "investment-banking", "private-markets", "real-estate-funds", "wealth-management",
    "closed-pension", "securitization",
})


@dataclass(frozen=True)
class Segment:
    """A market-regulator-licensed segment, pinned to taxonomy + ingestion sources."""
    regulator: str                       # "BCB" | "CVM"
    key: str                             # stable slug
    label: str                           # pt-BR
    industries: tuple[str, ...]          # taxonomy slugs it maps to
    signal_sources: tuple[str, ...]      # registry SOURCE ids that surface its events
    entity_sync: str | None = None       # the source/discovery that KEEPS its entity
    #                                      roster current (registry sync); None = gap
    note: str = ""


# The regulated-segment universe (BCB + CVM). `entity_sync` is the #14 registry-sync
# lever: where it is None we see the segment's SIGNALS but do not maintain its entity
# roster from the official list — that is the coverage gap to close.
SEGMENTS: tuple[Segment, ...] = (
    # --- BCB (market/prudential) --------------------------------------------------
    Segment("BCB", "bancos", "Bancos (múltiplos, comerciais, de investimento)",
            ("banking", "investment-banking"),
            ("regulatory", "dou", "news", "fatos"), entity_sync=None,
            note="majors seeded; no daily BCB-list sync -> #14"),
    Segment("BCB", "instituicoes-pagamento", "Instituições de pagamento (IP)",
            ("acquiring", "fintech"),
            ("regulatory", "new_entrants", "pix_moves", "news"), entity_sync=None,
            note="bcb_autorizacoes surfaces new IPs; not promoted to entities -> #14"),
    Segment("BCB", "sociedades-credito", "Sociedades de crédito (SCD/SEP/SCFI/financeiras)",
            ("fintech",),
            ("regulatory", "new_entrants", "news"), entity_sync=None),
    Segment("BCB", "cooperativas-credito", "Cooperativas de crédito",
            ("banking",),
            ("regulatory", "new_entrants"), entity_sync=None,
            note="fetched by bcb_autorizacoes (SedesCooperativas); no entity sync -> #14"),
    Segment("BCB", "consorcio", "Administradoras de consórcio",
            ("consorcio",),
            ("regulatory", "new_entrants", "news"), entity_sync="new_entrants",
            note="discover_consorcio (bcb_consorcio OLINDA) syncs the roster (#46)"),
    Segment("BCB", "corretoras-cambio", "Corretoras/distribuidoras de câmbio e títulos",
            ("investment-banking", "banking"),
            ("regulatory", "new_entrants", "news"), entity_sync=None),
    Segment("BCB", "registradoras", "Registradoras de recebíveis (CERC/TAG/B3)",
            ("securitization", "financial-data-analytics"),
            ("regulatory", "news"), entity_sync=None,
            note="receivables registration infra; no registrant sync -> #14"),
    # --- CVM (securities) ---------------------------------------------------------
    Segment("CVM", "fundos", "Fundos de investimento (FI/FIDC/FIP/ETF)",
            ("asset-management", "private-markets", "securitization"),
            ("competitor", "ofertas", "news"), entity_sync="competitor",
            note="cvm_fundos registrant list is the roster; FIDC = credit securitization"),
    Segment("CVM", "fii", "Fundos imobiliários (FII)",
            ("real-estate-funds",),
            ("competitor", "ofertas", "news"), entity_sync="competitor"),
    Segment("CVM", "fiagro", "Fundos do agronegócio (FIAGRO)",
            ("agri-funds",),
            ("competitor", "ofertas", "news"), entity_sync="fiagro_moves",
            note="cvm_fiagro + discover_fiagro sync the roster (ADR-011)"),
    Segment("CVM", "gestoras-administradoras", "Gestoras e administradoras de recursos",
            ("asset-management", "wealth-management"),
            ("ofertas", "news"), entity_sync=None,
            note="no CVM 'administradores' registrant sync -> #14"),
    Segment("CVM", "intermediarios", "Corretoras/distribuidoras (intermediários CVM)",
            ("investment-banking",),
            ("news",), entity_sync=None),
    Segment("CVM", "securitizadoras", "Securitizadoras (CRI/CRA) / cia. securitizadoras",
            ("securitization", "real-estate-funds"),
            ("ofertas", "fatos", "news"), entity_sync=None,
            note="CRI/CRA issuers; ofertas + IPE filings; no registrant sync -> #14"),
    Segment("CVM", "companhias-abertas", "Companhias abertas (emissores)",
            ("banking", "investment-banking"),
            ("fatos", "news"), entity_sync=None,
            note="cvm_ipe (fatos) + financials surface issuers; no registrant sync"),
    Segment("CVM", "psav-cripto", "Prestadoras de serviços de ativos virtuais",
            ("crypto",),
            ("regulatory", "news"), entity_sync=None,
            note="rule still forming; no registrant source -> #14"),
)


def _active_source_ids() -> set[str]:
    """Source ids that actually run for the FS vertical (import kept local)."""
    from src.ingest import registry as _reg
    return {s.id for s in _reg.active(_reg.VERTICAL_FS)}


def coverage_report(active_source_ids: set[str] | None = None) -> dict[str, Any]:
    """Reconcile the segment map against the running sources → covered / signal-only / gap.

    - ``entity_covered``: a registry sync keeps its roster current AND that source runs.
    - ``signal_only``: no live entity sync, but at least one signal source runs (we see
      the segment's events, not a maintained roster).
    - ``gap``: neither — a regulated segment we do not ingest at all.
    """
    active = active_source_ids if active_source_ids is not None else _active_source_ids()
    entity_covered, signal_only, gap = [], [], []
    for seg in SEGMENTS:
        row = {"regulator": seg.regulator, "key": seg.key, "label": seg.label,
               "industries": list(seg.industries), "note": seg.note}
        live_signals = sorted(set(seg.signal_sources) & active)
        row["signal_sources"] = live_signals
        if seg.entity_sync and seg.entity_sync in active:
            row["entity_sync"] = seg.entity_sync
            entity_covered.append(row)
        elif live_signals:
            signal_only.append(row)
        else:
            gap.append(row)
    by_reg: dict[str, int] = {}
    for seg in SEGMENTS:
        by_reg[seg.regulator] = by_reg.get(seg.regulator, 0) + 1
    return {
        "entity_covered": entity_covered,
        "signal_only": signal_only,
        "gap": gap,
        "summary": {
            "segments": len(SEGMENTS),
            "by_regulator": by_reg,
            "entity_covered": len(entity_covered),
            "signal_only": len(signal_only),
            "gap": len(gap),
            # the #14 registry-sync roadmap: segments seen only as signals or not at all.
            "sync_roadmap": [r["key"] for r in signal_only + gap],
        },
    }


# --- #14 radar-score ---------------------------------------------------------
# How strong is the evidence that an entity is real + correctly classified? An official
# registry listing (fixture/curated seed, a structured filing) outranks a CNPJ-only or a
# news-only sighting. Grades the ADR-018 provenance `confidence`, so discovery can rank a
# candidate and the dashboard can show a reliability tier. Not a threat score.
_RADAR_TIERS: dict[str, tuple[str, float]] = {
    "fixture": ("official", 1.0),
    "curated": ("official", 1.0),
    "structured": ("registry", 0.8),
    "discovery": ("registry", 0.7),
    "cnpj": ("identified", 0.5),
    "enrich": ("news", 0.35),
    "inferred": ("news", 0.3),
}


def radar_score(confidence: str | None) -> dict[str, Any]:
    """Map an entity's strongest provenance `confidence` to a radar tier + 0–1 score."""
    tier, score = _RADAR_TIERS.get(str(confidence or "").lower(), ("unknown", 0.2))
    return {"tier": tier, "score": score, "confidence": confidence}


def entity_radar(entity: dict[str, Any]) -> dict[str, Any]:
    """Radar score for a registry entity from its best per-field provenance confidence
    (ADR-018 `_prov`), falling back to the entity-level `confidence`."""
    prov = (entity or {}).get("_prov") or {}
    confs = [v.get("confidence") for v in prov.values() if isinstance(v, dict) and v.get("confidence")]
    best = None
    best_score = -1.0
    for c in confs + [entity.get("confidence")]:
        s = radar_score(c)["score"]
        if s > best_score:
            best, best_score = c, s
    return radar_score(best)
