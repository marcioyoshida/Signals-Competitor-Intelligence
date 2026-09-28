# Onça no Gemini Enterprise (A2A) — guia do administrador (#186)

A Onça publica um agente A2A: card em `https://onssa.org/.well-known/agent-card.json`, endpoint `https://onssa.org/a2a` (JSON-RPC `message/send`). Ele responde perguntas fundamentadas **só** nos dados licenciados da sua organização, com citações.

## Registro (Gemini Enterprise → Agents → Add agent → A2A)
1. **Agent card:** cole o JSON de `https://onssa.org/.well-known/agent-card.json`.
2. **Autorização OAuth 2.0:** peça à Onça (contato@onssa.org) um cliente pré-registrado para o Gemini Enterprise. Registramos com os redirects do Google e o recurso `/a2a`:
   `scripts/oauth_register_client.py --name "Gemini Enterprise (<empresa>)" --resource /a2a --redirect https://vertexaisearch.cloud.google.com/oauth-redirect --redirect https://vertexaisearch.cloud.google.com/static/oauth/oauth.html --out …`
3. Preencha:
   - **Client ID / Client secret:** os que a Onça enviar
   - **Authorization URI:** `https://onssa.org/oauth/authorize?client_id=<client ID>&response_type=code&redirect_uri=https://vertexaisearch.cloud.google.com/static/oauth/oauth.html&scope=onca:read`
   - **Token URI:** `https://onssa.org/oauth/token`
   - **Scopes:** `onca:read`
4. No primeiro uso, cada usuário entra com a própria conta Onça e autoriza em **"Permitir acesso à Onça?"**.

## Listagem na Google Cloud Marketplace (catálogo de agentes)
Exige uma conta de parceiro Google Cloud Marketplace da Smart Signals LLC; ainda não criada (ver o ADR `docs/2026-09-28-adr-agent-discovery-mcp.md` §5).
