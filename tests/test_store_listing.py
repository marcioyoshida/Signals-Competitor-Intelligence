"""#314: the store listing fits each store's field limits and keeps the B2B / no-purchase claims."""
import json
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "mobile" / "apps" / "onca" / "store"
L = json.loads((STORE / "listing.json").read_text(encoding="utf-8"))
LIMITS = {"name": 30, "subtitle": 30, "promotional_text": 170, "play_short_description": 80,
          "keywords": 100, "description": 4000, "whats_new": 4000}


def test_field_limits():
    for loc, f in L["locales"].items():
        for k, n in LIMITS.items():
            assert f[k] and len(f[k]) <= n, (loc, k, len(f[k]))


def test_b2b_and_no_purchase_claims():
    pt, en = L["locales"]["pt-BR"]["description"], L["locales"]["en-US"]["description"]
    assert "conta Onça" in pt and "não vende nada" in pt
    assert "existing Onça account" in en and "sells nothing" in en
    for f in L["locales"].values():
        assert "R$" not in f["description"] and "preço" not in f["description"].lower()


def test_urls_point_at_onssa():
    for u in L["urls"].values():
        assert u.startswith("https://onssa.org/")


def test_screenshot_sizes():
    want = {"iphone-6.9": (1320, 2868), "iphone-6.5": (1284, 2778), "android-phone": (1080, 2160)}
    for dev, size in want.items():
        files = sorted((STORE / "screenshots" / dev).glob("*.png"))
        assert len(files) >= 3, dev
        for f in files:
            w, h = struct.unpack(">II", f.read_bytes()[16:24])
            assert (w, h) == size, (f.name, w, h)
            assert max(w, h) <= 2 * min(w, h) or dev.startswith("iphone")     # Play: at most 2:1


def test_release_workflow_update_names_its_environment():
    """#311 dry run 2026-11-03: `eas update --non-interactive` refuses to run without --environment."""
    wf = (ROOT / ".github" / "workflows" / "mobile-release.yml").read_text()
    upd = wf[wf.index("- name: OTA update"):wf.index("- name: Quota guard")]
    assert '--environment "${{ inputs.profile }}"' in upd and "--non-interactive" in upd
    assert "on:\n  workflow_dispatch:" in wf and "push:" not in wf.split("jobs:")[0]   # manual only
