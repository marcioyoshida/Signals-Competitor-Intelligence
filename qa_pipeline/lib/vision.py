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


def parse_verdict(raw: str | None) -> dict[str, Any]:
    """Best-effort strict-JSON parse of a Bedrock vision response. Never raises — a
    malformed/empty response becomes an honest 'unavailable' result, not a crash, since this
    whole layer is advisory (ADR 027 #132: a fail/low-confidence verdict must never fail the
    pipeline; the same discipline applies to a verdict we couldn't even parse)."""
    if not raw:
        return {"available": False, "checklist": [], "raw": raw}
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        # Nova Pro occasionally appends stray trailing characters after a well-formed JSON
        # object (confirmed live: a trailing ";" after "...}]}") — json.loads rejects that
        # outright as "Extra data" even though the JSON itself parses fine. raw_decode()
        # stops at the first complete value and ignores whatever comes after it.
        parsed, _ = json.JSONDecoder().raw_decode(cleaned)
        checklist = parsed.get("checklist")
        if not isinstance(checklist, list):
            raise ValueError("no 'checklist' array in response")
        return {"available": True, "checklist": checklist, "raw": raw}
    except Exception as exc:  # noqa: BLE001 - advisory layer, never crash the pipeline
        return {"available": False, "checklist": [], "raw": raw, "parse_error": str(exc)}
