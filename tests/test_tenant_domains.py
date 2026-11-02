"""#306 / ADR 016 SaaS: CloudFront multi-tenant template + per-tenant custom domains.

Source-level checks (a synth would write the template, edge-auth hash included, to disk). The
live distribution must stay untouched: every behavior is mirrored through ONE helper."""
import re
from pathlib import Path

from scripts.provision_tenant_domain import _zone_of, upsert_context

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "infra" / "app.py").read_text(encoding="utf-8")
IDENT = (ROOT / "infra" / "identity.py").read_text(encoding="utf-8")


def test_template_is_tenant_only_without_ipv6_or_logging():
    block = APP[APP.index('"OncaTenantCdn"'):APP.index("def _add_behavior")]
    assert 'add_property_override("DistributionConfig.ConnectionMode", "tenant-only")' in block
    assert 'add_property_deletion_override("DistributionConfig.IPV6Enabled")' in block
    tmpl = block[:block.index(")\n")]
    for banned in ("enable_logging", "price_class", "domain_names", "certificate="):
        assert banned not in tmpl, banned


def test_every_behavior_goes_through_the_mirroring_helper():
    direct = [m.start() for m in re.finditer(r"distribution\.add_behavior\(", APP)]
    helper = APP.index("def _add_behavior")
    assert len(direct) == 1 and helper < direct[0] < helper + 400          # only inside the helper
    assert APP.count("_add_behavior(") >= 11
    assert APP.index("def _add_behavior") < APP.index('"/pricing.html"')   # defined before first use


def test_tenants_only_with_an_issued_certificate():
    loop = APP[APP.index("tenant_domains = [d for d"):APP.index("def _add_behavior")]
    assert 'if not _td.get("cert_arn"):' in loop and "CfnDistributionTenant(" in loop


def test_lambda_origins_trust_the_template_too():
    fn = APP[APP.index("def _grant_cloudfront_invoke"):APP.index("review_fn = lambda_.Function(")]
    assert "source_arn=_cf_distribution_arn" in fn and "source_arn=_tenant_cdn_arn" in fn and "if _tenant_cdn_arn:" in fn


def test_only_the_live_v2_pool_gets_tenant_callbacks():
    assert 'build_identity_pool(self, "", distribution_domain=distribution.distribution_domain_name)' in APP
    v2 = APP[APP.index('_ident_v2 = build_identity_pool(self, "V2"'):]
    assert "extra_hosts=[d[\"domain\"] for d in tenant_domains]" in v2[:400]
    assert "extra_hosts: Any = ()" in IDENT                                  # legacy pool: unchanged
    assert IDENT.count('*[f"https://{h}{p}" for h in extra_hosts for p in ("/", "/exec")]') == 2
    assert "[d[\"domain\"] for d in tenant_domains]),  # #306" in APP          # session broker hosts


def test_context_upsert_is_one_entry_per_tenant():
    cdk = {"context": {"tenant_domains": [{"tenant": "a", "domain": "a.onssa.org", "cert_arn": "x"}]}}
    upsert_context(cdk, "b", "b.onssa.org", "arn:b")
    upsert_context(cdk, "a", "a2.onssa.org", "arn:a2")
    assert cdk["context"]["tenant_domains"] == [
        {"tenant": "a", "domain": "a2.onssa.org", "cert_arn": "arn:a2"},
        {"tenant": "b", "domain": "b.onssa.org", "cert_arn": "arn:b"}]
    assert _zone_of("tenant-test.onssa.org") == "onssa.org"


def test_custom_domain_survives_an_operator_reput():
    from src.dashboard import tenant_config as tc
    assert "custom_domain" in tc._PREF_FIELDS


def test_template_has_an_explicit_tls12_sni_certificate():
    """Live 2026-11-02: with no ViewerCertificate CloudFront defaulted to SNI + SSLv3 and refused."""
    block = APP[APP.index('"OncaTenantCdn"'):APP.index("def _add_behavior")]
    assert '"DistributionConfig.ViewerCertificate"' in block
    assert '"SslSupportMethod": "sni-only"' in block and '"MinimumProtocolVersion": "TLSv1.2_2021"' in block


def test_template_uses_js2_twins_and_leaves_the_live_functions_alone():
    """Live 2026-11-02: a template distribution only accepts cloudfront-js-2.0 functions."""
    helper = APP[APP.index("def _js2("):APP.index("def _add_behavior")]
    assert "runtime=cloudfront.FunctionRuntime.JS_2_0" in helper
    assert "fn.node.default_child.function_code" in helper
    tmpl = APP[APP.index('"OncaTenantCdn"'):APP.index("_tenant_cfn = ")]
    assert "_js2_assocs(_default_behavior.function_associations)" in tmpl
    add = APP[APP.index("def _add_behavior"):APP.index("def _add_behavior") + 700]
    assert '_js2_assocs(kw["function_associations"])' in add
    live = APP[APP.index('auth_fn = cloudfront.Function('):APP.index('auth_fn = cloudfront.Function(') + 300]
    assert "runtime=" not in live                                     # live function unchanged
