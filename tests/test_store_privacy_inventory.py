"""#315: the store privacy declarations must cover every dependency and permission the app has."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPDIR = ROOT / "mobile" / "apps" / "onca"
INV = (ROOT / "docs" / "store-privacy-inventory.md").read_text(encoding="utf-8")
SECTION7 = INV[INV.index("## 7. Dependencies"):]


def test_every_app_dependency_is_declared():
    deps = json.loads((APPDIR / "package.json").read_text())["dependencies"]
    listed = set(re.findall(r"^\| ([a-z0-9@/-]+)(?:, ([a-z0-9@/-]+))? \|", SECTION7, re.M))
    names = {n for pair in listed for n in pair if n}
    missing = sorted(set(deps) - names)
    assert not missing, f"add to docs/store-privacy-inventory.md section 7: {missing}"


def test_no_tracking_or_crash_sdk():
    lock = (APPDIR / "package-lock.json").read_text()
    hits = re.findall(r"firebase|sentry|bugsnag|crashlytics|expo-insights|adsupport|tracking-transparency|"
                      r"analytics|amplitude|mixpanel|@segment", lock, re.I)
    assert not hits, hits[:5]


def test_permissions_match_the_declaration():
    app = json.loads((APPDIR / "app.json").read_text())["expo"]
    plugins = {p if isinstance(p, str) else p[0] for p in app.get("plugins", [])}
    # each permission-bearing plugin must be one the inventory explains
    assert plugins <= {"expo-web-browser", "expo-secure-store", "expo-notifications",
                       "expo-local-authentication", "./plugins/recents-privacy.js"}, plugins
    info = app["ios"]["infoPlist"]
    assert info["ITSAppUsesNonExemptEncryption"] is False
    assert not any(k.startswith(("NSLocation", "NSCamera", "NSMicrophone", "NSContacts", "NSPhotoLibrary",
                                 "NSUserTracking")) for k in info)
    assert not app.get("android", {}).get("permissions"), "declare any explicit Android permission in section 7"


def test_privacy_policy_names_the_new_data_flows():
    html = (ROOT / "src" / "dashboard" / "site" / "docs" / "privacy.html").read_text(encoding="utf-8")
    assert "Saúde do app" in html and "Atualizações do app" in html
    assert "App health and updates" in html
