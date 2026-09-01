"""v2 profile-driven dashboards — the industry registry + render engine (headless).

Runs the Node validator (scripts/validate_v2_industries.js) which loads
src/dashboard/site/v2/industries.js against a stubbed runtime and asserts: every FS
taxonomy industry resolves to a design, every lead maps to a real panel, and every
industry renders without throwing. Skips if Node isn't available.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_v2_industry_registry_renders_every_industry():
    r = subprocess.run(
        [shutil.which("node"), "scripts/validate_v2_industries.js"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    assert r.returncode == 0, "v2 industry validation failed"
    assert "VALIDATION OK" in r.stdout
