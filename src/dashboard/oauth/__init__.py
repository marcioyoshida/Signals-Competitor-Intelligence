"""OAuth 2.1 authorization server for Onça's remote MCP / A2A endpoints (#181, #182, #186).

A port of the fleet pattern built for Tarantula (Cyber-Monitor-Intelligence #99, ADR
docs/2026-09-27-adr-mcp-oauth-authorization.md there; threat IDs T1–T21 cited in comments come
from its threat model). Onça-specific: the principal is a provisioned USER (tenant + Cognito
groups, entitlement recomputed live), three audience-bound resources (/mcp read, /mcp/ops
write — operators only —, /a2a read), and verification in pure Python (no crypto dependency in
the shared Lambda bundle). See docs/2026-09-28-adr-agent-discovery-mcp.md.
"""
