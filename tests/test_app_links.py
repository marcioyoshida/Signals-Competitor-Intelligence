"""#289: the AASA claims ONLY shared card links for the store app."""
import json

from src.dashboard import app_links
from src.dashboard.oauth import handler


def _matches(component, path, query):
    """Apple's matching for one component: `?` = one char (path and query values)."""
    import fnmatch
    if not fnmatch.fnmatchcase(path, component["/"]) or "*" in component["/"]:
        return False
    for k, pat in (component.get("?") or {}).items():
        v = query.get(k)
        if v is None or len(v) != len(pat):
            return False
    return True


def test_served_by_the_oauth_lambda_as_json_without_redirect():
    r = handler.lambda_handler({"rawPath": "/.well-known/apple-app-site-association", "headers": {}})
    assert r["statusCode"] == 200
    assert r["headers"]["Content-Type"] == "application/json"
    d = json.loads(r["body"])
    assert d["applinks"]["details"][0]["appIDs"] == ["AWSP6DBQ68.org.onssa.onca"]


def test_only_card_links_open_the_app():
    comps = app_links.apple_app_site_association()["applinks"]["details"][0]["components"]
    hit = lambda path, q: any(_matches(c, path, q) for c in comps)
    assert hit("/exec", {"c": "0123456789abcdef"})
    assert hit("/c/0123456789abcdef", {})
    assert not hit("/c/0123456789abcdef/x", {}) and not hit("/c/", {}) and not hit("/c/short", {})
    # the web sign-in return and every other page stay in the browser
    assert not hit("/exec", {"code": "abc", "state": "x"})
    assert not hit("/exec", {})
    assert not hit("/v2/", {"c": "0123456789abcdef"})
    assert not hit("/exec/", {"c": "0123456789abcdef"})
