"""ADR 009 §2 — versioned-document section diff.

Phase B (`reg_documents.py`) stores each version of a tracked instrument's full text. When a
new version lands, this computes the DELTA against the prior version: segment both by
structural unit (preamble / Art. N / Anexo), align by unit key, and mark each
added / removed / modified (with a compact text diff for modified). Deterministic and
cited by construction (each change points to its article + the two version hashes) — the
grounded diff the LLM change-record (§3) will describe and rate. No LLM, no network here.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any

# Article / annex headers that open a structural unit. The DOU body is newline-joined
# paragraphs, so a unit header sits at the start of a line.
_ART_RX = re.compile(r"(?im)^\s*(art(?:igo)?\.?\s*\d+)")
_ANEXO_RX = re.compile(r"(?im)^\s*(anexo\s+[ivxlcdm\d]+|anexo)\b")


def _norm(text: str) -> str:
    """Whitespace/case-insensitive normalization for equality (not for display)."""
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip().lower()


def _unit_key(header: str) -> str:
    h = _norm(header)
    m = re.search(r"art(?:igo)?\.?\s*(\d+)", h)
    if m:
        return f"art-{int(m.group(1))}"
    m = re.search(r"anexo\s+([ivxlcdm\d]+)", h)
    if m:
        return f"anexo-{m.group(1)}"
    return "anexo"


def segment(text: str) -> list[dict[str, str]]:
    """Ordered structural units [{key, label, text}] — preamble, then each Art./Anexo."""
    text = str(text or "")
    # boundary positions: every article + annex header
    marks: list[tuple[int, str]] = []
    for rx in (_ART_RX, _ANEXO_RX):
        for m in rx.finditer(text):
            marks.append((m.start(), m.group(1).strip()))
    marks.sort()
    units: list[dict[str, str]] = []
    if not marks:
        body = text.strip()
        return [{"key": "preamble", "label": "Preâmbulo", "text": body}] if body else []
    pre = text[: marks[0][0]].strip()
    if pre:
        units.append({"key": "preamble", "label": "Preâmbulo", "text": pre})
    for i, (pos, header) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        body = text[pos:end].strip()
        if body:
            units.append({"key": _unit_key(header), "label": header.rstrip(".").strip(),
                          "text": body})
    return units


def _inline_diff(old: str, new: str, *, max_lines: int = 12) -> list[str]:
    """A compact unified diff (± lines) between two units, for the modified record."""
    o = re.split(r"(?<=[.;:])\s+", str(old or "").strip())
    n = re.split(r"(?<=[.;:])\s+", str(new or "").strip())
    out: list[str] = []
    for line in difflib.unified_diff(o, n, lineterm="", n=0):
        if line.startswith(("+++", "---", "@@")):
            continue
        if line and line[0] in "+-":
            out.append(line.strip())
        if len(out) >= max_lines:
            out.append("…")
            break
    return out


def diff_versions(old_text: str, new_text: str) -> dict[str, Any]:
    """Section-level delta old→new: per-unit status + a compact diff for modified units."""
    old_units = {u["key"]: u for u in segment(old_text)}
    new_units = {u["key"]: u for u in segment(new_text)}
    order: list[str] = list(new_units.keys()) + [k for k in old_units if k not in new_units]

    sections: list[dict[str, Any]] = []
    added, removed, modified = [], [], []
    for key in order:
        o, n = old_units.get(key), new_units.get(key)
        label = (n or o)["label"]
        if o and n:
            if _norm(o["text"]) == _norm(n["text"]):
                sections.append({"key": key, "label": label, "status": "unchanged"})
            else:
                modified.append(key)
                sections.append({"key": key, "label": label, "status": "modified",
                                 "diff": _inline_diff(o["text"], n["text"])})
        elif n:
            added.append(key)
            sections.append({"key": key, "label": label, "status": "added",
                             "text": n["text"][:400]})
        else:
            removed.append(key)
            sections.append({"key": key, "label": label, "status": "removed",
                             "text": o["text"][:400]})
    return {
        "sections": sections,
        "added": added, "removed": removed, "modified": modified,
        "summary": {"added": len(added), "removed": len(removed),
                    "modified": len(modified), "units_new": len(new_units)},
    }


def summarize_diff(diff: dict[str, Any], *, limit: int = 6) -> str:
    """One-line pt-BR: 'modifica Art. 1, Art. 5; inclui Art. 9; revoga Art. 7'."""
    s = diff.get("summary", {})
    lbl = {u["key"]: u["label"] for u in diff.get("sections", [])}
    parts: list[str] = []
    if diff.get("modified"):
        parts.append("modifica " + ", ".join(lbl.get(k, k) for k in diff["modified"][:limit]))
    if diff.get("added"):
        parts.append("inclui " + ", ".join(lbl.get(k, k) for k in diff["added"][:limit]))
    if diff.get("removed"):
        parts.append("revoga " + ", ".join(lbl.get(k, k) for k in diff["removed"][:limit]))
    return "; ".join(parts) or "sem alteração estrutural"
