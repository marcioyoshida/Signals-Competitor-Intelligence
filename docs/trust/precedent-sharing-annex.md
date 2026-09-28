# Anexo — Compartilhamento de padrões de decisão (opt-in)

*Contract annex for design-partner and SaaS agreements. Portuguese is the governing text; the
English summary is for internal reference. It implements the accepted ADR
`docs/2026-09-28-adr-dec7-cross-tenant-precedent-governance.md` (#100). Counsel must review it
before first use.*

## Texto do anexo (pt-BR)

**1. Objeto.** Este anexo regula a participação opcional do CLIENTE no conjunto de *padrões de
decisão de mercado* da Onça: estatísticas agregadas sobre como instituições reagem a categorias de
sinais públicos e quais resultados observam.

**2. Participação voluntária e separada.** A participação tem duas opções independentes, ambas
desativadas por padrão:
- (a) **Contribuir**: permitir que decisões encerradas do CLIENTE, com resultado revisado, entrem
  na base agregada;
- (b) **Consultar**: receber os padrões agregados como referência nas recomendações.

A opção (b) não depende da opção (a). Cada opção só vale após assinatura deste anexo **e** ativação
pelo administrador do CLIENTE no produto. A ativação é registrada (quem e quando).

**3. O que nunca é compartilhado.** Nunca saem do ambiente do CLIENTE: a identidade do CLIENTE,
de seus usuários ou dispositivos; qualquer texto livre (justificativas, notas de resultado,
recomendações), inclusive paráfrases geradas por IA; e decisões individuais.

**4. O que pode compor um padrão.** Somente campos categóricos e agregados: categoria do sinal,
entidade pública citada no sinal, setor, papel do executivo, veredito, classe de resultado e
semana. Um padrão só é publicado quando reúne **ao menos 5 instituições distintas** e nenhuma delas
responde por mais de **50%** das decisões do padrão. Contagens abaixo de 10 aparecem em faixas.

**5. Revogação.** O CLIENTE pode desativar qualquer opção a qualquer momento. A base agregada é
reconstruída do zero a cada versão, e as contribuições do CLIENTE deixam de compor a versão
seguinte.

**6. Clientes Sovereign (na própria conta AWS).** Não há canal automático de envio. A
contribuição, se houver, ocorre apenas por exportação gerada e revisada pelo administrador do
CLIENTE. A consulta se dá por pacote assinado e versionado, entregue com as atualizações.

**7. Base legal e papéis (LGPD).** A participação se fundamenta neste contrato e no consentimento
do CLIENTE (art. 7º, I e V). A Onça atua como controladora da base agregada e publica as regras de
agregação acima em sua política de privacidade.

## English summary

Opt-in, off by default; *contribute* and *consume* are separate switches. Each needs the signed
annex **and** the tenant admin's in-product switch, and both are journaled. Only aggregate
patterns (k ≥ 5 institutions, ≤ 50% dominance, ranges below 10) ever leave a tenant: no identity,
no free text, no single decision. Revocation removes the tenant from the next full rebuild.
Sovereign tenants have no automatic channel: they contribute by admin-reviewed export and consume
by signed pack.
