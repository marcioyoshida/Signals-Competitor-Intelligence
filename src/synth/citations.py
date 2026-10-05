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

    citations = [_url_citation(u, sources) for u in used]
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


def _url_citation(url: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
    """{"url"} plus, for news, the publisher as ``label`` — a Google News redirect alone says
    nothing about who published the story (49% of feed links were opaque redirects, 10-04)."""
    out: dict[str, Any] = {"url": url}
    src = next((s for s in sources or [] if s.get("publisher")
                and _normalize_url(str(s.get("url") or "")) == url), None)
    if src is not None:
        out["label"] = str(src["publisher"])
        if "news.google.com" in url:
            out["via"] = "Google Notícias"
    return out


def _split_sentences(text: str) -> list[str]:
    """Lightweight sentence split on .!? followed by space/end."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p.strip()]


# --- plain-text surfaces ----------------------------------------------------------------------
# A narrative keeps its inline URLs: the dashboards turn each one into a numbered citation
# link. Where the same text is shown as PLAIN prose (an /exec panel title, an MCP summary, a
# digest line) a raw Google News redirect is noise — 875 inline URLs in 301 narratives on
# 10-04 — and the links are already in the card's citations. plain_text() drops each URL with
# the attribution phrase that only pointed at it ("conforme divulgado em <url>", "Source: <url>").
_PLAIN_URL = re.compile(r"https?://[^\s<>]+")
# a run of URLs (", " / " e " / bare-space separated); "https:" alone = a URL cut by truncation
_URL_RUN = re.compile(r"https?:(?://[^\s<>]*)?(?:\s*(?:,|;|\be\b|\band\b)?\s*https?:(?://[^\s<>]*)?)*")
_LEAD_IN = {"conforme", "segundo", "acordo", "com", "divulgado", "divulgada", "divulgados",
            "publicado", "publicada", "noticiado", "informado", "destacado", "registrado",
            "em", "no", "na", "nos", "nas", "pelo", "pela", "por", "via", "de", "incluindo",
            "fonte", "fontes", "fonte:", "fontes:", "source", "source:", "sources:", "link",
            "link:", "notícia", "notícias", "noticias", "diversas", "várias", "veja", "ver",
            "mais", "informações", "detalhes", "disponível", "disponíveis", "aqui", "site",
            "como", "pode", "ser", "visto", "vista", "vistos", "mencionado", "mencionada",
            "citado", "citada", "indicado", "reportado", "relatado", "anunciado", "comunicado:"}


def plain_text(text: Any) -> str:
    s = str(text or "")
    if "http" not in s:
        return s
    # brackets/parens that hold nothing but URLs go entirely
    s = re.sub(r"[\(\[]\s*" + _URL_RUN.pattern + r"\s*[\)\]]", "", s)
    out, last = [], 0
    for m in _URL_RUN.finditer(s):
        run = m.group()
        trail = re.search(r"[)\].,;:!?]+$", run)
        keep = trail.group() if trail else ""
        head = s[last:m.start()]
        words = head.rstrip().split(" ")
        while words and words[-1].lower().strip(",") in _LEAD_IN:
            words.pop()
        head = " ".join(words)
        # the URL often ended the sentence: "… de 2026 https://… Afeta:" keeps its full stop
        nxt = s[m.end():].lstrip()
        if not keep and nxt[:1].isupper() and head.strip() and not re.search(r"[.!?:]\s*$", head):
            head, keep = head.rstrip(" ,;"), "."
        out.append(head)
        out.append(keep)
        last = m.end()
    out.append(s[last:])
    s = "".join(out)
    s = re.sub(r"[\(\[]\s*(?:,|;|\be\b)\s+", lambda x: x.group()[0], s)   # "( e a CBA" -> "(a CBA"
    s = re.sub(r"[\(\[]\s*[\)\]]", "", s)
    s = re.sub(r"\s+([.,;:!?)\]])", r"\1", s)
    s = re.sub(r"([,;:])\s*([.!?])", r"\2", s)
    s = re.sub(r"\.{2,}", ".", s)
    s = re.sub(r",(?:\s*,)+", ",", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" ,;:").lstrip(".! ")
