"""/v2/admin operator copy — Portuguese labels, not raw review codes or "(s)" plurals."""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "src" / "dashboard" / "site" / "v2" / "admin" / "index.html").read_text(encoding="utf-8")


def test_no_slash_plurals_or_english_lede():
    for leak in ("monitorada(s)", "sessão(ões)", "excluída(s)", "o moat", "Fila de vetting",
                 "feed.regulatory_coverage ausente", "entity_attrs sem radar"):
        assert leak not in PAGE, leak


def test_review_codes_and_swot_kinds_are_translated():
    assert "REASON_PT[r.reason] || r.reason" in PAGE and "MATCH_PT[r.confidence] || r.confidence" in PAGE
    assert 'prominence_gate: "filtro de proeminência"' in PAGE and 'structured: "dado estruturado"' in PAGE
    assert "SWOT_TAG_PT[p.kind]" in PAGE and 'p.kind === "new" ? "Nova tese"' in PAGE
