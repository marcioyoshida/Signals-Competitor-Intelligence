"""#161: the only paths the CloudFront basic-auth function lets through without the shared
password are /exec's shell and static assets. Widening this list must be a deliberate edit here."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra"))

import edge_policy  # noqa: E402


def test_exec_exemption_list_is_pinned():
    assert set(edge_policy.EXEC_PUBLIC_URIS) == {
        "/v3/index.html", "/v2/app.css", "/v2/app.js", "/v2/context.js", "/exec-sw.js",
        "/v3/manifest.webmanifest", "/v3/icons/icon-192.png", "/v3/icons/icon-512.png",
        "/v3/icons/icon-maskable-512.png", "/v3/icons/apple-touch-icon.png", "/v3/og.png"}


def test_no_data_or_api_or_other_surface_is_exempt():
    for u in edge_policy.EXEC_PUBLIC_URIS:
        assert not u.startswith("/api/") and "feed" not in u and not u.endswith(".json")
        assert not u.startswith("/v2/") or u in ("/v2/app.css", "/v2/app.js", "/v2/context.js")


def test_js_is_exact_match_not_prefix():
    js = edge_policy.exec_exemption_js()
    assert "execPublic[r.uri] === 1" in js and "indexOf" not in js and "startsWith" not in js
