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
    "If uncertain, say so. Use Portuguese or English; prefer Portuguese for "
    "regulatory titles when present."
)


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
        # Last resort: force a minimal citable heuristic and re-guard.
        raw_text = _heuristic_narrative(candidate, force_urls=True)
        guarded = citations.enforce_citations(raw_text, sources)
        mode = "heuristic"
        if not guarded["ok"]:
            return None

    return {
        "id": candidate.get("id"),
        "kind": candidate.get("kind"),
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
    lines = ["Sources (use only these URLs):"]
    for s in candidate.get("sources") or []:
        lines.append(
            f"- id={s.get('id')} source={s.get('source')} url={s.get('url') or '(none)'} "
            f"summary={_one_line(s)}"
        )
    lines.append("")
    lines.append("Write the flagged briefing now.")
    return "\n".join(lines)


def _one_line(src: dict[str, Any]) -> str:
    for key in (
        "subject",
        "fund_name",
        "issuer",
        "name",
        "institution",
        "security",
        "modality",
    ):
        if src.get(key):
            return str(src[key])[:200]
    return str(src.get("id") or "")[:80]


def _heuristic_narrative(
    candidate: dict[str, Any],
    *,
    force_urls: bool = False,
) -> str:
    """Deterministic briefing from structured fields — always attaches known URLs."""
    seed = candidate.get("seed") or {}
    related = candidate.get("related") or []
    parts: list[str] = []

    if seed.get("doc_type") or seed.get("subject"):
        url = seed.get("url") or ""
        parts.append(
            f"Regulatory signal: {seed.get('doc_type') or 'document'} "
            f"{seed.get('number') or ''} — {seed.get('subject') or 'n/a'}. "
            f"{url}".strip()
        )
    elif seed.get("fund_name"):
        url = seed.get("url") or ""
        parts.append(
            f"Competitor fund filing: {seed.get('fund_name')} "
            f"(admin {seed.get('admin') or 'n/a'}). {url}".strip()
        )
    elif seed.get("issuer") or seed.get("security"):
        url = seed.get("url") or ""
        parts.append(
            f"Securities offering: {seed.get('security') or 'instrument'} by "
            f"{seed.get('issuer') or 'issuer'}; lead {seed.get('leader') or 'n/a'}. "
            f"{url}".strip()
        )
    elif seed.get("name") or seed.get("cnpj"):
        parts.append(
            f"New supervised entity signal: {seed.get('name') or seed.get('cnpj')} "
            f"({seed.get('entity_type') or 'entity'})."
        )
    elif seed.get("pct_change") is not None:
        parts.append(
            f"Market metric move: {seed.get('institution') or seed.get('ispb') or 'institution'} "
            f"{seed.get('modality') or ''} changed {seed.get('pct_change')}% "
            f"(value {seed.get('rate_year') or seed.get('tx_value')})."
        )
    else:
        parts.append(f"Competitor signal id={seed.get('id')}.")

    for rel in related[:3]:
        url = rel.get("url") or ""
        label = (
            rel.get("fund_name")
            or rel.get("issuer")
            or rel.get("name")
            or rel.get("institution")
            or rel.get("id")
        )
        parts.append(f"Related competitor context: {label}. {url}".strip())

    if force_urls:
        for s in candidate.get("sources") or []:
            if s.get("url"):
                parts.append(f"Source: {s['url']}")

    return " ".join(p for p in parts if p)
