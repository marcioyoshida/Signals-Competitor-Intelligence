"""v2 position-map (ameaça × expansão) data-sufficiency gate — headless.

Runs scripts/validate_quadrant_gate.js, which exercises OncaCtx.quadrantViable:
a scatter is shown only when BOTH axes disperse (>= 3 competitors, >= 2 with
expansion, threat span >= 0.12) — the regression-validity precondition, not an
R²/correlation test. Degenerate industries (thin early-corpus data) withdraw the
hero rather than render a misleading 1-D strip. Skips if Node isn't available.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_quadrant_gate_rejects_degenerate_scatters():
    r = subprocess.run(
        [shutil.which("node"), "scripts/validate_quadrant_gate.js"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    assert r.returncode == 0, "quadrant gate validation failed"
    assert "QUADRANT GATE OK" in r.stdout
