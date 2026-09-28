"""Onça's machine-readable front door (#180, #186, #183) — generated, never hand-edited.

* ``/llms.txt`` — orientation for a model: what Onça answers, which endpoint for which question.
* ``/ai.txt`` — terms for AI crawlers and agents (permission, attribution).
* ``/openapi.json`` — the authenticated REST surface a developer can call.
* ``/.well-known/agent-card.json`` — the A2A agent card (#186), served by the OAuth Lambda (the
  whole ``/.well-known/*`` path is one CloudFront behavior — the distribution's behavior quota).
* ``server.json`` — the official MCP Registry entry (#183; READ server only — ``/mcp/ops`` is
  never listed anywhere public).

All of it DESCRIBES the existing surface; nothing here opens new data. No tenant data, entity
list, prices or tier internals (test-enforced). ``scripts/build_discovery.py`` writes the static
files into the site; ``tests/test_discovery.py`` fails if a committed file drifts from this.
"""
from __future__ import annotations

import json
from typing import Any

BASE = "https://onssa.org"
VERSION = "1.0.0"
TITLE = "Onça — Brazilian financial-sector competitive & regulatory intelligence"
SUMMARY_EN = ("Competitive and regulatory intelligence on Brazilian financial institutions, built from "
              "public records (CVM, BCB, CADE, DOU, CEIS/CNEP, PNCP, Receita) and specialist press, "
              "resolved to a curated entity registry. Every row links to its primary source.")
SUMMARY_PT = ("Inteligência competitiva e regulatória sobre instituições financeiras brasileiras, a partir de "
              "registros públicos (CVM, BCB, CADE, DOU, CEIS/CNEP, PNCP, Receita) e imprensa especializada, "
              "resolvidos a um registro curado de entidades. Toda linha aponta para a fonte primária.")

# Display names only — the licensing tier of each industry is a pricing internal, never published.
INDUSTRIES = ["Banking", "Investment Banking", "Insurance", "Asset Management", "Wealth Management",
              "Private Markets (VC/PE)", "Fintech", "Financial Data & Analytics", "Advisory",
              "Crypto & Digital Assets", "Consórcios", "Betting & iGaming", "Fundos Imobiliários (FIIs)",
              "Fundos do Agro (FIAGRO)", "Adquirência (Maquininhas)", "Previdência Fechada (EFPCs)",
              "Securitização & Crédito"]
SOURCES = ["CVM (fatos relevantes, ofertas, informes, normas, PAS)", "Banco Central (normativos, "
           "IF.data, balancetes, Pix)", "CADE (atos de concentração)", "Diário Oficial da União",
           "CEIS/CNEP (sanções)", "PNCP (compras públicas)", "Receita Federal (QSA)", "SUSEP, PREVIC, ANS",
           "App Store / YouTube (radar de produto)", "imprensa especializada"]

MCP_TOOLS = [
    ("lookup_entity", "Resolve a Brazilian institution by name, alias or CNPJ to the curated registry."),
    ("entity_signals", "Dated regulatory, sanction, product and distress signals for one institution, each linked to its primary source."),
    ("regulatory_events", "Sector-wide regulatory changes (official act first) for your licensed industries."),
    ("ask", "A grounded, cited answer from your licensed data (a language model — can be wrong)."),
]

ENDPOINTS: list[dict[str, Any]] = [
    {"path": "/api/v1/agent/ask", "method": "POST", "auth": "tenantApiKey",
     "summary": "Grounded question-answering for a tenant's own agents (API key).",
     "description": ("Answers ONLY from the tenant's licensed Onça data, with citations to primary sources; "
                     "declines when the data doesn't support an answer. A language-model call — it can be wrong; "
                     "check `citations`. Metered per tenant."),
     "request": {"q": "Quais mudanças regulatórias recentes afetam bancos?", "scope": {"industry": "banking"}}},
    {"path": "/api/ask", "method": "POST", "auth": "onssaSignIn",
     "summary": "The same grounded Q&A for a signed-in Onça user (dashboard session token).",
     "description": "Same contract as /api/v1/agent/ask, authenticated with the user's Onça sign-in (Cognito ID token).",
     "request": {"q": "Quem está em alerta agora?", "officer": "cso"}},
    {"path": "/api/feed", "method": "GET", "auth": "onssaSignIn",
     "summary": "The caller's licensed feed: cards, entities, regulatory and sector events.",
     "description": ("Server-scoped to the caller's licensed industries (never filtered in the client). Each card "
                     "carries its date, entity, threat score and citations to primary sources."),
     "request": None},
]


