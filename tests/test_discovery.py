"""#180/#183/#186: the committed discovery files match the generator, describe only what exists,
and never leak tenant data, entity lists or pricing internals."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.dashboard import discovery  # noqa: E402

SITE = ROOT / "src" / "dashboard" / "site"


def test_committed_files_match_the_generator():
    for rel, content in discovery.files().items():
        assert (SITE / rel).read_text(encoding="utf-8") == content, f"{rel} drifted — run scripts/build_discovery.py"
    assert json.loads((ROOT / "server.json").read_text()) == discovery.render_server_json()


def test_no_pricing_tiers_tenant_or_entity_data_leak():
    from src.synth.entity_registry import INDUSTRIES
    blob = "\n".join(discovery.files().values()) + json.dumps(discovery.render_server_json())
    for word in ("premium", '"tier"', "tier:", "R$", "sovereign", "opkey", "/mcp/ops", "qa-internal"):
        assert word not in blob, word
    for slug in ("itau", "bradesco", "nubank", "btg"):          # no entity roster
        assert slug not in blob.lower().replace("itaú", "")
    assert len(discovery.INDUSTRIES) == len(INDUSTRIES)          # every industry, display names only


def test_openapi_describes_real_routes_with_auth():
    spec = discovery.render_openapi()
    assert spec["openapi"] == "3.1.0" and spec["info"]["termsOfService"].endswith("/docs/terms.html")
    for path, ops in spec["paths"].items():
        for op in ops.values():
            assert op["security"] and "401" in op["responses"]
    assert set(spec["paths"]) == {"/api/v1/agent/ask", "/api/ask", "/api/feed"}


def test_agent_card_has_the_a2a_required_fields_and_points_at_real_endpoints():
    c = discovery.render_agent_card()
    for k in ("protocolVersion", "name", "description", "url", "version", "capabilities",
              "defaultInputModes", "defaultOutputModes", "skills"):
        assert c[k], k
    assert c["url"] == "https://onssa.org/a2a" and c["capabilities"]["streaming"] is False
    flow = c["securitySchemes"]["onssa"]["flows"]["authorizationCode"]
    assert flow["authorizationUrl"].endswith("/oauth/authorize") and "onca:read" in flow["scopes"]


def test_server_json_lists_only_the_read_server():
    s = discovery.render_server_json()
    assert [r["url"] for r in s["remotes"]] == ["https://onssa.org/mcp"]
    assert len(s["description"]) <= 100 and s["name"].startswith("io.github.")


def test_mcp_tools_described_match_the_server():
    from src.dashboard import mcp_server
    assert [n for n, _ in discovery.MCP_TOOLS] == [t["name"] for t in mcp_server.read_tools()]


def test_agent_card_is_served_by_the_oauth_lambda():
    from src.dashboard.oauth import handler
    r = handler.lambda_handler({"rawPath": "/.well-known/agent-card.json", "headers": {}})
    assert r["statusCode"] == 200 and json.loads(r["body"]) == discovery.render_agent_card()


def test_only_the_discovery_txt_files_are_in_the_site():
    # CloudFront serves /*.txt publicly (one behavior for llms.txt + ai.txt): nothing else may be .txt
    assert sorted(p.relative_to(SITE).as_posix() for p in SITE.rglob("*.txt")) == ["ai.txt", "llms.txt"]
