# Onça — directory and registry submission kit (#183)

Everything here lists the **read** server `https://onssa.org/mcp` only. `/mcp/ops` is never submitted anywhere.
Each submission is made by the owner, under the owner's own login, one at a time.

| Where | What to submit | Status |
|---|---|---|
| **Official MCP Registry** | `server.json` at the repo root (`io.github.marcioyoshida/onca`, one streamable-http remote). | **live** v1.0.0 (2026-09-28) |
| **Glama** | Auto-listed from the registry: https://glama.ai/mcp/connectors/io.github.marcioyoshida/onca. Claim it with the GitHub account. | **listed** (claim pending) |
| **Smithery** | "Add server" → remote URL `https://onssa.org/mcp`, OAuth (discovered from the 401 challenge). | ready |
| **PulseMCP** | Submit form: name, `https://onssa.org/mcp`, the `llms.txt` summary, the privacy/terms URLs. | ready |
| **Claude connectors directory** | Remote MCP with OAuth; privacy `https://onssa.org/docs/privacy.html`, terms `https://onssa.org/docs/terms.html`, support `contato@onssa.org`, tool annotations present (`readOnlyHint` on all four). Reviewer test account: `directory-reviewer` (below). | **ready**: reviewer verified live 11/11 (2026-09-30) |
| **APIs.guru** | `https://onssa.org/openapi.json` (valid OpenAPI 3.1, `x-logo` set). | submitted: APIs-guru/openapi-directory#3464 |
| **Postman public workspace** | Import `https://onssa.org/openapi.json` into a public workspace. | ready |

Pre-flight before any submission:
1. `curl -si -X POST https://onssa.org/mcp -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}'` → **401** with `WWW-Authenticate: Bearer resource_metadata=…`.
2. `AWS_PROFILE=my2027 .venv/bin/python scripts/oauth_mcp_e2e.py` → all PASS.
3. Owner review of `/docs/privacy.html` and `/docs/terms.html`.

## Paste-ready form text

**Name:** Onça — Brazil financial competitive & regulatory intelligence
**Server URL:** `https://onssa.org/mcp` (streamable HTTP; OAuth 2.1 + PKCE, discovered from the 401; Client ID Metadata Documents, no DCR)
**Short description (≤100):** Brazilian financial institutions: entity registry, sourced regulatory signals, cited Q&A
**Long description:**
> Competitive and regulatory intelligence on Brazilian banks, insurers, fintechs and asset managers, built from public records (CVM, Banco Central, CADE, Diário Oficial, CEIS/CNEP, PNCP, Receita) and specialist press, resolved to a curated entity registry. Four read-only tools: resolve an institution by name/alias/CNPJ, list its dated signals, list regulatory events by industry, and ask a grounded question answered with citations. Every row links to its primary source. Requires an Onça account; each tool is limited to the account's licensed industries.

**Category:** Finance (secondary: Legal & compliance, Government data)
**Tools (all `readOnlyHint: true`):** `lookup_entity`, `entity_signals`, `regulatory_events`, `ask`
**Example prompts:**
1. "Use Onça to find what changed at Nubank in the last 30 days, with sources."
2. "Which recent Banco Central or CVM rules affect digital banks?"
3. "Resolve Itaú in Onça and list its latest regulatory signals."

**Links:** website `https://onssa.org` · privacy `https://onssa.org/docs/privacy.html` · terms `https://onssa.org/docs/terms.html` · support `contato@onssa.org` · docs `https://onssa.org/llms.txt` · icon `https://onssa.org/v3/icons/icon-512.png`

**Reviewer instructions (Claude directory):**
> Connect `https://onssa.org/mcp`; the connector opens the Onça sign-in. Use the test account below (licensed for Banking + Fintech). On the consent screen choose "Permitir". Try the example prompts above. Institutions outside the licence (e.g. insurers) are intentionally not returned.

Test account: user and password are in AWS Secrets Manager `signalscompetitor/onca/directory-reviewer` (tenant `directory-reviewer`, saas: banking + fintech). Copy them into the form yourself; never paste them into a ticket or chat. Rotate the password after the review.
