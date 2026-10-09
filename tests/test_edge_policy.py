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
        "/v3/icons/icon-maskable-512.png", "/v3/icons/apple-touch-icon.png", "/v3/og.png",
        "/404.html", "/403.html"}  # #221: static branded error pages, no data


def test_no_data_or_api_or_other_surface_is_exempt():
    for u in edge_policy.EXEC_PUBLIC_URIS:
        assert not u.startswith("/api/") and "feed" not in u and not u.endswith(".json")
        assert not u.startswith("/v2/") or u in ("/v2/app.css", "/v2/app.js", "/v2/context.js")


def test_js_is_exact_match_not_prefix():
    js = edge_policy.exec_exemption_js()
    assert "execPublic[r.uri] === 1" in js and "indexOf" not in js and "startsWith" not in js


# --- #216 directory redirects + branded 404 --------------------------------------------------
def _run_fn(uri, qs=None):
    """Execute the site_paths_js snippet in node against a fake request; return redir."""
    import json, subprocess
    from edge_policy import site_paths_js
    site = Path(__file__).resolve().parents[1] / "src" / "dashboard" / "site"
    js = ("var r = " + json.dumps({"uri": uri, "querystring": qs or {}}) + ";\n"
          'var routes = { "/app": "app", "/admin": "admin" };\n'
          'var key = r.uri; if (key.length > 1 && key.charAt(key.length - 1) === "/") key = key.substring(0, key.length - 1);\n'
          + site_paths_js(site) + "console.log(JSON.stringify(redir));\n")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_site_paths_redirects_dirs_and_unknown_paths():
    import shutil
    if not shutil.which("node"):
        import pytest
        pytest.skip("node not installed")
    assert _run_fn("/v2/app") == "/v2/app/"
    assert _run_fn("/v2/admin", {"opkey": {"value": "k"}}) == "/v2/admin/?opkey=k"
    assert _run_fn("/entry") == "/entry/"
    assert _run_fn("/nonexistent-page-xyz") == "/404.html"
    assert _run_fn("/v2/") == "/404.html"
    for ok in ("/", "/v2/app/", "/index.html", "/feed.json", "/app", "/admin", "/exec",
               "/api/act", "/api/run/", "/v3/index.html", "/404.html"):
        assert _run_fn(ok) is None, ok


def test_unknown_files_go_to_404_and_favicon_to_the_icon():
    """#220: a missing path WITH an extension got S3's raw AccessDenied XML."""
    import shutil
    if not shutil.which("node"):
        import pytest
        pytest.skip("node not installed")
    assert _run_fn("/favicon.ico") == edge_policy.FAVICON_TARGET
    for missing in ("/robots.txt", "/x.js", "/missing.html", "/v2/nope.css"):
        assert _run_fn(missing) == "/404.html", missing
    for ok in ("/v2/app.js", "/v2/app.css", "/v3/manifest.webmanifest", "/exec-sw.js",
               "/feed.json", "/feed.entry.json", "/anything.json", "/403.html", "/404.html",
               edge_policy.FAVICON_TARGET):
        assert _run_fn(ok) is None, ok


def _run_public(uri):
    """Execute the public function's route rewrite + public_paths_js snippet in node."""
    import json, subprocess
    site = Path(__file__).resolve().parents[1] / "src" / "dashboard" / "site"
    js = ("var r = " + json.dumps({"uri": uri}) + ";\n"
          'var routes = { "/sample": "/sample/index.html", "/sample/": "/sample/index.html",'
          ' "/docs": "/docs/index.html", "/docs/": "/docs/index.html" };\n'
          "function f() { if (routes[r.uri]) { r.uri = routes[r.uri]; }\n"
          + edge_policy.public_paths_js(site) + "  return r; }\n"
          "var o = f(); console.log(JSON.stringify(o.statusCode ? [o.statusCode, o.headers.location.value] : o.uri));\n")
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)


def test_public_docs_sample_unknown_paths_go_to_404():
    """#221: /docs/* and /sample/* unknown paths got S3's raw AccessDenied XML."""
    import shutil
    if not shutil.which("node"):
        import pytest
        pytest.skip("node not installed")
    assert _run_public("/docs/privacy") == [301, "/docs/privacy.html"]
    for missing in ("/docs/nope.html", "/docs/x.js", "/sample/x", "/sample/x.html", "/docs/qa/"):
        assert _run_public(missing) == [302, "/404.html"], missing
    for ok in ("/docs", "/docs/", "/sample", "/sample/", "/docs/privacy.html", "/docs/terms.html",
               "/docs/qa/oauth-test-client.json", "/sample/index.html", "/pricing.html"):
        assert isinstance(_run_public(ok), str), ok
