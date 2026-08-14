"""Citation guardrail — only URLs present in source evidence may appear.

Enforces CLAUDE.md "no uncited claims" in code, not prompt hope.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

_URL_RE = re.compile(r"https?://[^\s\]\)\"'<>]+", re.IGNORECASE)

# Placeholder "citations" the LLM emits when a source has no real link. Two
# costumes seen live: "(URL: CVM-Ofertas)" / "URL: None", and angle-bracket
# pseudo-links "<BCB-Autorizacoes>". Both mimic a citation without being one.
_FAKE_URL_RE = re.compile(
    r"\s*\(?\s*url\s*[:=]\s*(?!https?://)[^)\n]*?\)?(?=[\s.,;)]|$)",
    re.IGNORECASE,
)
# <https://real/link> → keep the bare URL (markdown autolink).
_ANGLE_LINK_RE = re.compile(r"<\s*(https?://[^>\s]+)\s*>", re.IGNORECASE)
# <anything-that-is-not-a-link> → drop entirely (source-name pseudo-link).
_ANGLE_FAKE_RE = re.compile(r"\s*<\s*(?!https?://)[^>\n]*?>", re.IGNORECASE)


def scrub_fake_url_tokens(text: str) -> str:
    """Strip non-link citation placeholders; keep real URLs; tidy whitespace."""
    cleaned = _ANGLE_LINK_RE.sub(r"\1", text or "")
    cleaned = _ANGLE_FAKE_RE.sub("", cleaned)
    cleaned = _FAKE_URL_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+([.,;])", r"\1", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def collect_allowed_urls(sources: list[dict[str, Any]]) -> set[str]:
    """Gather citation URLs from source records (normalized, no trailing punct)."""
    allowed: set[str] = set()
    for src in sources:
        for key in ("url", "source_url", "link"):
            raw = src.get(key)
            if not raw or not isinstance(raw, str):
                continue
            cleaned = _normalize_url(raw)
            if cleaned:
                allowed.add(cleaned)
    return allowed


def _normalize_url(url: str) -> str:
    url = url.strip().rstrip(".,;:)")
    if not url.startswith("http"):
        return ""
    try:
        parts = urlparse(url)
        if not parts.netloc:
            return ""
        # Drop fragment; keep query (some CVM/BCB links use it).
        return f"{parts.scheme}://{parts.netloc}{parts.path}" + (
            f"?{parts.query}" if parts.query else ""
        )
    except ValueError:
        return ""


def extract_urls(text: str) -> list[str]:
    return [_normalize_url(m.group(0)) for m in _URL_RE.finditer(text or "") if _normalize_url(m.group(0))]


def enforce_citations(
    narrative: str,
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    """Strip sentences with disallowed URLs; return cleaned text + citation list.

    If no allowed citations remain, narrative is empty (caller should drop).
    """
    allowed = collect_allowed_urls(sources)
    if not narrative or not narrative.strip():
        return {"narrative": "", "citations": [], "dropped_urls": [], "ok": False}

    narrative = scrub_fake_url_tokens(narrative)
    if not narrative:
        return {"narrative": "", "citations": [], "dropped_urls": [], "ok": False}

    sentences = _split_sentences(narrative)
    kept: list[str] = []
    dropped: list[str] = []
    used: list[str] = []

    for sent in sentences:
        urls = extract_urls(sent)
        bad = [u for u in urls if u not in allowed]
        if bad:
            dropped.extend(bad)
            # Drop entire sentence if it cites unallowed material.
            continue
        kept.append(sent)
        for u in urls:
            if u in allowed and u not in used:
                used.append(u)

    # If narrative had no URLs at all, still require attaching source citations
    # when sources provide them — product feed must be citable.
    cleaned = " ".join(s.strip() for s in kept if s.strip())
    if cleaned and not used and allowed:
        used = sorted(allowed)[:5]

    citations = [{"url": u} for u in used]
    # Also allow sources without URLs to appear as id-only citations.
    for src in sources:
        if src.get("url"):
            continue
        sid = src.get("id")
        if sid:
            citations.append({"id": sid, "source": src.get("source")})

    ok = bool(cleaned) and bool(citations)
    return {
        "narrative": cleaned if ok else "",
        "citations": citations if ok else [],
        "dropped_urls": sorted(set(dropped)),
        "ok": ok,
    }


def _split_sentences(text: str) -> list[str]:
    """Lightweight sentence split on .!? followed by space/end."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p.strip()]
