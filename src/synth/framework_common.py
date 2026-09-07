"""Shared per-dimension evidence binding for the strategy frameworks (ADR #34).

The narrative corpus classifies evidence across TWO disjoint fields: derived /
detector cards carry a stamped ``axis`` (comparative, longitudinal, regulatory, …)
while base-news narratives carry ``lenses`` (news, pix, entrants, juros, …) and no
axis. So a framework dimension binds to the evidence that actually evidences it via

    DIM_SIGNALS[dim] = (axis_set, lens_set)

A cited evidence item is *on-signal* for a dimension iff its ``axis`` is in
``axis_set`` OR any of its ``lenses`` is in ``lens_set``. Empty sets on BOTH sides
mean the dimension is unconstrained (any cited evidence counts) — the safe fallback
so a dimension is never silently starved by a mapping gap.

This realizes the ADR-006 addendum (#32) "hard, axis/lens-valid evidence link"
without an axis-only whitelist (which would drop the ~83% of narratives that are
axis-less). The gate can be disabled wholesale via ``ONCA_FRAMEWORK_SIGNAL_GATE=0``
(reversibility: falls back to the prior "any cited in-range evidence" behavior).
"""
from __future__ import annotations

import os
from typing import Any

# (axes, lenses) — either side empty is "no constraint from this side"; BOTH empty
# means the dimension accepts any cited evidence.
Signal = tuple[frozenset[str], frozenset[str]]


def fz(*items: str) -> frozenset[str]:
    """Terse frozenset constructor for the DIM_SIGNALS tables."""
    return frozenset(items)


def _gate_enabled() -> bool:
    return os.environ.get("ONCA_FRAMEWORK_SIGNAL_GATE", "1") not in ("0", "false", "False")


def evidence_cap(default: int = 20) -> int:
    """SURF-9 (#89): the per-entity evidence budget a framework hands the LLM drafter. Raised from
    the original 12 to give each assessment more grounding — the first, mechanical lever on the
    'frameworks read thin' problem (more evidence per bullet ⇒ more nuance). Env-overridable via
    ``ONCA_FRAMEWORK_EVIDENCE_CAP``. NB the deeper levers (financial-feature prompt injection +
    multi-pass reasoning) require live LLM eval and are tracked separately on #89."""
    try:
        return max(1, int(os.environ.get("ONCA_FRAMEWORK_EVIDENCE_CAP", default)))
    except (TypeError, ValueError):
        return default


def multipass_enabled() -> bool:
    """SURF-9 (#89): whether the frameworks run the self-critique/refine pass. On by default;
    disable wholesale with ``ONCA_FRAMEWORK_MULTIPASS=0`` (reversibility)."""
    import os

    return os.environ.get("ONCA_FRAMEWORK_MULTIPASS", "1") not in ("0", "false", "False")


_CRITIQUE = (
    "Revise criticamente o rascunho JSON acima como um analista sênior. Para CADA avaliação: "
    "remova as que não têm evidência clara ou são genéricas/vagas; refine a redação para ser "
    "específica, concreta e acionável (evite obviedades); garanta que cada avaliação cite ao menos "
    "um índice de evidência VÁLIDO da lista acima. Não invente fatos além das evidências/contexto "
    "dados. Mantenha EXATAMENTE o mesmo formato JSON (mesmas chaves). Retorne APENAS o JSON "
    "minificado revisado, sem prosa nem markdown."
)


def refine_draft(*, system: str, base_prompt: str, draft: str,
                 model_id: str | None = None, max_tokens: int = 1000) -> str:
    """SURF-9 (#89) multi-pass — a self-critique/refine pass over a framework's first draft: the
    drafter re-reads its own JSON against the evidence + rules and returns a sharper version
    (drops weak/vague/uncited assessments, tightens wording). Schema-agnostic (it preserves the
    JSON shape), so it serves all frameworks. Returns the revised raw, or the ORIGINAL draft on any
    failure/empty — never worse than a single pass."""
    if not (draft or "").strip():
        return draft
    from src.synth.bedrock_llm import converse

    prompt = f"{base_prompt}\n\nSeu rascunho:\n{draft.strip()}\n\n{_CRITIQUE}"
    revised = converse(prompt, model_id=model_id, system=system, max_tokens=max_tokens)
    return revised or draft


