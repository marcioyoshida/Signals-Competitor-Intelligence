# Onça no Microsoft Copilot Studio — guia do administrador (#184)

Conecte o servidor MCP da Onça a um agente do Copilot Studio. Os usuários fazem perguntas no Microsoft 365 Copilot / Teams e recebem respostas com os dados **da licença Onça da sua organização**. Cada usuário entra com a **própria** conta Onça; nada é compartilhado entre usuários ou organizações.

## Pré-requisitos
- Um ambiente Copilot Studio e permissão para criar ferramentas (tools) no agente.
- Contas Onça provisionadas para os usuários (tenant ativo). Contas sem licença são recusadas no login.
- Uma política de dados do Power Platform que permita conectores personalizados (o MCP usa conectores).

## 1. Peça as credenciais do cliente à Onça
O Copilot Studio usa OAuth 2.0 no modo **Manual**, que precisa de um *client ID* e de um *client secret* pré-registrados. A Onça **não** oferece "Dynamic discovery" (registro dinâmico de clientes), por decisão de segurança.

1. No Copilot Studio: **Tools → Add a tool → New tool → Model Context Protocol**.
2. Preencha:
   - **Server name:** `Onça`
   - **Server description:** `Inteligência competitiva e regulatória sobre instituições financeiras brasileiras: registro de entidades, sinais com fonte primária (CVM, BCB, DOU…), eventos regulatórios setoriais e respostas fundamentadas.`
   - **Server URL:** `https://onssa.org/mcp`
3. **Authentication → OAuth 2.0 → Manual**, com:
   - **Authorization URL:** `https://onssa.org/oauth/authorize`
   - **Token URL template** e **Refresh URL:** `https://onssa.org/oauth/token`
   - **Scopes:** `onca:read`
   - *Client ID* e *Client secret*: deixe provisório e copie o **callback URL** que aparece depois de **Create**.
4. Envie o callback URL para **contato@onssa.org**. A Onça registra o cliente (`scripts/oauth_register_client.py --redirect <callback URL>`) e devolve *client ID* + *secret* por um canal seguro.
5. Edite a ferramenta com o *client ID* e o *secret* recebidos → **Create a new connection** → **Add to agent**.

## 2. Teste
Pergunte ao agente: *"Quais mudanças regulatórias recentes afetam bancos?"* — no primeiro uso, o Copilot pede o login Onça e mostra a tela **"Permitir acesso à Onça?"**. A resposta traz links para as fontes primárias.

## Segurança
- A Onça guarda só o hash do *secret*; tokens de acesso duram 1 hora, com renovação de até 30 dias (7 dias sem uso).
- As ferramentas são somente leitura. As ferramentas de operação (`/mcp/ops`) não ficam disponíveis por este conector.
- Para revogar: peça a revogação do cliente (contato@onssa.org) ou desative o usuário na Onça; o acesso cai na próxima renovação.
