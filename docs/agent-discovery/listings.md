# Onça — directory and registry submission kit (#183)

Everything here lists the **read** server `https://onssa.org/mcp` only. `/mcp/ops` is never submitted anywhere.
Each submission is made by the owner, under the owner's own login, one at a time.

| Where | What to submit | Status |
|---|---|---|
| **Official MCP Registry** | `server.json` at the repo root (`io.github.marcioyoshida/onca`, one streamable-http remote). Publish with `mcp-publisher login github` then `mcp-publisher publish` from the repo root. | ready |
| **Glama** | Auto-lists from the official registry; claim the listing with the GitHub account. | after the registry |
| **Smithery** | "Add server" → remote URL `https://onssa.org/mcp`, OAuth (discovered from the 401 challenge). | ready |
| **PulseMCP** | Submit form: name, `https://onssa.org/mcp`, the `llms.txt` summary, the privacy/terms URLs. | ready |
| **Claude connectors directory** | Remote MCP with OAuth; privacy `https://onssa.org/docs/privacy.html`, terms `https://onssa.org/docs/terms.html`, support `contato@onssa.org`, tool annotations present (`readOnlyHint` on all four). Reviewer test account: a provisioned demo tenant, to be created at submission time. | ready — needs a reviewer tenant |
| **APIs.guru** | Submit `https://onssa.org/openapi.json` (valid OpenAPI 3.1, `x-logo` set). | ready |
| **Postman public workspace** | Import `https://onssa.org/openapi.json` into a public workspace. | ready |

Pre-flight before any submission:
1. `curl -si -X POST https://onssa.org/mcp -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}'` → **401** with `WWW-Authenticate: Bearer resource_metadata=…`.
2. `AWS_PROFILE=my2027 .venv/bin/python scripts/oauth_mcp_e2e.py` → all PASS.
3. Owner review of `/docs/privacy.html` and `/docs/terms.html`.
