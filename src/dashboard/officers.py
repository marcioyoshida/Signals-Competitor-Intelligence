"""Four specialist officer personas + the chief-of-staff router (ADR 020, Phases 2–3).

An **officer** is a domain **lens** (its grounding sources + a read persona) plus a
**bounded action catalog** (a subset of the `/api/act` intents it may emit). This module
is the *declarative* contract; enforcement lives in `act_api` (writes) and `agent_ask`
(the persona read). Officers are **runtime product agents acting within Onça** — distinct
from the dev-time Claude Code subagents in `.claude/agents/`.

Phase 3 adds the **chief-of-staff router** (`route`) — classify a free-text intent to the
right officer — and **hand-off** (`owner_of`): an action that belongs exclusively to one
officer is routed to that owner when another officer (or an unscoped operator) emits it, so
the Regulator's "roll this back" lands on the Compliance officer, journaled.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Officer:
    role: str            # strategic | regulator | compliance | product
    title: str           # display name (pt-BR)
    mandate: str         # persona preamble prepended to the grounded-read system contract
    primary_lens: str    # soft grounding bias for the read
    cues: tuple[str, ...]     # router keywords → this officer (accent-folded match)
    actions: tuple[str, ...]  # allowed /api/act intents (subset of the global catalog)


OFFICERS: dict[str, Officer] = {
    "strategic": Officer(
        role="strategic",
        title="Estrategista-chefe (CSO)",
        mandate=(
            "Você é o Estrategista-chefe da Onça. Sua lente é a estratégia competitiva: "
            "o mapa de posição ameaça×expansão, SWOT/TOWS/Porter e os frameworks, "
            "momentum/ameaça e os grupos econômicos. Priorize onde cada concorrente ganha "
            "ou perde terreno e a tese competitiva."
        ),
        primary_lens="estrategia",
        cues=(
            "estrategia", "concorrente", "competidor", "competicao", "tese", "swot",
            "porter", "tows", "posicao", "ameaca", "expansao", "momentum", "grupo",
            "mercado", "participacao",
        ),
        actions=("open_watch", "curate_belief", "trigger_run", "record_decision", "set_outcome", "append_reference", "record_engagement"),
    ),
    "regulator": Officer(
        role="regulator",
        title="Oficial regulatório (CRO)",
        mandate=(
            "Você é o Oficial regulatório da Onça. Sua lente é o eixo regulatório: mudanças "
            "normativas, diffs de seção, blast-radius/dificuldade, prazos e os documentos "
            "regulatórios. Priorize o que mudou, quem é afetado e os prazos de vigência."
        ),
        primary_lens="regulacao",
        cues=(
            "regulacao", "regulatorio", "norma", "normativo", "resolucao", "circular",
            "instrucao", "cvm", "bcb", "susep", "previc", "prazo", "vigencia", "mudanca",
            "deliberacao", "portaria",
        ),
        actions=("open_watch", "trigger_run", "record_decision", "set_outcome", "append_reference", "record_engagement"),
    ),
    "compliance": Officer(
        role="compliance",
        title="Oficial de compliance (CCO)",
        mandate=(
            "Você é o Oficial de compliance da Onça. Sua lente é integridade/governança e "
            "risco: achados de integridade, sanções (CEIS/CNEP), antitruste (CADE), distress "
            "societário e o log de curadoria. Priorize riscos de integridade, sanções e "
            "insolvência na carteira."
        ),
        primary_lens="integridade",
        cues=(
            "integridade", "sancao", "sancoes", "ceis", "cnep", "cade", "antitruste",
            "distress", "recuperacao", "falencia", "rollback", "reverter", "auditoria",
            "risco", "governanca", "insolvencia",
        ),
        actions=("run_integrity_audit", "flag_entity", "rollback_field", "revert_entity", "record_decision", "set_outcome", "append_reference", "record_engagement"),
    ),
    "product": Officer(
        role="product",
        title="Oficial de produto (CPO)",
        mandate=(
            "Você é o Oficial de produto da Onça. Sua lente é produto/mercado e cobertura: a "
            "fila de lacunas de cobertura, o mapa CVM/BCB, propostas de descoberta e o "
            "radar-score, além do encaixe de mercado (JTBD). Priorize pontos cegos e cobertura."
        ),
        primary_lens="cobertura",
        cues=(
            "cobertura", "lacuna", "cego", "descoberta", "radar", "proposta", "vertical",
            "jtbd", "produto", "fonte", "detector", "onboarding", "entrante",
        ),
        actions=("resolve_review", "propose_vertical", "propose_registry_change", "record_decision", "set_outcome", "append_reference", "record_engagement"),
    ),
}

# The router's fallback when nothing matches — product owns "where are our blind spots?"
_DEFAULT_ROLE = "product"

# ADR-021 uses the buyer-facing C-suite titles (CSO/CRO/CCO/CPO); ADR-020 keys officers by
# role. They are the same four officers — accept either spelling everywhere via this alias map.
_ALIASES = {"cso": "strategic", "cro": "regulator", "cco": "compliance", "cpo": "product"}


_REVERSE_ALIASES = {v: k for k, v in _ALIASES.items()}


def resolve_role(role: str | None) -> str | None:
    """Canonicalize an officer id (accepts the CSO/CRO/CCO/CPO aliases → role key)."""
    r = (role or "").strip().lower()
    return _ALIASES.get(r, r) if (r in _ALIASES or r in OFFICERS) else None


def short_role(role: str | None) -> str | None:
    """The buyer-facing C-suite id (cso/cro/cco/cpo) for a role key or alias — the form the
    decision log / KB precedent metadata is keyed by."""
    canonical = resolve_role(role)
    return _REVERSE_ALIASES.get(canonical) if canonical else None


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFD", str(s or ""))
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()


def is_officer(role: str) -> bool:
    return resolve_role(role) is not None


def get(role: str) -> Officer | None:
    return OFFICERS.get(resolve_role(role) or "")


def catalog(role: str) -> tuple[str, ...]:
    off = OFFICERS.get(resolve_role(role) or "")
    return off.actions if off else ()


def owner_of(intent: str) -> str | None:
    """The officer that owns ``intent`` EXCLUSIVELY (exactly one officer lists it), else
    None (a shared action like ``trigger_run``/``open_watch``, or an operator-only action).
    Drives hand-off: an exclusively-owned action emitted by another officer is routed to
    its owner."""
    owners = [r for r, o in OFFICERS.items() if intent in o.actions]
    return owners[0] if len(owners) == 1 else None


def route(text: str) -> str:
    """Chief-of-staff dispatch: classify a free-text intent to the best officer by
    accent-folded cue overlap. Deterministic (a cheap router model is the documented
    upgrade path). Ties break by OFFICERS order; no signal ⇒ the default officer."""
    toks = set(re.findall(r"[a-z0-9]{3,}", _fold(text)))
    best_role, best_score = _DEFAULT_ROLE, 0
    for role, off in OFFICERS.items():
        score = sum(1 for cue in off.cues if _fold(cue) in toks)
        if score > best_score:
            best_role, best_score = role, score
    return best_role


def brief_persona(role: str) -> str | None:
    """The officer's mandate preamble for the grounded read (prepended to the Ask system
    contract, never replacing its grounding/citation/anti-fabrication rules)."""
    off = OFFICERS.get(resolve_role(role) or "")
    return off.mandate if off else None


def primary_lens(role: str) -> str | None:
    off = OFFICERS.get(resolve_role(role) or "")
    return off.primary_lens if off else None


def roster() -> list[dict[str, object]]:
    """Public roster for a client/officer picker: role, title, lens, action catalog."""
    return [
        {"role": o.role, "title": o.title, "primary_lens": o.primary_lens,
         "actions": list(o.actions)}
        for o in OFFICERS.values()
    ]
