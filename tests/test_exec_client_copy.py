"""#238 — the /exec client page speaks Portuguese, not internal model/tier jargon."""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "src" / "dashboard" / "site" / "v3" / "index.html").read_text(encoding="utf-8")


def test_no_internal_jargon_in_client_copy():
    for leak in ("beliefs vetados", "requer beliefs", "official → identified", '"TDR requer baseline"',
                 '"ETS parcial"', "Registrar baseline"):
        assert leak not in PAGE, leak


def test_provenance_tiers_have_portuguese_labels():
    assert 'const TIER_PT = { official: "oficial", structured: "estruturada", registry: "cadastral", identified: "identificada" }' in PAGE
    assert "${TIER_PT[t] || t}</span>" in PAGE


def test_recent_signal_count_agrees_in_number():
    assert '${p.recent === 1 ? "sinal recente" : "sinais recentes"}' in PAGE
