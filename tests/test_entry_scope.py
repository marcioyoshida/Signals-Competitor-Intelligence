"""#237 — Entry is sold per module: no client surface reads the all-verticals static slice."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "src" / "dashboard" / "site"


def test_newentry_reads_the_server_scoped_feed():
    page = (SITE / "v2" / "newentry" / "index.html").read_text(encoding="utf-8")
    assert "C.bootSaaS(" in page and "feed.entry.json\" })" not in page
    assert "entry: true" not in page and 'src="/v2/industries.js"' in page
    # unlicensed Entry verticals: names + upgrade link only, never their data
    assert 'C.upgradeHref("entry", m, D.upgrade)' in page
    assert "C.ENTRY_INDUSTRIES.filter((m) => lic.indexOf(m) === -1)" in page


def test_old_entry_portal_redirects_to_exec():
    page = (SITE / "entry" / "index.html").read_text(encoding="utf-8")
    assert 'location.replace("/exec"' in page
    assert "fetch(" not in page


def test_static_entry_slice_is_operator_only_at_the_edge():
    app = (ROOT / "infra" / "app.py").read_text(encoding="utf-8")
    assert """if (key === "/feed.json" || key === "/feed.entry.json") {""" in app
