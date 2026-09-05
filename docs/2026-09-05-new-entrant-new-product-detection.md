# New-entrant & new-product detection — the 2-job in-pipeline model + source matrix

- Status: **DESIGN / SOURCES RAISED — 2026-09-05.** Adapts the sister project's two-job
  entrant/product framing to Onça's financial-services regulators. Documents what already exists,
  and **raises the likely regulator sources** for the sectors/jobs still uncovered.
- Builds on: `bcb_autorizacoes` (Job-1 pattern), `cvm_ofertas` (Job-2 pattern), the entrant
  machinery (`_new_since_last_run` delta → `entrants` lens → `discover_*` auto-create/propose →
  `receita_cnpj.enrich_entrants`), the discovery framework (ADR-011), and the source registry
  (ADR-019, `src/ingest/registry.py`).

## The two jobs (sister-project framing, mapped to Onça)

- **Job 1 — New entrant** = a *company new to the sector*. Observable event: a new record appears
  in the sector regulator's **company / licence registry**. Onça already does this for the
  BCB-regulated sectors (`bcb_autorizacoes` scans BCB "Instituições em funcionamento"; a new row →
  `entrants` lens → `discover_bcb_institutions` classifies + auto-creates/proposes, Receita-enriched).
- **Job 2 — New product of a tracked entity** = an existing competitor launches something.
  Observable event: a new record in the regulator's **product / offering registry**, attributed to
  a tracked entity. Onça already does this for CVM securities (`cvm_ofertas`, RCVM 160 offerings →
  `ofertas` lens) + new fund registrations (`cvm_fiagro`/`cvm_fundos`).

Both jobs reuse the same in-pipeline shape — **fetch registry → `detect_new` on stable ids
(first-run seed-suppressed) → attribute → lens (`entrants` for Job 1, `ofertas`/`products` for
Job 2) → discovery (Job 1 auto-create/propose; Job 2 attribute to the existing entity)** — so a
new source is a new ingester + a lens mapping, not new machinery.

## What's covered today

| Sector | Regulator | Job 1 (entrant) | Job 2 (product) |
|---|---|---|---|
| Banking / cooperativas | BCB | ✅ `bcb_autorizacoes` | partial (news `ofertas`) |
| Fintech / payments (SCD/SEP/SCFI/IP) | BCB | ✅ `bcb_autorizacoes` + `discover_bcb_institutions` | ❌ (payment arrangements not scanned) |
| Consórcio | BCB | ✅ `discover_consorcio` (`bcb_consorcio`) | partial (new grupos) |
| Agri-funds (FIAGRO) / FII | CVM | ✅ `discover_fiagro` | ✅ new fund registrations |
| Asset mgmt / IB / securities | CVM / B3 | partial (issuers via financials/discovery) | ✅ `cvm_ofertas` (RCVM 160) |
| **Insurance / capitalização / open-pension** | **SUSEP** | ❌ | ❌ |
| **Closed-pension (EFPC)** | **PREVIC** | ❌ | ❌ |
| **Betting / iGaming** | **SPA/MF** | ❌ | ❌ (product = licensed operator = entrant) |
| **Crypto / VASP** | **BCB (VASP)** | ❌ | ❌ |
| Advisory / AAI / consultores | CVM | ❌ (participant registry not scanned) | n/a |

## Likely sources raised (the ask) — endpoints to VERIFY before building

### Job 1 — new-entrant / company-licence registries

| # | Sector | Regulator registry (raise) | Access | Delta key |
|---|---|---|---|---|
| E1 | **Insurance / resseguro / capitalização** | **SUSEP** — relação de entidades supervisionadas (seguradoras, resseguradoras, EAPC, sociedades de capitalização, corretoras) | SUSEP Dados Abertos / SES (Sistema de Estatísticas) — *verify dataset URL* | CNPJ / código SUSEP |
| E2 | **Closed-pension (EFPC)** | **PREVIC** — relação de EFPCs (entidades fechadas de previdência complementar) + planos administrados | PREVIC publica a relação (portal/dados abertos) — *verify* | CNPB / CNPJ |
| E3 | **Betting / iGaming** | **SPA/MF** (Secretaria de Prêmios e Apostas) — relação de casas de apostas **autorizadas** (Lei 14.790) | SPA publica a lista de autorizadas (gov.br/fazenda/spa) — *verify machine-readable form* | CNPJ / nº autorização |
| E4 | **Crypto / VASP** | **BCB** — registro de prestadores de serviços de ativos virtuais (framework Lei 14.478 + regulação BCB em curso) + PSAV atuais | BCB (quando publicado) — *framework em implementação; monitorar* | CNPJ |
| E5 | **Advisory / consultores / AAI** | **CVM** — registro de participantes (consultores de valores mobiliários, agentes autônomos, administradores fiduciários, securitizadoras) | CVM Dados Abertos — cadastro de participantes — *verify* | CNPJ / código CVM |
| E6 | (harden) all BCB sectors | **BCB** — already `bcb_autorizacoes`; extend the delta to emit the BCB **class** so the entrant is sector-typed at ingest | done source, extend | CNPJ + class |

