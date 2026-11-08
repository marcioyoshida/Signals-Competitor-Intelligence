"""Mapa Competitivo (/exec CSO): zoom, hover readout, no redundant arrows (owner ask 2026-11-08)."""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "src" / "dashboard" / "site" / "v3" / "index.html").read_text(encoding="utf-8")
JS = PAGE[PAGE.index("const SC = { seq: 0, maps: {} };"):PAGE.index("// Threat timeline (v1 → CSO)")]


def test_the_redundant_delta_arrows_are_gone():
    # momentum IS recent − prior, so a prior→recent arrow only restated the x-axis sign.
    assert "sc-whisker" not in PAGE and "traço = variação" not in PAGE
    assert "momentum = ameaça média dos últimos 7 dias − 7 dias anteriores" in JS


def test_zoom_rerenders_in_data_space_with_controls():
    for k in ('data-sc-zoom="in"', 'data-sc-zoom="out"', 'data-sc-zoom="reset"'):
        assert k in JS
    assert "function scZoom(" in JS and "function scClamp(" in JS
    assert 'root.querySelector(".sc-plot").innerHTML = scSvg(' in JS        # not a CSS transform
    # a plain wheel keeps scrolling the page; only Ctrl/⌘ (and trackpad pinch) zooms
    assert "if (!(e.ctrlKey || e.metaKey)) return;" in JS and "{ passive: false }" in JS
    assert "clip-path=\"url(#${clip})\"" in JS                               # dots clipped to the frame


def test_hover_and_tap_show_the_measurements():
    assert '<div class="sc-tip" hidden></div>' in JS
    assert ".sc-tip[hidden] { display:none; }" in PAGE                      # a display:flex class beats [hidden]
    for field in ("Ameaça <strong>", "semana anterior", "Momentum <strong", "Ativos R$", "sem dado (IF.data/CVM)"):
        assert field in JS, field
    assert "function scCross(" in JS and "sc-bx" in JS and "sc-by" in JS    # axis read-outs
    assert "touch has no hover" in JS                                       # tap = same readout


def test_axes_carry_tick_values_and_all_four_quadrants_are_named():
    assert "function scTicks(" in JS and 'class="sc-tick"' in JS
    for q in ("ameaça alta · acelerando", "ameaça alta · recuando", "ameaça baixa · acelerando", "ameaça baixa · recuando"):
        assert q in JS, q
