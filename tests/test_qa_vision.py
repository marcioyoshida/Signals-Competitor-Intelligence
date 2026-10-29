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


def test_parse_verdict_repairs_truncation_missing_one_closing_brace():
    """Confirmed live 2026-09-17: a response truncated by exactly ONE missing closing
    brace (the literal raw output from a real run, even at max_tokens=900) — a higher
    token budget is not a real fix since there's no provable ceiling; repairing the
    truncation is."""
    raw = (
        '{"checklist": [{"item": "Are any two text labels or data-point markers '
        'overlapping, making either hard to read?", "pass": true, "confidence": 0.95, '
        '"note": ""}, {"item": "Does any axis label, legend, or title get clipped or '
        'cut off at the edge of the chart?", "pass": false, "confidence": 0.9, "note": '
        '"The legend at the bottom is partially cut off."}, {"item": "Does any text '
        'fail to have clearly sufficient contrast against its background?", "pass": '
        'true, "confidence": 0.9, "note": "All text has sufficient contrast against '
        'its background."}]'
    )
    out = vision.parse_verdict(raw)
    assert out["available"] is True
    assert len(out["checklist"]) == 3
    assert out["checklist"][1]["pass"] is False


def test_parse_verdict_repairs_truncation_mid_string_value():
    """A cut-off INSIDE a string value (not just after it) — the unclosed string AND the
    unclosed structure around it both need closing."""
    raw = '{"checklist": [{"item": "x", "pass": false, "confidence": 0.8, "note": "cut off mid'
    out = vision.parse_verdict(raw)
    assert out["available"] is True
    assert out["checklist"][0]["pass"] is False
    assert out["checklist"][0]["note"] == "cut off mid"


def test_parse_verdict_does_not_repair_genuinely_malformed_json():
    """More closes than opens — not a truncation, don't pretend otherwise."""
    out = vision.parse_verdict('{"checklist": []}}')
    assert out["available"] is True  # raw_decode stops at the first complete value
    assert out["checklist"] == []


def test_parse_verdict_malformed_json_is_unavailable_not_a_crash():
    out = vision.parse_verdict("not json at all")
    assert out["available"] is False
    assert "parse_error" in out


def test_parse_verdict_missing_checklist_key_is_unavailable():
    out = vision.parse_verdict('{"something_else": []}')
    assert out["available"] is False


# --- checks/vision.py: which persona captures, and do the /exec bands still exist ----------
# 10-23..10-28 (and earlier): both /exec captures timed out every night because the branch
# logged in as the `entry` persona, whose /exec is the Entry board with no officer suite.

import re
from pathlib import Path

V3_HTML = Path(__file__).resolve().parents[1] / "src/dashboard/site/v3/index.html"


def _cso_board_source() -> str:
    """The `if (OFFICER === "cso") { ... }` branch of v3 render() — where both /exec bands live."""
    src = V3_HTML.read_text(encoding="utf-8")
    start = src.index('if (OFFICER === "cso") {')
    return src[start:src.index('} else if (OFFICER === "cro")', start)]


def test_every_exec_band_title_is_rendered_by_the_cso_board():
    from qa_pipeline.checks import vision as vision_check

    cso = _cso_board_source()
    exec_bands = [v for _, path, kind, v in vision_check.PANELS if path == "/exec" and kind == "band_text"]
    assert exec_bands
    for title in exec_bands:
        assert re.search(r'band\("' + re.escape(title) + '"', cso), title


def test_app_quadrant_id_exists():
    from qa_pipeline.checks import vision as vision_check

    html = (V3_HTML.parents[1] / "v2/app/index.html").read_text(encoding="utf-8")
    for _, path, kind, value in vision_check.PANELS:
        if kind == "id":
            assert f'id="{value}"' in html, value


def test_entry_tenant_never_reaches_the_cso_board():
    """Pins the reason for the persona swap: render() returns into mountEntry() before any
    officer board is built. If Entry ever gains the officer suite, this test (and the swap)
    can go."""
    src = V3_HTML.read_text(encoding="utf-8")
    render = src[src.index("function render() {"):src.index('if (OFFICER === "cso") {')]
    assert "Ctx.isEntryTier(DATA)) { mountEntry(); return; }" in render


def _run_with_fakes(monkeypatch, event):
    """Drive checks/vision.run() with Playwright, Cognito, S3 and Bedrock all faked;
    returns (result, {"user": username logged in as})."""
    from contextlib import contextmanager
    from unittest import mock

    from qa_pipeline.checks import vision as vision_check
    from src.synth import bedrock_llm

    logged_in = {}
    monkeypatch.setattr(vision_check.config, "qa_credentials", lambda: {"personas": {
        "entry": {"username": "e", "password": "p"}, "admin": {"username": "a", "password": "p"}}})
    monkeypatch.setattr(vision_check.config, "basic_auth", lambda: ("u", "p"))
    monkeypatch.setattr(vision_check.auth, "login_with_password",
                        lambda page, url, username, password: logged_in.setdefault("user", username))
    monkeypatch.setattr(vision_check, "upload_artifact", lambda *a, **k: None)
    monkeypatch.setattr(vision_check, "_capture", lambda page, kind, value: b"png")
    monkeypatch.setattr(bedrock_llm, "converse", lambda *a, **k: '{"checklist": []}')

    @contextmanager
    def fake_playwright():
        yield mock.MagicMock()

    monkeypatch.setattr(vision_check, "sync_playwright", fake_playwright)
    return vision_check.run(event), logged_in


def test_vision_swaps_the_entry_persona_for_the_officer_suite_one(monkeypatch):
    result, logged_in = _run_with_fakes(monkeypatch, {"persona": "entry", "run_id": "t"})
    assert logged_in["user"] == "a"
    assert [f["panel"] for f in result["findings"]] == ["mapa_competitivo", "quotes", "quadrant"]
    assert all(f["captured"] for f in result["findings"])


def test_vision_honors_an_explicit_non_entry_persona(monkeypatch):
    _, logged_in = _run_with_fakes(monkeypatch, {"persona": "admin", "run_id": "t"})
    assert logged_in["user"] == "a"
