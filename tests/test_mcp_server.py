"""#181/#182/#186: Onça's MCP server — licence scoping, provenance on every row, the 401
challenge from the Lambda itself, operator-only ops tools, and A2A message/send."""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.dashboard import mcp_server as m

TODAY = dt.date(2026, 9, 28)
FEED = {
    "as_of": "2026-09-28", "industries": [{"slug": "banking"}, {"slug": "betting"}],
    "entity_attrs": {"itau": {"label": "Itaú", "industries": ["banking"], "ticker": "ITUB4"},
                     "betano": {"label": "Betano", "industries": ["betting"]}},
    "entities": [{"entity": "itau"}, {"entity": "betano"}],
    "feed": [
        {"id": "c1", "date": "2026-09-27", "entity": "itau", "industries": ["banking"], "kind": "entity_fusion",
         "lenses": ["ofertas"], "narrative": "Itaú lançou um fundo.", "threat_score": 0.72, "is_alert": True,
         "citations": [{"url": "https://cvm.gov.br/x"},
                       {"url": "https://news.google.com/rss/articles/xyz", "label": "Valor Econômico",
                        "via": "Google Notícias"}]},
        {"id": "c2", "date": "2026-09-27", "entity": "betano", "industries": ["betting"], "kind": "entity_fusion",
         "narrative": "Betano.", "citations": [{"url": "https://a.b/c"}]},
        {"id": "c3", "date": "2026-07-01", "entity": "itau", "industries": ["banking"], "narrative": "old",
         "citations": [{"url": "https://old"}]},
    ],
    "sector_events": [
        {"id": "se1", "industries": ["banking"], "industry": "banking", "date": "2026-09-25", "title": "MP 1.393",
         "severity": "high", "change_label": "novo marco", "n_affected": 4,
         "sources": [{"url": "https://press/x", "kind": "press"}, {"url": "https://www.in.gov.br/mp", "kind": "official", "organ": "Presidência"},
                     {"url": "https://news.google.com/rss/articles/abc", "kind": "news", "publisher": "O GLOBO"}]},
        {"id": "se2", "industries": ["betting"], "industry": "betting", "date": "2026-09-25", "title": "MP 1.394",
         "severity": "critical", "sources": [{"url": "https://www.in.gov.br/b", "kind": "official"}]}],
    "enforcement": [{"id": "enf1", "entity": None, "entities": ["itau"], "kind": "sancao", "authority": "CVM",
                     "date": "2026-09-20", "title": "CVM multa", "industries": ["banking"],
                     "sources": [{"url": "https://cvm.gov.br/pas"}]}],
}


def ctx(elevated=False, modules=("banking",), acts=None, asks=None):
    return m.Context(principal={"sub": "u1", "tenant": "acme", "groups": ["operator"] if elevated else []},
                     modules=list(modules), elevated=elevated, feed=lambda: FEED,
                     resolve=lambda q: {"itau": ["itau"], "betano": ["betano"],
                                        "cnpj:60701190": ["itau"]}.get(q.lower(), []),
                     get_entity=lambda e: {"display_name": "Itaú Unibanco", "aliases": ["itau", "itaú"],
                                           "cnpj_roots": ["60701190"]} if e == "itau" else None,
                     ask=lambda q, scope: (asks.append((q, scope)) if asks is not None else None) or
                     {"answer": "Resposta.", "grounded": True, "citations": [{"id": "c1", "url": "https://cvm.gov.br/x"}]},
                     act=lambda body: (acts.append(body) if acts is not None else None) or {"outcome": "applied"})


def payload(result):
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    class D(dt.date):
        @classmethod
        def today(cls):
            return TODAY
    monkeypatch.setattr(m.dt, "date", D)


def test_read_tools_are_read_only_and_described_for_a_model():
    tools = m.read_tools()
    assert [t["name"] for t in tools] == ["lookup_entity", "entity_signals", "regulatory_events", "ask"]
    assert all(t["annotations"]["readOnlyHint"] and t["title"] for t in tools)
    assert "CAN BE WRONG" in tools[-1]["description"]


def test_lookup_entity_resolves_by_name_and_cnpj_within_the_licence_only():
    c = ctx()
    p = payload(m.call_read_tool("lookup_entity", {"query": "Itau"}, c))
    assert p["entities"][0]["entity"] == "itau" and p["entities"][0]["cnpj_roots"] == ["60701190"]
    assert p["as_of"] and p["source"] and p["license"] and p["entities"][0]["url"]
    assert payload(m.call_read_tool("lookup_entity", {"query": "60.701.190/0001-04"}, c))["entities"][0]["entity"] == "itau"
    assert payload(m.call_read_tool("lookup_entity", {"query": "Betano"}, c))["entities"] == []   # not licensed


