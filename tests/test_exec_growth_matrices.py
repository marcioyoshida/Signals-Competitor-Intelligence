"""#377: CPO Ansoff/BCG band renders as two linked 2×2 grids keyed by the REAL dimensions."""
import re
from pathlib import Path

from src.synth import ansoff, bcg

HTML = (Path(__file__).resolve().parents[1] / "src/dashboard/site/v3/index.html").read_text(encoding="utf-8")
CSS = (Path(__file__).resolve().parents[1] / "src/dashboard/site/v2/app.css").read_text(encoding="utf-8")


def _cells(fw: str) -> list[str]:
    block = re.search(fw + r': \{ name:.*?cells: \[(.*?)\] \}', HTML, re.S).group(1)
    return re.findall(r'\["([a-z_]+)", "', block)


def test_quadrant_keys_match_backend_dimensions():
    assert sorted(_cells("ansoff")) == sorted(ansoff.DIMENSIONS)
    assert sorted(_cells("bcg")) == sorted(bcg.DIMENSIONS)


def test_cpo_band_uses_matrices_and_keeps_lead():
    assert 'band("Crescimento (Ansoff) & portfólio (BCG)", growthSubtitle(p), growthMatrices(p), { lead: true })' in HTML
    assert 'if (OFFICER === "cpo") wireQuads(el);' in HTML
    assert "function rowsFramework(" in HTML and "rowsFramework(p.porter)" in HTML   # CSO/CCO unchanged


def test_honesty_and_accessibility_hooks():
    assert "não é posição medida de share × crescimento" in HTML          # no fabricated BCG positions
    assert '"sem tese"' in HTML and "radar: ${U.esc(r)}" in HTML
    assert 'aria-pressed="false"' in HTML and 'aria-expanded="false"' in HTML
    for glyph in ("★", "◆", "✕"):
        assert glyph in HTML                                              # color is never the only signal
    assert ".qcell.is-all .qchips { max-height:" in CSS                   # dense cells scroll, never stretch the grid
