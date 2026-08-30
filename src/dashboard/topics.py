"""Narrative topic taxonomy (ADR #34 Phase 2).

A single coarse, business-meaningful classification that spans the two disjoint
evidence fields — derived cards carry an ``axis``, base-news carries ``lenses`` —
into one ``topics`` list per card. Derived at FEED-BUILD time (from the fields a
card already has), so there is no synth-schema change and no historical backfill:
every card, new or old, is classified the moment the feed is built.

Consumers: the dashboard topic filter (``feed.topic_options`` + per-card
``topics``) and the agent's grounding boost (``question_topics`` vs a card's
``topics``). The framework evidence gate stays on axis+lens (Phase 1) — topic is a
human/retrieval rollup, not the framework binding.
"""
from __future__ import annotations

from typing import Any, Iterable

# Ordered so the dashboard shows a stable, sensible filter order.
TOPICS = (
    "regulacao", "pagamentos", "credito", "mercado_capitais", "fundos",
    "concorrencia", "novos_entrantes", "analise", "geral",
)

TOPIC_LABELS = {
    "regulacao": "Regulação",
    "pagamentos": "Pagamentos",
    "credito": "Crédito & Juros",
    "mercado_capitais": "Mercado de capitais",
    "fundos": "Fundos",
    "concorrencia": "Concorrência",
    "novos_entrantes": "Novos entrantes",
    "analise": "Análise / Inferência",
    "geral": "Geral",
}

# News-source "lenses" → topic. ``news`` is deliberately omitted: it sits on almost
# every card, so it would make the filter useless — a card with only the ``news``
# lens (and nothing more specific) falls through to ``geral``.
LENS_TO_TOPIC = {
    "regulatory": "regulacao", "dou": "regulacao",
    "pix": "pagamentos",
    "juros": "credito",
    "fatos": "mercado_capitais", "sec": "mercado_capitais",
    "ofertas": "mercado_capitais", "market": "mercado_capitais",
    # "funds" also covers FIAGRO agri-funds moves (task b, digest key
    # `fiagro_moves` — reuses the "funds" lens in candidates.py, see comment
    # there). Deliberately not a separate "agri-funds" topic: this taxonomy is
    # a coarse, fixed set (module docstring); the agri-funds-specific cut
    # already exists via the entities registry `industry` rollup
    # (feed.json.industries / entity_industry_map), which every fiagro_moves
    # card reaches automatically once its entity resolves — no topics.py
    # change needed for that distinction.
    "funds": "fundos", "inf_diario": "fundos",
    "entrants": "novos_entrantes",
}

# Derived belief-axis / detector cards → topic.
AXIS_TO_TOPIC = {
    "regulatory": "regulacao", "regulatory_lifecycle": "regulacao",
    "comparative": "concorrencia", "cohort": "concorrencia",
    "behavioral": "analise", "longitudinal": "analise", "silence": "analise",
    "relational": "analise", "predictive": "analise", "ecosystem": "analise",
    "thematic": "analise",
}

# Question-intent cues (accent-folded, lowercase substrings) → topic, for the agent
# grounding boost. Kept small and unambiguous.
_QUESTION_CUES = {
    "regulacao": ("regula", "bacen", "banco central", "cvm", "susep", "dou", "norma", "resolucao", "lei "),
    "pagamentos": ("pix", "pagamento", "maquininha", "adquir", "cartao", "carteira digital"),
    "credito": ("credito", "juros", "selic", "inadimpl", "emprest", "financiamento", "spread"),
    "mercado_capitais": ("acao", "acoes", "oferta", "ipo", "fato relevante", "bolsa", "b3", "ticker", "dividendo", "recompra"),
    "fundos": ("fundo", "fii", "fiagro", "cota", "informe diario", "gestora"),
    "concorrencia": ("concorren", "market share", "participacao de mercado", "ranking", "lidera", "rival"),
    "novos_entrantes": ("novo entrante", "novos entrantes", "startup", "autorizacao", "entrou no mercado", "nova fintech"),
}


def _fold(s: Any) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def topics_of(card: dict[str, Any]) -> list[str]:
    """The topics a narrative/feed card touches (from its lenses + axis), ordered by
    TOPICS; ``["geral"]`` when nothing specific matches."""
    found: set[str] = set()
    for lens in card.get("lenses") or []:
        t = LENS_TO_TOPIC.get(str(lens))
        if t:
            found.add(t)
    ax = card.get("axis")
    if ax is not None:
        t = AXIS_TO_TOPIC.get(str(ax))
        if t:
            found.add(t)
    if not found:
        return ["geral"]
    return [t for t in TOPICS if t in found]


def question_topics(question: str) -> set[str]:
    """Topics a natural-language question is about (for the grounding boost)."""
    q = _fold(question)
    return {topic for topic, cues in _QUESTION_CUES.items() if any(c in q for c in cues)}


def topic_options(cards: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """Ordered [{slug, label}] for the topics actually present in ``cards`` — the
    dashboard filter control's options."""
    present: set[str] = set()
    for c in cards:
        present.update(c.get("topics") or [])
    return [{"slug": t, "label": TOPIC_LABELS[t]} for t in TOPICS if t in present]
