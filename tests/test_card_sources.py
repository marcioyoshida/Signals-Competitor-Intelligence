"""#290: the /exec drawer's card lookup keeps the NEWEST copy of a repeated id, and id-only BCB
citations render as labelled links to their public dataset pages (scripts/validate_card_sources.js
runs the real v2/app.js)."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_newest_card_and_dataset_sources():
    r = subprocess.run([shutil.which("node"), "scripts/validate_card_sources.js"], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_id_keyed_card_map_uses_the_newest_helper():
    for page in ("src/dashboard/site/v3/index.html", "src/dashboard/site/v2/admin/index.html"):
        text = (ROOT / page).read_text(encoding="utf-8")
        assert "[c.id] = c" not in text, f"{page}: last-wins id map (keeps the oldest card)"
        assert "U.newestById(DATA.feed)" in text, page
