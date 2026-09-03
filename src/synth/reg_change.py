"""ADR 009 Phase A — Regulatory Change Intelligence: the amending-act parser.

The grounded FLOOR the ADR calls "higher-signal, easier": an amending act is a
machine-readable changelog — it says, in its own text, what it does ("altera a
Resolução CMN 5304", "revoga o art. 7º", "dá nova redação ao art. 12"). We parse those
change VERBS + the article/dispositivo references + the base instrument they target, into
discrete, cited change records. No LLM, no network: it reads the act text we already
ingest and attaches to the existing regulatory-lifecycle thread (`regulatory.py`).

Discipline (ADR 009 guardrail): the change text is SOURCED (it is the act's own words,
quoted) — this parser never rates impact/blast/difficulty. Those rated-inference
attributes are the later LLM phase; Phase A only enumerates what the act literally says.

Deferred to later phases: full-text `regdocs/` versioned store + section diff of versioned
documents (Manual do DICT etc.), and the bounded LLM change-record (impact/blast_radius/
difficulty tags). This module is the deterministic core those build on.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.lower()


# Change verbs an amending act uses about a prior act / its own articles. Order matters:
# a longer, more specific phrase is tried before a shorter one it contains.
# Cover both the act's dispositive PRESENT ("altera") and the news-narrative PAST
# ("alterou/alterada") — our regulatory narratives are largely news-derived, which use
# the preterite/participle, so a present-only pattern misses most real changes.
_VERBS: list[tuple[str, str, str]] = [
    # (relation, pt-BR label, accent-free regex)
    ("restates", "dá nova redação", r"(d[aá]|deu)\s+nova\s+redacao"),
    ("revokes", "revoga", r"revoga(?:m|do|da|r|ram|ndo|ções|cao)?\b|revogou\b"),
    ("amends", "altera", r"altera(?:m|do|da|dos|das|r|coes|cao|ram|ndo)?\b|alterou\b"),
    ("inserts", "inclui", r"inclui(?:u|ram|ndo|da|do)?\b"),
    ("adds", "acrescenta", r"acrescenta(?:m|r|ram|ndo)?\b|acrescentou\b"),
    ("renumbers", "renumera", r"renumera(?:m|r|ram)?\b|renumerou\b"),
    ("extends", "prorroga o prazo",
     r"(prorroga|prorrogou|amplia(?:r|ram)?\s+o\s+prazo|ampliou\s+o\s+prazo|adia|adiou)\b"),
    ("suspends", "suspende", r"suspende(?:m|r|ram|ndo)?\b|suspendeu\b"),
]
_VERB_RX = [(rel, lab, re.compile(rx)) for rel, lab, rx in _VERBS]

# Article / dispositivo references, most-specific first so a "§ 3º do art. 5º" reads as
# the article. Kept compact and cited; we do not resolve them to text (that's the diff
# phase once the base document is stored).
_ARTICLE_RX = re.compile(
    r"(art(?:igo)?s?\.?\s*\d+[\ºo\-]?(?:\s*a\s*\d+)?"          # art. 5 / artigos 5 a 9
    r"|inciso[s]?\s+[ivxlcdm]+"                                  # inciso II
    r"|§+\s*\d+[\ºo]?|paragrafo[s]?\s+\d+"                       # § 3 / parágrafo 3
    r"|alinea\s+[\"“']?\w"                                       # alínea "a"
    r"|anexo\s+[ivxlcdm\d]+"                                     # Anexo I
    r"|capitulo[s]?\s+[ivxlcdm\d]+"                              # Capítulo II
    r"|dispositivos?)"                                           # generic "dispositivos"
)

# How much text after a verb binds its article refs + amended target.
_WINDOW = 180


def _articles_in(segment: str) -> list[str]:
    seen: list[str] = []
    for m in _ARTICLE_RX.finditer(segment):
        a = re.sub(r"\s+", " ", m.group(1)).strip()
        if a not in seen:
            seen.append(a)
    return seen


def _targets_in(segment: str, self_key: str | None) -> list[dict[str, str]]:
    """Base instruments the change acts on (excluding the act itself)."""
    # Lazy import: reuse the instrument taxonomy so "altera a Resolução CMN 5304"
    # resolves the TARGET to the same stable key the axis threads by (avoids the
    # regulatory<->reg_change import cycle).
    from src.synth import regulatory as _reg

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for key, disp, _s, _e in _reg._mentions(segment, include_comunicados=False):
        if key == self_key or key in seen:
            continue
        seen.add(key)
        out.append({"instrument": key, "label": disp})
    return out


def parse_changes(text: str, *, self_key: str | None = None,
                  max_changes: int = 12) -> list[dict[str, Any]]:
    """Enumerate the discrete changes an amending act declares, in document order.

    Each record: {relation, verb (pt label), articles[], targets[], quote}. The quote is
    the act's own words (sourced); nothing here is rated inference."""
    raw = str(text or "")
    norm = _norm(raw)
    # All verb occurrences, in document order — each one's CLAUSE runs up to the next verb
    # (capped), so a verb binds only its own article refs + target, not later clauses'.
    hits: list[tuple[int, int, str, str]] = []
    for rel, label, rx in _VERB_RX:
        for m in rx.finditer(norm):
            hits.append((m.start(), m.end(), rel, label))
    hits.sort()

    changes: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for i, (start, _end, rel, label) in enumerate(hits):
        nxt = hits[i + 1][0] if i + 1 < len(hits) else len(norm)
        end = min(nxt, start + _WINDOW)
        seg = norm[start:end]
        if end < nxt:  # capped mid-clause — back off to a word boundary so a number
            sp = seg.rfind(" ")  # (e.g. "5130") is never sliced into a false "513"
            if sp > 40:
                seg = seg[:sp]
        articles = _articles_in(seg)
        targets = _targets_in(seg, self_key)
        # A bare verb with neither an article nor a target is too weak to enumerate
        # (avoids "altera" inside unrelated prose); keep only bound changes.
        if not articles and not targets:
            continue
        sig = (rel, ",".join(articles), ",".join(t["instrument"] for t in targets))
        if sig in seen:
            continue
        seen.add(sig)
        quote = re.sub(r"\s+", " ", raw[start: start + 140]).strip()
        changes.append({
            "relation": rel, "verb": label, "articles": articles,
            "targets": targets, "quote": quote,
        })
        if len(changes) >= max_changes:
            break
    return changes


def summarize_changes(changes: list[dict[str, Any]], *, limit: int = 4) -> str:
    """One-line pt-BR summary: 'altera a Resolução CMN 5304 (art. 5); revoga art. 7º'."""
    parts: list[str] = []
    for c in changes[:limit]:
        seg = c["verb"]
        tgt = c.get("targets") or []
        if tgt:
            seg += " " + " e ".join(t["label"] for t in tgt)
        arts = c.get("articles") or []
        if arts:
            seg += f" ({'; '.join(arts)})"
        parts.append(seg)
    more = len(changes) - limit
    if more > 0:
        parts.append(f"+{more} mudança(s)")
    return "; ".join(parts)