def _mcp_block(b: str) -> list[str]:
    return ([f"### MCP server — `{b}/mcp` (streamable HTTP, JSON-RPC 2.0, OAuth sign-in)", "",
             "Connect any MCP client (e.g. `claude mcp add --transport http onca " + b + "/mcp`). The client "
             "opens an Onça sign-in; only provisioned Onça accounts are admitted, and every tool is limited to "
             "the account's licensed industries.", ""] +
            [f"- `{n}` — {d}" for n, d in MCP_TOOLS] + [""])


def render_llms_txt(base: str = BASE) -> str:
    lines = [
        f"# {TITLE}", "", f"> {SUMMARY_EN}", "", SUMMARY_PT, "",
        "Onça serves strategy, regulatory, compliance and product officers at Brazilian banks, insurers, "
        "fintechs and asset managers: who moved, which rule changed, who was sanctioned, which competitor "
        "launched what — for their licensed industries, with a link to the primary source on every row.", "",
        "## Coverage", "", "Industries: " + ", ".join(INDUSTRIES) + ".", "",
        "Sources: " + "; ".join(SOURCES) + ".", "",
        "## Access", "",
        "Onça is a licensed product: every interface requires an Onça account (provisioned per organization) "
        f"or a tenant API key. Plans and a public sample: {base}/pricing.html · {base}/sample/", "",
        "## Choosing an interface", "",
    ] + _mcp_block(base) + [
        "### REST", "",
    ]
    for e in ENDPOINTS:
        lines += [f"- `{e['method']} {base}{e['path']}` — {e['summary']} {e['description']}"]
    lines += [
        "", f"### A2A agent — `{base}/a2a` (card: {base}/.well-known/agent-card.json)", "",
        "## Honesty posture", "",
        "- Every row carries `as_of`, `source` and a URL to the primary record (CVM, BCB, DOU…). Cite it.",
        "- `lookup_entity`, `entity_signals` and `regulatory_events` are deterministic retrieval. `ask` is a "
        "language model grounded ONLY in the licensed data; it returns citations and declines rather than guess.",
        "- Absence of a signal is reported as absence, never as \"no risk\".", "",
        "## Machine-readable", "",
        f"- OpenAPI 3.1: {base}/openapi.json", f"- Terms for AI systems: {base}/ai.txt",
        f"- OAuth authorization server: {base}/.well-known/oauth-authorization-server/oauth",
        f"- Privacy: {base}/docs/privacy.html · Terms: {base}/docs/terms.html", "",
        "## Attribution", "", f"Cite as **Onça (onssa.org)** plus the primary source of each fact.", "",
    ]
    return "\n".join(lines)


def render_ai_txt(base: str = BASE) -> str:
    return "\n".join([
        "# ai.txt — terms for AI crawlers, answer engines and agents · onssa.org (Onça)",
        "", "User-Agent: *", "",
        "# Public pages you may read, index and quote (with attribution):",
        "Allow: /llms.txt", "Allow: /ai.txt", "Allow: /openapi.json", "Allow: /.well-known/agent-card.json",
        "Allow: /pricing.html", "Allow: /sample/", "Allow: /docs/", "",
        "# Licensed data: only through an authorized interface (MCP with sign-in, the tenant API),",
        "# within the licensing organization. Do not scrape, cache or redistribute it outside that organization.",
        "Disallow: /exec", "Disallow: /app", "Disallow: /api/", "Disallow: /feed", "",
        "# Attribution: cite \"Onça (onssa.org)\" and the primary source each row links to (CVM, BCB, DOU…).",
        "# Carry the qualifications through: `ask` answers come from a model and cite their sources.", "",
        f"Contact: contato@onssa.org", f"Manifest: {base}/llms.txt", f"OpenAPI: {base}/openapi.json", "",
    ])


_ANSWER = {"type": "object", "required": ["answer"], "properties": {
    "answer": {"type": "string"}, "refused": {"type": "boolean"}, "grounded": {"type": "boolean"},
    "citations": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "entity_label": {"type": "string"}, "date": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "object", "properties": {"url": {"type": "string"}}}}}}}}}
_FEED = {"type": "object", "properties": {
    "as_of": {"type": "string"}, "scoped_modules": {"type": "array", "items": {"type": "string"}},
    "feed": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "date": {"type": "string"}, "entity": {"type": ["string", "null"]},
        "narrative": {"type": "string"}, "threat_score": {"type": "number"},
        "citations": {"type": "array", "items": {"type": "object", "properties": {"url": {"type": "string"}}}}}}},
    "sector_events": {"type": "array", "items": {"type": "object"}}}}


