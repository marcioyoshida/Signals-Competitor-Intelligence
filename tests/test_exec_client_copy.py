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


def test_hoje_and_painel_rows_share_one_binding_that_resolves_targets():
    # #239: Hoje only opened rows whose id was a card id (sector_event:/enf:/entity rows were dead)
    assert "if (BYID[id]) inspect(id)" not in PAGE
    assert PAGE.count("bindRows(el);") == 2
    assert 'id.indexOf("sector_event:") === 0 || id.indexOf("enf:") === 0' in PAGE
    assert ".exrow[tabindex] { cursor:pointer; }" in PAGE


def test_drawer_and_rows_are_keyboard_accessible():
    # #240: rows activatable by Enter/Space, Esc closes, focus moves in and comes back
    assert 'row.setAttribute("tabindex", "0")' in PAGE and 'e.key !== "Enter" && e.key !== " "' in PAGE
    assert 'if (e.key === "Escape") { e.preventDefault(); closeDrawer(); return; }' in PAGE
    assert 'document.getElementById("drClose").focus(' in PAGE and "DR_RETURN.focus(" in PAGE
    assert 'aria-modal="true"' in PAGE


def test_enforcement_and_distress_labels_are_portuguese():
    # #241
    for leak in ('"Enforcement — "', 'tile("Enforcement"', '"Enforcement & sanções"', "Enforcement · distress",
                 'tile("Distress', "(distress/reputação"):
        assert leak not in PAGE, leak
    assert '"Medida de supervisão — " + (r.kind_label' in PAGE
