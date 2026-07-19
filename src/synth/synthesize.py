"""Build flagged narratives from candidates — LLM preferred, heuristic fallback."""
from __future__ import annotations

import datetime as dt
from typing import Any

from src.synth import bedrock_llm, citations


SYSTEM = (
    "You are a competitive-intelligence analyst for Brazilian financial services. "
    "Write a short flagged briefing (3–6 sentences). "
    "Every factual claim about a filing or rule must include the source URL "
    "exactly as provided in the Sources list. Do not invent URLs or numbers. "
    "If uncertain, say so. Prefer Portuguese for regulatory titles when present."
)

ENTITY_LABELS = {
    "nubank": "Nubank / Nu Holdings",
    "stone": "Stone / StoneCo",
    "pagseguro": "PagSeguro / PagBank",
    "inter": "Inter&Co",
    "xp": "XP Inc / XP Investimentos",
    "itau": "Itaú",
    "btg": "BTG Pactual",
    "bradesco": "Bradesco",
    "santander": "Santander",
    "bb": "Banco do Brasil",
    "caixa": "Caixa Econômica Federal",
    "picpay": "PicPay",
    "mercado_pago": "Mercado Pago",
    "c6": "C6 Bank",
    "original": "Banco Original",
    "neon": "Neon",
}


def synthesize_candidate(
    candidate: dict[str, Any],
    *,
    use_llm: bool = True,
) -> dict[str, Any] | None:
    """Return a narrative record with enforced citations, or None if unusable."""
    sources = list(candidate.get("sources") or [])
    raw_text: str | None = None
    mode = "heuristic"

    if use_llm:
        prompt = _build_prompt(candidate)
        raw_text = bedrock_llm.converse(
            prompt,
            model_id=bedrock_llm.DEFAULT_SYNTH_MODEL,
            system=SYSTEM,
        )
        if raw_text:
            mode = "llm"

    if not raw_text:
        raw_text = _heuristic_narrative(candidate)
        mode = "heuristic"

    guarded = citations.enforce_citations(raw_text, sources)
    if not guarded["ok"]:
        raw_text = _heuristic_narrative(candidate, force_urls=True)
        guarded = citations.enforce_citations(raw_text, sources)
        mode = "heuristic"
        if not guarded["ok"]:
            return None

    return {
        "id": candidate.get("id"),
        "kind": candidate.get("kind"),
        "entity": candidate.get("entity"),
        "entities": candidate.get("entities") or [],
        "lenses": candidate.get("lenses") or [],
        "is_alert": bool(candidate.get("is_alert")),
        "threat_score": candidate.get("threat_score"),
        "threat_score_note": "estimated_heuristic",
        "narrative": guarded["narrative"],
        "citations": guarded["citations"],
        "dropped_urls": guarded["dropped_urls"],
        "mode": mode,
        "as_of": dt.date.today().isoformat(),
        "source_ids": [s.get("id") for s in sources if s.get("id")],
    }


def _build_prompt(candidate: dict[str, Any]) -> str:
    lines = [
        f"Entity: {candidate.get('entity') or candidate.get('entities') or 'n/a'}",
        f"Lenses: {', '.join(candidate.get('lenses') or [])}",
        "Sources (use only these URLs):",
    ]
    for s in candidate.get("sources") or []:
        lines.append(
            f"- lens={s.get('_lens')} id={s.get('id')} source={s.get('source')} "
            f"url={s.get('url') or '(none)'} summary={_one_line(s)}"
        )
    lines.append("")
    lines.append("Write the flagged briefing now, fusing the lenses into one narrative.")
    return "\n".join(lines)


def _one_line(src: dict[str, Any]) -> str:
    for key in (
        "subject",
        "fund_name",
        "issuer",
        "name",
        "company",
        "institution",
        "security",
        "modality",
        "ticker",
        "form",
    ):
        if src.get(key):
            extra = ""
            if src.get("pct_change") is not None:
                extra = f" pct_change={src.get('pct_change')}"
            if src.get("pl") is not None:
                extra += f" pl={src.get('pl')}"
            return f"{src[key]}{extra}"[:220]
    return str(src.get("id") or "")[:80]