def financial_context_map(bucket: str | None = None, *, s3: Any | None = None) -> dict[str, str]:
    """SURF-9 (#89): per-entity compact financial context (IF.data / Pilar 3 / FinBERT) to ground
    the framework drafters — so Porter/BCG/Ansoff/… reason over REAL financials (ROE, Basileia,
    LCR/NSFR, tone), not only news claims. Best-effort: returns {} if the bucket/stores are
    unavailable (frameworks then draft exactly as before — non-breaking). This is grounding
    CONTEXT, labeled inference; the drafter still cites narrative-evidence indices."""
    import os

    bucket = bucket or os.environ.get("ONCA_DIGESTS_BUCKET")
    if not bucket:
        return {}
    fun: dict = {}
    snd: dict = {}
    km: dict = {}
    tone: dict = {}
    try:
        from src.ingest import bcb_fundamentals, bcb_km1, bcb_soundness
        from src.synth import financial_tone
    except Exception:  # pragma: no cover - optional deps
        return {}
    for load, proj, store in (
        (lambda: bcb_fundamentals.load_index(bucket, s3=s3), bcb_fundamentals.fundamentals_by_entity, "fun"),
        (lambda: bcb_soundness.load_index(bucket, s3=s3), bcb_soundness.soundness_by_entity, "snd"),
        (lambda: bcb_km1.load_index(bucket, s3=s3), bcb_km1.km1_by_entity, "km"),
        (lambda: financial_tone.load_index(bucket, s3=s3), financial_tone.tone_by_entity, "tone"),
    ):
        try:
            locals()[store].update(proj(load()))
        except Exception:  # pragma: no cover - each store best-effort
            pass
    out: dict[str, str] = {}
    for e in set(fun) | set(snd) | set(km) | set(tone):
        parts: list[str] = []
        f, s, k, t = fun.get(e) or {}, snd.get(e) or {}, km.get(e) or {}, tone.get(e) or {}
        if f.get("roe_pct") is not None:
            parts.append(f"ROE {f['roe_pct']}%")
        if f.get("roa_pct") is not None:
            parts.append(f"ROA {f['roa_pct']}%")
        if f.get("leverage") is not None:
            parts.append(f"alavancagem {f['leverage']}x")
        if f.get("lucro_share_pct") is not None:
            parts.append(f"{f['lucro_share_pct']}% do lucro do setor")
        if s.get("indice_basileia") is not None:
            parts.append(f"Basileia {s['indice_basileia']}%" + (f" ({s['band']})" if s.get("band") else ""))
        if k.get("lcr_pct") is not None:
            parts.append(f"LCR {k['lcr_pct']}%")
        if k.get("nsfr_pct") is not None:
            parts.append(f"NSFR {k['nsfr_pct']}%")
        if t.get("financial_tone_net") is not None:
            parts.append(f"tom financeiro {t['financial_tone_net']:+.2f}")
        if parts:
            out[e] = "Contexto financeiro (IF.data/Pilar 3/FinBERT, inferência): " + ", ".join(parts) + "."
    return out


def on_signal(ev: dict[str, Any], signal: Signal) -> bool:
    """True iff evidence ``ev`` matches ``signal`` (its axis in axis_set OR one of its
    lenses in lens_set). An unconstrained signal (both sets empty) always matches."""
    axes, lenses = signal
    if not axes and not lenses:
        return True
    ax = ev.get("axis")
    if ax is not None and ax in axes:
        return True
    return any(l in lenses for l in (ev.get("lenses") or []))


def on_signal_ids(
    cited_indices: list[Any],
    evidence: list[dict[str, Any]],
    dim: str,
    dim_signals: dict[str, Signal],
) -> list[str]:
    """The ids of cited evidence that are on-signal for ``dim`` (order-stable, deduped).

    ``cited_indices`` are indices into ``evidence`` (already validated in-range by the
    caller's ``_parse_draft``). A dimension absent from ``dim_signals`` is treated as
    unconstrained. With the gate disabled, every in-range cited id is kept."""
    gate = _gate_enabled()
    signal = dim_signals.get(dim)
    out: list[str] = []
    for j in cited_indices:
        try:
            j = int(j)
        except (TypeError, ValueError):
            continue
        if not (0 <= j < len(evidence)):
            continue
        ev = evidence[j]
        if gate and signal is not None and not on_signal(ev, signal):
            continue
        eid = ev.get("id")
        if eid and eid not in out:
            out.append(eid)
    return out