### Job 2 — new-product / offering registries

| # | Sector | Regulator product registry (raise) | Signal |
|---|---|---|---|
| P1 | **Insurance / previdência aberta / capitalização** | **SUSEP** — registro de **produtos** (planos de seguro / previdência / capitalização registrados) | a tracked seguradora registering a new plan |
| P2 | **Payments** | **BCB** — registro de **arranjos de pagamento** (novos arranjos autorizados) | a tracked IP launching a new arrangement |
| P3 | **Consórcio** | **BCB** — novos **grupos** de consórcio por administradora | a tracked administradora opening a group |
| P4 | **Securities / funds (deepen)** | **CVM** — beyond RCVM 160: **FIDC/CRI/CRA** registrations, new fund classes (RCVM 175) | a tracked manager launching a fund/securitization |
| P5 | **Listed products** | **B3** — new ETFs / BDRs / índices / listagens | a tracked issuer listing a product |

## In-pipeline design (reuse, don't reinvent)

1. **Ingester per source** (`src/ingest/<regulator>_<registry>.py`) following `bcb_autorizacoes` /
   `cvm_ofertas`: fetch → normalize → `detect_new` on a stable id → seed-suppress first run.
2. **Register in `src/ingest/registry.py`** (ADR-019): a `SourceSpec` mapping the source to the
   `entrants` lens (Job 1) or `ofertas`/a new `products` lens (Job 2), `resolution="cnpj"`.
3. **Job 1 → discovery**: route new rows through a `discover_<sector>` (or generalize
   `discover_bcb_institutions`) — name-quality gate + prominence gate + the ADR-017 sub-entity
   auto-link already shipped; Receita-enrich the CNPJ.
4. **Job 2 → attribution**: resolve the product's issuer to a tracked entity and emit an
   `ofertas`/`products` card (a *new-product* signal on that entity) — no new entity created.
5. **Gating**: each source `ONCA_INGEST_<SOURCE>`-gated + `detect_new` seed-suppressed, so a first
   run never floods; heavy scans default-off (the ADR-019 discipline).
6. **Verticals (ADR-019)**: SUSEP/PREVIC/SPA sources are first-class for the sectorial split — the
   same ingesters serve the Anteater regulated-sectors product.

## Guardrails (unchanged)

- First-run seed-suppression (never alert the historical baseline as "new").
- Name-quality + prominence gates before auto-create; brand-collision → ADR-017 sub-entity link or
  review (no parent pollution).
- No fabricated endpoints — every source above is marked **verify** until the live URL/shape is
  confirmed by the `internet-ingestion` path.
- Provenance-stamped writes (ADR-018); review-gated where the source is fuzzy.

## Priority

1. **E1 SUSEP entrants + P1 SUSEP products** — insurance is a fully-uncovered premium sector; one
   regulator unlocks both jobs.
2. **E3 SPA betting operators** — a small, high-interest licensed set; the authorized list *is* the
   entrant feed.
3. **E2 PREVIC EFPCs** — completes the two new industries added earlier (previdência fechada).
4. **P2 BCB payment arrangements / P3 consórcio grupos** — Job 2 for the BCB base.
5. **E5 CVM participant registry** — advisory/AAI entrants.
6. **E4 crypto/VASP** — monitor; build when the BCB registry publishes.

Related: `bcb_autorizacoes`, `cvm_ofertas`, ADR-011 (discovery), ADR-019 (source registry/verticals),
ADR-017 (sub-entity auto-link), `receita_cnpj` (entrant enrichment), the Anteater sectorial fork.