def render_openapi(base: str = BASE) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for e in ENDPOINTS:
        op: dict[str, Any] = {
            "summary": e["summary"], "description": e["description"],
            "operationId": e["path"].strip("/").replace("/", "_").replace("api_", ""),
            "security": [{e["auth"]: []}],
            "responses": {"200": {"description": "OK", "content": {"application/json": {
                "schema": _FEED if e["method"] == "GET" else _ANSWER}}},
                "401": {"description": "Missing or invalid credentials"},
                "403": {"description": "No active licence for this account/key"}}}
        if e["request"] is not None:
            op["requestBody"] = {"required": True, "content": {"application/json": {
                "schema": {"type": "object", "required": ["q"], "properties": {
                    "q": {"type": "string", "maxLength": 800, "description": "The question (PT or EN)."},
                    "scope": {"type": "object", "properties": {"industry": {"type": "string"}}},
                    "officer": {"type": "string", "enum": ["cso", "cro", "cco", "cpo", "auto"]}}},
                "example": e["request"]}}}
        paths[e["path"]] = {e["method"].lower(): op}
    return {
        "openapi": "3.1.0",
        "info": {"title": "Onça API", "version": VERSION, "summary": SUMMARY_EN,
                 "description": SUMMARY_EN + " See /llms.txt for orientation and /ai.txt for terms. The MCP "
                                             "server at /mcp exposes the same data as tools (OAuth sign-in).",
                 "termsOfService": f"{base}/docs/terms.html",
                 "contact": {"name": "Onça (Smart Signals LLC)", "url": base, "email": "contato@onssa.org"},
                 "license": {"name": "Licensed data — see the terms", "url": f"{base}/docs/terms.html"},
                 "x-logo": {"url": f"{base}/v3/icons/icon-512.png", "backgroundColor": "#0e1214"}},
        "externalDocs": {"description": "Orientation for AI agents (llms.txt)", "url": f"{base}/llms.txt"},
        "servers": [{"url": base}],
        "components": {"securitySchemes": {
            "tenantApiKey": {"type": "http", "scheme": "bearer",
                             "description": "A tenant API key (sk_onca_…) issued in the tenant's admin panel."},
            "onssaSignIn": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT",
                            "description": "An Onça sign-in ID token (Cognito)."}}},
        "paths": paths,
    }


def render_agent_card(base: str = BASE) -> dict[str, Any]:
    """A2A agent card (protocol 0.3). Only `message/send` is served — say so, no streaming."""
    return {
        "protocolVersion": "0.3.0", "name": "Onça", "version": VERSION,
        "description": SUMMARY_EN + " Answers are grounded only in the caller's licensed data and cite sources.",
        "url": f"{base}/a2a", "preferredTransport": "JSONRPC",
        "provider": {"organization": "Smart Signals LLC (Onça)", "url": base},
        "documentationUrl": f"{base}/llms.txt", "iconUrl": f"{base}/v3/icons/icon-512.png",
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain", "application/json"],
        "securitySchemes": {"onssa": {"type": "oauth2", "description": "Onça sign-in (provisioned accounts only).",
                                      "flows": {"authorizationCode": {
                                          "authorizationUrl": f"{base}/oauth/authorize",
                                          "tokenUrl": f"{base}/oauth/token",
                                          "scopes": {"onca:read": "Read your licensed Onça data and ask Onça."}}}}},
        "security": [{"onssa": ["onca:read"]}],
        "skills": [{"id": "ask", "name": "Ask Onça",
                    "description": ("Grounded, cited answers about Brazilian financial institutions and regulation — "
                                    "competitor moves, sanctions, regulatory changes, distress — for the caller's "
                                    "licensed industries. Declines when the data doesn't support an answer."),
                    "tags": ["brazil", "financial-services", "regulation", "competitive-intelligence", "compliance"],
                    "examples": ["Quais mudanças regulatórias recentes afetam bancos?",
                                 "What did the CVM sanction this month?", "Quem está em alerta agora no setor de seguros?"],
                    "inputModes": ["text/plain"], "outputModes": ["text/plain", "application/json"]}],
    }


def render_server_json(base: str = BASE) -> dict[str, Any]:
    """Official MCP Registry entry (#183). The READ server only."""
    return {
        "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
        "name": "io.github.marcioyoshida/onca",
        "title": "Onça — Brazil financial competitive & regulatory intelligence",
        # the registry caps descriptions at 100 characters
        "description": "Brazilian financial institutions: entity registry, sourced regulatory signals, cited Q&A",
        "version": VERSION,
        "websiteUrl": base,
        "icons": [{"src": f"{base}/v3/icons/icon-512.png", "mimeType": "image/png", "sizes": ["512x512"]}],
        "remotes": [{"type": "streamable-http", "url": f"{base}/mcp"}],
    }


def files(base: str = BASE) -> dict[str, str]:
    """published path → content, for scripts/build_discovery.py and the drift test."""
    return {
        "llms.txt": render_llms_txt(base),
        "ai.txt": render_ai_txt(base),
        "openapi.json": json.dumps(render_openapi(base), ensure_ascii=False, indent=1) + "\n",
    }
