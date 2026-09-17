"""ADR 027 #132 — Bedrock-vision checklist prompts + a strict-JSON verdict parser, shared by
checks/vision.py. Model: amazon.nova-pro-v1:0 (same family already in production for
framework synthesis — src/synth/bedrock_llm.py's FRAMEWORK_MODEL — no new model-access
request needed; Nova Pro is multimodal, Nova Micro/Lite's cheaper tiers are not)."""
from __future__ import annotations

import json
import re
from typing import Any

VISION_MODEL = "amazon.nova-pro-v1:0"

# One small checklist per curated panel type (ADR 027: "curated panel list only... not every
# screenshot in the full browser/viewport matrix" — cost containment). Deliberately about
# VISUAL/layout defects only, never the underlying business data — this is not a fact-check.
CHECKLISTS: dict[str, list[str]] = {
    "mapa_competitivo": [
        "Are any two text labels overlapping or touching each other, making either hard to read?",
        "Is any arrow, line, or whisker fully hidden INSIDE a circle/bubble mark rather than "
        "visibly extending out from it?",
        "Does any text fail to have clearly sufficient contrast against its background?",
    ],
    "quadrant": [
        "Are any two text labels or data-point markers overlapping, making either hard to read?",
        "Does any axis label, legend, or title get clipped or cut off at the edge of the chart?",
        "Does any text fail to have clearly sufficient contrast against its background?",
    ],
    "quotes": [
        "Does every row in the table render as clean, aligned text — no visibly broken, "
        "overlapping, or garbled rows?",
        "Is there any broken-image icon, placeholder glyph, or obviously corrupted rendering?",
        "Does any text fail to have clearly sufficient contrast against its background?",
    ],
}

PROMPT_TEMPLATE = """You are a meticulous UI QA reviewer looking at ONE screenshot of a real
data dashboard panel. Answer ONLY the checklist below about what you can SEE in the image —
do not comment on the underlying data/business content, only visual/layout defects.

Checklist:
{items}

Respond with STRICT JSON only, no markdown fences, no commentary outside the JSON, in this
exact shape:
{{"checklist": [{{"item": "<verbatim checklist item text>", "pass": true|false, \
"confidence": <0.0-1.0>, "note": "<one short sentence, empty if pass>"}}, ...]}}

"pass": true means NO defect found for that item (the check passes). "pass": false means you
DID find the described defect."""


def build_prompt(panel_key: str) -> str:
    items = CHECKLISTS[panel_key]
    numbered = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(items))
    return PROMPT_TEMPLATE.format(items=numbered)


def _close_truncated_json(s: str) -> str | None:
    """If `s` looks like a JSON value cut off mid-stream (an unclosed string and/or one or
    more unclosed `{`/`[`), return it with the missing closers appended so it parses.
    Returns None if `s` isn't a simple truncation (e.g. more closes than opens — genuinely
    malformed, not just cut short). String-literal-aware (tracks `"..."` and `\\`-escapes)
    so a brace mentioned inside a "note" string is never mistaken for real structure.

    Confirmed live (#132): even a raised max_tokens doesn't guarantee headroom — Nova Pro's
    response length varies with how much it has to say, and a response truncated by as
    little as ONE missing closing brace still fails json.loads/raw_decode outright. Retrying
    with more tokens is not a fix (there is no ceiling that's provably always enough);
    repairing the truncation is."""
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in s:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                return None
            stack.pop()
    if not stack and not in_string:
        return None
    closer = '"' if in_string else ""
    closer += "".join("}" if ch == "{" else "]" for ch in reversed(stack))
    return s + closer


def parse_verdict(raw: str | None) -> dict[str, Any]:
    """Best-effort strict-JSON parse of a Bedrock vision response. Never raises — a
    malformed/empty response becomes an honest 'unavailable' result, not a crash, since this
    whole layer is advisory (ADR 027 #132: a fail/low-confidence verdict must never fail the
    pipeline; the same discipline applies to a verdict we couldn't even parse)."""
    if not raw:
        return {"available": False, "checklist": [], "raw": raw}
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    candidates = [cleaned]
    repaired = _close_truncated_json(cleaned)
    if repaired is not None:
        candidates.append(repaired)
    last_exc: Exception | None = None
    for candidate in candidates:
        try:
            # Nova Pro occasionally appends stray trailing characters after a well-formed
            # JSON object (confirmed live: a trailing ";" after "...}]}") — json.loads
            # rejects that outright as "Extra data" even though the JSON itself parses
            # fine. raw_decode() stops at the first complete value and ignores the rest.
            parsed, _ = json.JSONDecoder().raw_decode(candidate)
            checklist = parsed.get("checklist")
            if not isinstance(checklist, list):
                raise ValueError("no 'checklist' array in response")
            return {"available": True, "checklist": checklist, "raw": raw}
        except Exception as exc:  # noqa: BLE001 - advisory layer, never crash the pipeline
            last_exc = exc
    return {"available": False, "checklist": [], "raw": raw, "parse_error": str(last_exc)}