def _heuristic_narrative(
    candidate: dict[str, Any],
    *,
    force_urls: bool = False,
) -> str:
    """Deterministic multi-lens briefing from structured fields."""
    seed = candidate.get("seed") or {}
    related = candidate.get("related") or []
    entity = candidate.get("entity")
    entity_label = ENTITY_LABELS.get(str(entity or ""), str(entity or "").title() or None)
    parts: list[str] = []

    if candidate.get("kind") == "entity_fusion" and entity_label:
        lenses = ", ".join(candidate.get("lenses") or [])
        parts.append(
            f"Fused competitor signal on {entity_label} across lenses [{lenses}]."
        )

    parts.append(_describe_signal(seed))
    for rel in related[:4]:
        parts.append(_describe_signal(rel, prefix="Related"))

    if force_urls:
        for s in candidate.get("sources") or []:
            if s.get("url"):
                parts.append(f"Source: {s['url']}")

    return " ".join(p for p in parts if p)


def _describe_signal(sig: dict[str, Any], prefix: str = "") -> str:
    lens = sig.get("_lens") or ""
    url = sig.get("url") or ""
    alert = " [NEW]" if sig.get("is_new") else ""
    head = f"{prefix}: " if prefix else ""

    if lens == "regulatory" or sig.get("doc_type") or sig.get("subject"):
        return (
            f"{head}Regulatory{alert}: {sig.get('doc_type') or 'document'} "
            f"{sig.get('number') or ''} — {sig.get('subject') or 'n/a'}. {url}"
        ).strip()
    if lens == "sec" or sig.get("ticker") and sig.get("form"):
        return (
            f"{head}SEC filing{alert}: {sig.get('ticker')} {sig.get('form')} "
            f"({sig.get('company') or ''}) filed {sig.get('filed') or ''}. {url}"
        ).strip()
    if lens == "ofertas" or sig.get("issuer") or sig.get("security"):
        amt = sig.get("amount")
        amt_s = f" amount={amt}" if amt is not None else ""
        return (
            f"{head}CVM offering{alert}: {sig.get('security') or 'instrument'} by "
            f"{sig.get('issuer') or 'issuer'}; lead {sig.get('leader') or 'n/a'}"
            f"{amt_s}. {url}"
        ).strip()
    if lens == "inf_diario" or sig.get("pl") is not None and sig.get("fund_name"):
        pct = sig.get("pct_change")
        pct_s = f" move={pct}%" if pct is not None else ""
        return (
            f"{head}Fund AUM{alert}: {sig.get('fund_name') or sig.get('cnpj')} "
            f"PL={sig.get('pl')}{pct_s} (admin {sig.get('admin') or 'n/a'}). {url}"
        ).strip()
    if lens == "pix" or (sig.get("ispb") and sig.get("tx_value") is not None):
        return (
            f"{head}Pix metric{alert}: {sig.get('institution') or sig.get('ispb')} "
            f"value={sig.get('tx_value')} pct_change={sig.get('pct_change')}. {url}"
        ).strip()
    if lens == "juros" or sig.get("rate_year") is not None:
        return (
            f"{head}Pricing{alert}: {sig.get('institution') or sig.get('cnpj8')} "
            f"{sig.get('modality') or ''} rate_year={sig.get('rate_year')} "
            f"pct_change={sig.get('pct_change')}. {url}"
        ).strip()
    if lens == "entrants" or sig.get("entity_type"):
        return (
            f"{head}New entrant{alert}: {sig.get('name') or sig.get('cnpj')} "
            f"({sig.get('entity_type') or 'entity'})."
        ).strip()
    if lens == "market" or sig.get("share_pct") is not None:
        return (
            f"{head}Market share context: {sig.get('institution')} "
            f"share_pct={sig.get('share_pct')} value={sig.get('value')}."
        ).strip()
    if sig.get("fund_name"):
        return (
            f"{head}Fund filing{alert}: {sig.get('fund_name')} "
            f"(admin {sig.get('admin') or 'n/a'}). {url}"
        ).strip()
    return f"{head}Signal{alert} id={sig.get('id')}. {url}".strip()