def test_entity_signals_are_dated_sourced_and_windowed():
    p = payload(m.call_read_tool("entity_signals", {"entity": "itau"}, ctx()))
    kinds = [r["kind"] for r in p["signals"]]
    assert kinds == ["entity_fusion", "enforcement:sancao"]            # c3 is outside 30 days
    assert all(r["url"] and r["date"] for r in p["signals"])
    assert p["signals"][0]["threat"] == 72
    srcs = p["signals"][0]["sources"]
    assert srcs[0] == {"url": "https://cvm.gov.br/x"}                  # no invented label
    assert srcs[1] == {"url": "https://news.google.com/rss/articles/xyz", "label": "Valor Econômico",
                       "via": "Google Notícias"}
    assert p["signals"][0]["urls"] == [s["url"] for s in srcs]         # urls kept for old clients
    assert p["signals"][1]["sources"][0]["label"] == "CVM"             # enforcement: the authority
    err = m.call_read_tool("entity_signals", {"entity": "betano"}, ctx())
    assert err["isError"]                                              # unlicensed entity


def test_regulatory_events_are_scoped_and_official_act_first():
    p = payload(m.call_read_tool("regulatory_events", {}, ctx()))
    assert [e["title"] for e in p["events"]] == ["MP 1.393"]            # betting event not licensed
    assert p["events"][0]["url"] == "https://www.in.gov.br/mp"          # official before press
    by_url = {s["url"]: s for s in p["events"][0]["sources"]}
    assert by_url["https://www.in.gov.br/mp"]["label"] == "Presidência"
    gn = by_url["https://news.google.com/rss/articles/abc"]
    assert gn["label"] == "O GLOBO" and gn["via"] == "Google Notícias"
    assert "via" not in by_url["https://www.in.gov.br/mp"]


def test_ask_passes_scope_and_validates_length():
    asks = []
    p = payload(m.call_read_tool("ask", {"q": "O que mudou?", "industry": "Banking"}, ctx(asks=asks)))
    assert p["answer"] and asks == [("O que mudou?", {"industry": "banking"})]
    assert m.call_read_tool("ask", {"q": "x"}, ctx())["isError"]


def test_ops_tools_hidden_and_refused_for_non_operators():
    c = ctx()
    r = m.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, path="/mcp/ops", ctx=c)
    assert r["result"]["tools"] == []
    r = m.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "trigger_run", "arguments": {"idempotency_key": "k" * 10}}},
                     path="/mcp/ops", ctx=c)
    assert r["result"]["isError"]


def test_ops_tools_for_operators_require_an_idempotency_key():
    acts = []
    c = ctx(elevated=True, acts=acts)
    tools = {t["name"]: t for t in m.ops_tools(True)}
    assert "trigger_run" in tools and "record_decision" in tools
    assert all("idempotency_key" in t["inputSchema"]["required"] for t in tools.values())
    assert tools["trigger_run"]["annotations"]["destructiveHint"] is True
    assert tools["propose_registry_change"]["annotations"]["destructiveHint"] is False
    assert tools["list_product_radar"]["annotations"]["readOnlyHint"] is True
    assert m.call_ops_tool("trigger_run", {}, c)["isError"]              # no key → refused
    m.call_ops_tool("trigger_run", {"idempotency_key": "abc12345", "args": {"x": 1}}, c)
    assert acts == [{"intent": "trigger_run", "args": {"x": 1}, "idempotency_key": "abc12345"}]


def test_read_endpoint_never_exposes_ops_tools():
    r = m.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, path="/mcp", ctx=ctx(elevated=True))
    assert {t["name"] for t in r["result"]["tools"]} == {"lookup_entity", "entity_signals", "regulatory_events", "ask"}


def test_notifications_get_no_response_and_initialize_says_what_it_is():
    c = ctx()
    assert m.handle_rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, path="/mcp", ctx=c) is None
    r = m.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, path="/mcp", ctx=c)
    assert r["result"]["protocolVersion"] and "cite" in r["result"]["instructions"]


def test_a2a_message_send_returns_an_agent_message_with_citations():
    r = m.handle_a2a({"jsonrpc": "2.0", "id": 7, "method": "message/send", "params": {"message": {
        "role": "user", "messageId": "m1", "parts": [{"kind": "text", "text": "Quem está em alerta?"}]}}}, ctx())
    res = r["result"]
    assert res["kind"] == "message" and res["role"] == "agent" and res["parts"][0]["text"] == "Resposta."
    assert res["parts"][1]["data"]["citations"][0]["url"]
    assert m.handle_a2a({"jsonrpc": "2.0", "id": 8, "method": "tasks/get"}, ctx())["error"]["code"] == -32601


