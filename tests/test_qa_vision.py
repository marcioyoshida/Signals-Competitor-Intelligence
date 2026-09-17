"""qa_pipeline/lib/vision.py — pure prompt/parsing logic (no network, no Playwright)."""
from __future__ import annotations

from qa_pipeline.lib import vision


def test_build_prompt_numbers_every_checklist_item():
    prompt = vision.build_prompt("mapa_competitivo")
    assert "1. Are any two text labels overlapping" in prompt
    assert "3. Does any text fail to have clearly sufficient contrast" in prompt


def test_build_prompt_unknown_panel_raises():
    import pytest

    with pytest.raises(KeyError):
        vision.build_prompt("not-a-real-panel")


def test_parse_verdict_none_is_unavailable_not_a_crash():
    out = vision.parse_verdict(None)
    assert out == {"available": False, "checklist": [], "raw": None}


def test_parse_verdict_strict_json():
    raw = '{"checklist": [{"item": "x", "pass": true, "confidence": 0.9, "note": ""}]}'
    out = vision.parse_verdict(raw)
    assert out["available"] is True
    assert out["checklist"][0]["pass"] is True


def test_parse_verdict_strips_markdown_fences():
    raw = '```json\n{"checklist": [{"item": "x", "pass": false, "confidence": 0.5, "note": "n"}]}\n```'
    out = vision.parse_verdict(raw)
    assert out["available"] is True
    assert out["checklist"][0]["pass"] is False


def test_parse_verdict_tolerates_trailing_garbage_after_the_json():
    """Confirmed live (#132): Nova Pro occasionally appends a stray trailing character
    (e.g. ";") after an otherwise well-formed JSON object — must not be treated as
    malformed."""
    raw = '{"checklist": [{"item": "x", "pass": true, "confidence": 0.9, "note": ""}]};'
    out = vision.parse_verdict(raw)
    assert out["available"] is True
    assert out["checklist"][0]["pass"] is True


def test_parse_verdict_malformed_json_is_unavailable_not_a_crash():
    out = vision.parse_verdict("not json at all")
    assert out["available"] is False
    assert "parse_error" in out


def test_parse_verdict_missing_checklist_key_is_unavailable():
    out = vision.parse_verdict('{"something_else": []}')
    assert out["available"] is False
