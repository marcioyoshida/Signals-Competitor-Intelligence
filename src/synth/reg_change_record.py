"""ADR 009 §3 — the bounded LLM change-record (rated impact / blast_radius / difficulty).

Phase A gives the deterministic change list (what the act SAYS it does) and §2 the section
diff (what actually changed). This layer asks a bounded LLM to DESCRIBE and RATE that change
— impact, affected surfaces, blast radius, implementation difficulty — as **labeled
inference**, never sourced fact. Discipline (ADR guardrail): the change text + effective date
are grounded (Phase A / the deadline); only impact/blast/difficulty are the model's rated
read, and the grounded facts (`n_entities`, `affected_industries`) are taken from OUR data,
never the model's — the LLM cannot invent a count or a taxonomy tag.

Cost-bounded: only instruments with a real change (a Phase-A change list or a §2 diff), a
capped number per run, one short strict-JSON call each. Safe to call — returns None when
Bedrock is unavailable, so the pipeline never blocks on it.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from src.synth.bedrock_llm import converse as _default_converse

_SYSTEM = (
    "Você é um analista regulatório. Responda SOMENTE com um objeto JSON válido, sem texto "
    "fora do JSON. Baseie-se apenas no que foi fornecido; não invente números nem entidades. "
    "impacto, dificuldade e raio de alcance são INFERÊNCIA (leitura estimada), não fato."
)

_INDUSTRY_PT = {
    "acquiring": "Adquirência", "fintech": "Fintechs", "banking": "Bancos",
    "insurance": "Seguros", "investment-banking": "Banco de investimento",
    "consorcio": "Consórcios", "asset-management": "Gestão de ativos",
    "wealth-management": "Wealth", "real-estate-funds": "FIIs", "agri-funds": "FIAGRO",
}


def _prompt(label: str, domain: str, changes: list[dict[str, Any]],
            diff_summary: str | None, industries: list[str]) -> str:
    change_lines = "; ".join(
        (c.get("verb", "") + " " + " ".join(t.get("label", "") for t in (c.get("targets") or []))
         + (" (" + "; ".join(c.get("articles") or []) + ")" if c.get("articles") else "")).strip()
        for c in (changes or [])[:8]
    ) or "(sem lista determinística)"
    inds = ", ".join(_INDUSTRY_PT.get(s, s) for s in industries)
    return (
        f"Instrumento: {label}\nDomínio afetado: {domain}\n"
        f"Indústrias potencialmente afetadas: {inds}\n"
        f"Mudanças declaradas pelo ato (fonte): {change_lines}\n"
        + (f"Diferenças estruturais entre versões: {diff_summary}\n" if diff_summary else "")
        + "\nProduza um JSON com as chaves: "
        '"change" (frase curta do que muda), '
        '"affected_surfaces" (lista curta de sistemas/áreas afetadas, ex.: "base de clientes"), '
        '"impact" (frase: o que as equipes precisam fazer), '
        '"difficulty_score" (0 a 1), "difficulty_drivers" (lista curta), '
        '"action_required" (frase curta). '
        "Não inclua contagem de entidades nem tags de indústria — isso é calculado à parte."
    )


def _parse(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _clamp01(v: Any) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def _band(score: float, cuts: tuple[float, float], names: tuple[str, str, str]) -> str:
    return names[0] if score < cuts[0] else names[1] if score < cuts[1] else names[2]


def _strlist(v: Any, *, limit: int = 4, maxlen: int = 60) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [str(x)[:maxlen] for x in v if str(x).strip()][:limit]


def build_change_record(
    *, label: str, domain: str, industries: list[str], n_entities: int,
    changes: list[dict[str, Any]], diff_summary: str | None = None,
    effective_date: str | None = None, source_url: str | None = None,
    converse_fn: Callable[..., "str | None"] = _default_converse,
    model_id: str | None = None,
) -> dict[str, Any] | None:
    """Draft one change record. Grounded facts (industries, n_entities, effective_date) come
    from the caller; the LLM only rates impact/difficulty. Returns None if the LLM is down."""
    raw = converse_fn(_prompt(label, domain, changes, diff_summary, industries),
                      system=_SYSTEM, model_id=model_id, max_tokens=600)
    data = _parse(raw)
    if data is None:
        return None
    # blast radius: n_entities is CONCRETE (from the registry) — never the model's guess.
    band_blast = _band(float(n_entities), (3, 13), ("narrow", "sector", "market"))
    blast_score = round(min(1.0, n_entities / 20.0), 2)
    diff_score = _clamp01(data.get("difficulty_score"))
    return {
        "change": str(data.get("change") or "")[:400],
        "effective_date": effective_date,                      # grounded (the deadline)
        "affected_industries": list(industries),               # grounded (our taxonomy)
        "affected_surfaces": _strlist(data.get("affected_surfaces")),
        "impact": str(data.get("impact") or "")[:400],
        "blast_radius": {"score": blast_score, "band": band_blast, "n_entities": int(n_entities)},
        "difficulty": {"score": round(diff_score, 2),
                       "band": _band(diff_score, (0.34, 0.67), ("low", "medium", "high")),
                       "drivers": _strlist(data.get("difficulty_drivers"))},
        "action_required": str(data.get("action_required") or "")[:300],
        "source_url": source_url,
        "is_inference": True,                                  # ALWAYS: rated, not sourced
        "mode": "derived",
    }


def enrich_lifecycles(
    lifecycles: dict[str, dict[str, Any]], *, industry_counts: dict[str, int] | None = None,
    max_records: int = 20, converse_fn: Callable[..., "str | None"] = _default_converse,
    model_id: str | None = None,
) -> int:
    """Attach a `change_record` to each lifecycle that declares a real change (Phase A list),
    bounded by max_records. Mutates in place; returns how many were drafted."""
    from src.synth import reg_change, regulatory

    counts = industry_counts or {}
    drafted = 0
    for lc in lifecycles.values():
        if drafted >= max_records:
            break
        changes = reg_change.parse_changes(
            " ".join((m.get("summary") or "") for m in lc.get("timeline") or [])[:3000],
            self_key=lc.get("instrument"))
        if not changes:
            continue
        industries = regulatory._industries_for(lc.get("domain") or "")
        n_entities = sum(counts.get(i, 0) for i in industries)
        rec = build_change_record(
            label=lc.get("label") or lc.get("instrument") or "",
            domain=lc.get("domain") or "", industries=industries, n_entities=n_entities,
            changes=changes, effective_date=lc.get("deadline"),
            converse_fn=converse_fn, model_id=model_id)
        if rec:
            lc["change_record"] = rec
            drafted += 1
    return drafted