def test_handler_challenges_unauthenticated_initialize_from_the_lambda(monkeypatch):
    from src.dashboard.oauth import handler as oh
    monkeypatch.setattr(oh, "killed", lambda: False)
    monkeypatch.setenv("ONCA_OAUTH_TABLE", "t")
    monkeypatch.setattr(m, "_public_keys", lambda: {})

    class _Store:
        def __init__(self, *a):
            pass

        def get(self, *a):
            return None
    import src.dashboard.oauth.store as st
    monkeypatch.setattr(st, "DynamoStore", _Store)
    for path in ("/mcp", "/mcp/ops", "/a2a"):
        r = m.lambda_handler({"rawPath": path, "headers": {}, "requestContext": {"http": {"method": "POST"}},
                              "body": json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})})
        assert r["statusCode"] == 401
        assert r["headers"]["www-authenticate"] == (
            'Bearer resource_metadata="https://onssa.org/.well-known/oauth-protected-resource%s", scope="%s"'
            % (path, "onca:write" if path == "/mcp/ops" else "onca:read"))
    r = m.lambda_handler({"rawPath": "/mcp", "headers": {"authorization": "Bearer x.y.z"},
                          "requestContext": {"http": {"method": "POST"}}, "body": "{}"})
    assert r["statusCode"] == 401 and 'error="invalid_token"' in r["headers"]["www-authenticate"]


def test_exact_name_outranks_a_noisy_alias_index_hit():
    # live 2026-09-28: ALIAS#ITAU pointed at a FII ("ITAÚ ICDI11") that carries a bare manager alias
    feed = dict(FEED, entity_attrs=dict(FEED["entity_attrs"], icdi11={"label": "ITAÚ ICDI11", "industries": ["banking"]}))
    c = m.Context(principal={"sub": "u"}, modules=["banking"], elevated=False, feed=lambda: feed,
                  resolve=lambda q: ["icdi11", "itau"] if q.lower() in ("itaú", "itau") else [],
                  get_entity=lambda e: None, ask=None, act=None)
    ids = [r["entity"] for r in payload(m.call_read_tool("lookup_entity", {"query": "Itaú"}, c))["entities"]]
    assert ids[0] == "itau" and "icdi11" in ids


def test_certifications_as_strings_and_tool_errors_become_is_error_results():
    feed = dict(FEED, entity_attrs=dict(FEED["entity_attrs"], itau=dict(FEED["entity_attrs"]["itau"],
                                                                        certifications=["Autorização BCB"])))
    c = m.Context(principal={"sub": "u"}, modules=["banking"], elevated=False, feed=lambda: feed,
                  resolve=lambda q: ["itau"], get_entity=lambda e: None, ask=None, act=None)
    assert payload(m.call_read_tool("lookup_entity", {"query": "Itaú"}, c))["entities"][0]["certifications"] == ["Autorização BCB"]
    boom = m.Context(principal={"sub": "u"}, modules=["banking"], elevated=False,
                     feed=lambda: (_ for _ in ()).throw(RuntimeError("s3 down")),
                     resolve=lambda q: [], get_entity=lambda e: None, ask=None, act=None)
    r = m.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "regulatory_events", "arguments": {}}}, path="/mcp", ctx=boom)
    assert r["result"]["isError"] is True


def test_request_log_fields_identify_client_without_raw_user():
    from src.dashboard.mcp_server import request_log_fields
    init = {"jsonrpc": "2.0", "id": 7, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "claude-ai", "version": "0.1.0"}}}
    hdr = {"user-agent": "Claude-User", "mcp-protocol-version": "2025-06-18"}
    p = {"sub": "abc-123", "client_id": "https://claude.ai/oauth/mcp-oauth-client-metadata"}
    f = request_log_fields(init, hdr, p)
    assert f["rpc_id"] == 7 and f["proto_req"] == "2025-06-18" and f["client"] == "claude-ai/0.1.0"
    assert f["ua"] == "Claude-User" and f["client_id"].startswith("https://claude.ai/")
    assert f["user"] and "abc-123" not in str(f)
    # notifications / unauthenticated: no id, no principal → keys omitted, never raises
    assert request_log_fields({"method": "notifications/initialized"}, {}, None) == {}
    assert request_log_fields([init, init], {}, None) == {"batch": 2}
