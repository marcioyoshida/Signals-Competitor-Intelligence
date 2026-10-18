"""The Onça-branded managed-login page (auth.onssa.org): both Hosted-UI domains run managed login
(version 2), every Hosted-UI client of the live pool has the Onça style, the assets stay small
enough to inline in the template, and every URL that can land on it asks for Portuguese.

Since #277 the client sign-in path (/exec and every OncaCtx.login() page) is Onça's own form
(tests/test_signin_form.py); the managed page is what remains behind the OAuth/MCP facade, an
agent connecting, and as the interstitial of a Google/Apple redirect — so it keeps its brand."""
import json
import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import assertions

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra"))
import identity  # noqa: E402

APP_SRC = (ROOT / "infra" / "app.py").read_text(encoding="utf-8")
ASSETS = ROOT / "infra" / "assets" / "managed_login"


def _brandings():
    app = cdk.App(context={"google_client_id": "t.apps.googleusercontent.com", "apple_services_id": "o.e",
                           "apple_team_id": "T", "apple_key_id": "K"})
    stack = cdk.Stack(app, "T", env=cdk.Environment(account="111111111111", region="us-east-1"))
    v2 = identity.build_identity_pool(stack, "V2", distribution_domain="d1.cloudfront.net",
                                      email_mutable=True, deletion_protection=True)
    identity.build_managed_login_branding(stack, v2, "V2")
    res = assertions.Template.from_stack(stack).to_json()["Resources"]
    return res, {k: v for k, v in res.items() if v["Type"] == "AWS::Cognito::ManagedLoginBranding"}


def test_each_hosted_ui_client_gets_the_onca_style():
    res, brandings = _brandings()
    assert sorted(brandings) == ["OncaManagedLoginOAuthFacadeV2", "OncaManagedLoginWarroomV2"]
    clients = {k for k, v in res.items() if v["Type"] == "AWS::Cognito::UserPoolClient"}
    settings = json.loads((ASSETS / "settings.json").read_text())
    for b in brandings.values():
        p = b["Properties"]
        assert p["ClientId"]["Ref"] in clients
        assert p["UseCognitoProvidedValues"] is False
        assert p["Settings"] == settings
        assert sorted((a["Category"], a["ColorMode"]) for a in p["Assets"]) == sorted(
            (c, m) for c, m, _ in identity.MANAGED_LOGIN_ASSETS)


def test_style_uses_the_app_tokens_and_follows_the_device_theme():
    s = json.loads((ASSETS / "settings.json").read_text())
    css = (ROOT / "src" / "dashboard" / "site" / "v2" / "app.css").read_text()
    assert "--primary:      #157a5a" in css and "--primary:      #3ec39a" in css
    pb = s["components"]["primaryButton"]
    assert pb["lightMode"]["defaults"]["backgroundColor"] == "157a5aff"
    assert pb["darkMode"]["defaults"]["backgroundColor"] == "3ec39aff"
    assert s["categories"]["global"]["colorSchemeMode"] == "DYNAMIC"
    assert s["components"]["form"]["logo"]["enabled"] is True


def test_assets_stay_small_enough_to_inline():
    # base64 in the OncaPrototypeStack template (≈500 KB of a 1 MB limit), once per client
    total = sum((ASSETS / name).stat().st_size for _, _, name in identity.MANAGED_LOGIN_ASSETS)
    assert total < 90_000, total
    for _, _, name in identity.MANAGED_LOGIN_ASSETS:
        svg = (ASSETS / name).read_text()
        assert svg.startswith("<svg") and "<script" not in svg and "<text" not in svg, name
        assert "<use" not in svg and "<symbol" not in svg, name   # Cognito rejects <use> (deploy 10-16)


def test_both_domains_run_managed_login_after_the_style_exists():
    assert APP_SRC.count("managed_login_version=cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN") == 2
    assert '_login_branding = build_managed_login_branding(self, _ident_v2, "V2")' in APP_SRC
    assert "user_pool_domain.node.add_dependency(*_login_branding)" in APP_SRC
    assert "auth_custom_domain.node.add_dependency(*_login_branding)" in APP_SRC


def test_sign_in_urls_ask_for_portuguese():
    site = ROOT / "src" / "dashboard" / "site"
    # v1 index.html still builds the managed-login URL; context.js (every v2 page, admin included
    # since #277) builds only the provider URL (oauth2/authorize?identity_provider=…), keeping lang
    for f in ("index.html", "v2/context.js"):
        assert 'lang: "pt-BR"' in (site / f).read_text(encoding="utf-8"), f
    ctx = (site / "v2" / "context.js").read_text(encoding="utf-8")
    assert "identity_provider: useIdp" in ctx and "${AUTH.domain}/login" not in ctx
    assert '"lang": "pt-BR"' in (ROOT / "src" / "dashboard" / "oauth" / "handler.py").read_text()
