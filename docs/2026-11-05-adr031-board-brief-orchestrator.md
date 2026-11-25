# ADR 031: Board brief orchestrator (multi-officer debate over MCP)

- Status: proposed, 2026-11-05. Epic #321; milestones Board M0–M3 (#322–#344).
- Amended 2026-11-15 (owner review): focal vs subject, competitor proximity, onboarding sets the focal institution.
- Amended 2026-11-14 by ADR 032 (five boards): the Risco board (`risk`) replaces the planned
  Financial officer, and the brief uses the five board names. See *Amendment 2026-11-14* below.
- Scope: Onça. Anteater has the same officer layout (its ADR #27) and can adopt this later.
- Origin: an external "ADR-001: Multi-Agent MCP Orchestrator for Board-Level Decision Making"
  sketch (rating-downgrade case). This ADR **adopts its process** (frozen context, typed
  contracts, challenge rounds, audit, human gate, replay) and **maps it onto what Onça
  already runs**. Like ADR 022, it keeps the domain idea and rejects assumptions the
  deployment cannot honour.
- Related: ADR 020 (`/api/act` and the four officers), ADR 021 (Executive Flow, decision log,
  ETS/TDR, decision→KB), ADR 022 (prudential data), ADR 016 + addendum (Sovereign SKU),
  ADR 025 (agentic API billing), the agent-discovery ADR (2026-09-28), ADR 030 (limit
  budgets), DEC-7 / #100 (cross-tenant precedent).

## Context

### What the sketch assumes

Six specialist agents (Regulatory, Compliance, Product, Strategy, Financial, Enterprise
Risk) already exist as MCP APIs over the bank's **certified internal data**: warehouse,
semantic metrics, risk-appetite store, capital and liquidity positions, funding costs,
deposit betas and cohort economics. An orchestrator only has to sequence them.

### What Onça actually has (checked 2026-11-05)

| Sketch element | Onça today |
|---|---|
| Regulatory, Compliance, Product, Strategy agents | Four officers in `src/dashboard/officers.py` (CRO = **Regulator**, CCO, CPO, CSO), each with an action catalog. They are personas over `agent_ask`, not separate MCP servers. |
| Financial agent | No agent. Public prudential data exists (ADR 022): KM1 Basel/CET1/leverage/LCR/NSFR via DASFN, IF.data, COSIF, NPL, read through `ratio_guards`. Quarterly, published with a lag. |
| Enterprise Risk agent | None. No risk-appetite input exists. |
| Context fabric | feed.json, the entity registry, the Bedrock KB (Pilar 3 prose, decisions), regulatory documents, DOU, CVM filings, reputation stores. **No** funding costs, deposit betas, product cohort economics, risk appetite or rating agency reports. |
| Workflow engine | Step Functions runs the pipeline. **The main stack is at 481/500 resources.** |
| Run store / audit | Append-only curation log, the `/api/act` journal, `DECISION#` items with ADR-018 provenance and rollback. |
| Human gate | `record_decision`, `set_outcome`, `set_board_adoption` intents. |
| Challenge round, schema enforcement | None. |
| Replay / evaluation harness | #339 episodes + #340 scoring and release gate (`src/board/evaluate.py`, `scripts/board_eval.py`). |
| Customer-impact score | External proxies only (complaints, rates, reputation). No customer data. |
| MCP surface | Four read-only tools (`lookup_entity`, `entity_signals`, `regulatory_events`, `ask`). Write tools go to tenant connectors only (agent-discovery ADR). |

Live state of the learning loop: **21 decisions recorded, all `aprovado`, all outcome
`pendente`, one tenant.** There are no outcome labels yet, so nothing can be calibrated on
our own decisions. There are no paying clients, and Bedrock spend is reserved for paying
clients.

### Consequence

Onça is **outside-in**. It can tell a board what peers, regulators, the market and customers
are doing, and where the bank stands on public prudential metrics against peers. It
cannot, on its own data, decide the sketch's question: the capital, liquidity, pricing and
customer response to *our own* downgrade. That needs the bank's internal data.

## Decision

1. **Build the orchestrator on the existing officers, in its own stack.** A Step Functions
   state machine in a new `OncaBoardStack` owns the run lifecycle. Officers stay where they
   are and are called in-process through an adapter; MCP remains the outside interface.
2. **Ship an outside-in "board brief" first, and name it that.** Until a client supplies
   internal data, the artifact is a *brief* (situation, peer and regulatory position,
   options, dissents, monitoring triggers), never a capital or pricing decision. The word
   "decision" is reserved for the human gate.
3. **Risco carries the prudential view now; the client's risk appetite comes in M3.**
   *(Amended 2026-11-14.)* The original plan was a separate Financial officer (`cfo`) over the
   public prudential data (ADR 022), with Enterprise Risk held until a client supplied its
   risk appetite. ADR 032 has since shipped the Risco board (`risk`, URL `#risco`) over that
   same data, plus resolution regimes and insolvency. Risco therefore takes the Financial
   role in the brief, and M3 adds the client's risk appetite to Risco instead of creating a
   sixth officer. `cro` still means Regulatório.
4. **Collect historic public data before claiming evaluation.** The replay harness depends
   on backfills listed in M1. The first evaluation uses a synthetic scenario; historic
   replay follows once the backfills land.
5. **Every run is spend-gated.** A run executes only for a paying tenant or with an explicit
   owner flag, under a per-run token/cost cap (ADR 025 metering, ADR 030 budgets).

## Specification

### Run lifecycle (state machine)

1. **Assemble and freeze the Shared Context Pack.** Built from feed/registry/KM1/IF.data/KB
   for the target entity and its peers. Every fact carries `source`, `as_of` and a citation
   id. Stored as an immutable S3 object keyed by its sha256; the run references it by id.
   Size cap so it fits an officer context window.
2. **Parallel diagnosis.** Each officer receives the common envelope and must answer in the
   response schema below. Non-conforming output gets one re-prompt, then is recorded as
   `invalid` (the run continues with partial results).
3. **Challenge round.** Regulatório and Compliance challenge the Produto and Risco options;
   Risco challenges Estratégia. One reply each.
4. **Quantify.** Risco attaches public-metric deltas against peers (stating the quarter and
   publication lag); Produto attaches rate and complaint evidence; Estratégia ranks options
   by fit.
5. **Synthesize.** One synthesis call produces 2–3 options with an explicit
   customer-impact band, dissents kept verbatim, residual regulatory risk and monitoring
   triggers. Any claim without a citation id from the pack is dropped (same guard family
   as the ADR-010 grounded answers).
6. **Human gate.** The pack is a `propose`. The board secretary accepts, modifies or
   rejects through `record_decision`; adoption through `set_board_adoption`; outcome later
   through `set_outcome`. These feed ETS/TDR and the decision→KB loop unchanged.

Every phase has a time box. A timeout yields partial results plus a visible gap, never a
silent omission.

### Contracts

Envelope (orchestrator → officer):

```json
{"run_id": "uuid", "phase": "diagnosis|challenge|quantify|synthesize",
 "shared_context_id": "sha256", "prior_positions": [], "task": "string",
 "output_schema": "board/v1/position"}
```

Position (officer → orchestrator):

```json
{"officer": "cso|risk|cco|cpo|cro", "diagnosis": "string", "constraints": [],
 "options": [{"id": "opt-1", "description": "", "customer_impact": "low|medium|high",
              "capital_liquidity_effect": "", "red_lines": []}],
 "citations": [{"pack_fact_id": "", "source": "", "as_of": ""}],
 "tool_call_summary": []}
```

### Run store

A dedicated table `OncaBoardRuns` in `OncaBoardStack`: one item per run (pack id, status,
cost, timings) and one per officer response per phase. Append-only; tenant-scoped like the
decision items. Retention follows the privacy policy (90 days for logs) unless the tenant's
contract says otherwise. This is an open question for M3.

### Exposure

- `/api/act` intent `board_brief` (catalog-only, journaled, idempotent).
- MCP tools `board_brief` and `board_run_status`, **tenant connectors only** (OAuth), never
  in a public registry.
- `/exec`: a read-only board pack view.

### Client model (BYOK, #345)

A run's model calls go to the tenant's own model when it has connected one; hosted Nova Lite
stays the default for trials. Stories #346–#353.

- **One switch, tenant paths only.** `bedrock_llm.converse(target=…)` routes a call to the
  tenant's model (`client_model.Target`). Only tenant-request paths resolve a target (Ask, the
  agent API, then `board_brief`); pipeline and synthesis output is shared by every tenant and
  stays hosted.
- **No fallback to hosted.** A failing client model returns a reader-facing code
  (`model_key_rejected`, `model_no_credit`, `model_no_access`, `model_key_needs_workspace`,
  `model_unavailable`, `model_refused`), never a Nova answer in its place. The code is not a
  coverage gap.
- **Guardrail parity.** Outside Bedrock, the same guardrail runs standalone (`ApplyGuardrail`) on
  the question before the call and on the answer after it, and fails closed.
- **Billing.** Client-billed tokens are reported, but they don't draw the included API quota.
  The spend gate (#329) treats a connected model as satisfying it.
- **Key store (#347).** Table `OncaModelKeys` holds one row per tenant. The key is sealed under a
  dedicated KMS key (rotation on). The encryption context is `{onca-tenant, onca-purpose=model-key}`
  and every IAM grant is conditioned on that purpose. The key is verified with a real call before
  it is stored. Only the last 4 characters are shown, and the key is scrubbed from the act journal.
  `connect_model` is never an MCP tool.
- **Who may manage it.** Intents on `/api/me/act`: `model_status` for any member;
  `connect_model`, `test_model` and `disconnect_model` for the tenant's `model_admins` only. That
  list holds verified e-mails and an operator sets it (`set_model_admins`), because connecting
  sends the tenant's questions to a third party. It starts empty, so no one can connect by default.
- **The client's own Bedrock (#350).** A second provider, `bedrock_role`: Claude in Amazon
  Bedrock in the client's AWS account, called with the SDK's `AnthropicBedrockMantle` client
  (Messages API on Bedrock, IAM action `bedrock-mantle:CreateInference`, model
  `anthropic.claude-sonnet-5-5`). The client's role trusts exactly one Onça principal: the
  fixed-name broker role `onca-client-model-broker` (RETAIN; a re-created role gets a new unique
  id and every client trust policy would stop matching). The Lambdas assume the broker, and the
  broker assumes the client role with the tenant's **external id**. Onça generates that id once
  per tenant (`tenant_config.ensure_model_external_id`); it is never taken from a request, which
  is the confused-deputy guard. The broker may assume a role only with an external id. The row
  stores the role ARN, region and external id. None of them is secret, so there is no KMS
  envelope. Assumed credentials are reused in a warm container until shortly before expiry,
  but the row is read on every call, so a disconnect still stops the next call.
  - Codes: `model_role_denied` means the client's trust policy or external id refused. A
    broker failure is Onça's own fault and returns `model_unavailable`.
  - Fallback: Bedrock has no server-side refusal fallback, so a decline there is
    `model_refused`.
  - Client setup: the template is published at `/docs/onca-bedrock-role.yaml`; it grants
    inference only, on the allowed model.
  - Region caveat: in `sa-east-1` Claude on Bedrock is global routing only, so in-country
    processing is not available there today. The drawer says so.
- **Prompt caching (#351).** `converse(context=…)` takes a frozen context pack. On a client
  model the pack is the first block of the final user turn, with a cache breakpoint after it.
  Calls reuse the system + pack prefix, and only the question after it changes. The pack is
  never part of `system`, because it is retrieved data, not operator instructions. On hosted
  Nova the pack is sent as its own block ahead of the question, so the guarded question is never
  matched inside the pack. Ask does not use this. Its system prompt (~300 tokens) is under Sonnet
  5.5's 512-token minimum and the grounding changes per question, so caching would only add the
  write premium. The first user is `board_brief` (#321). `usage_out` and `ask_usage` carry
  `cache_read`/`cache_write`.
- **Measure before connecting (#352).** `config/model_eval_bar.json` fixes the pass bar before
  any run. `scripts/model_eval.py` runs the Ask regression and the guardrail probes on a QA
  tenant connected to the model under test. It refuses a tenant that answers on hosted Nova and
  writes `eval/model-profiles/<model>-<date>.json`. Each `MODELS` entry carries `profile` (None
  until measured), and `model_status` reports `measured`.
- **Client-billed metering (#353).** The agent API records client-billed calls in
  `client_tokens`/`client_calls` (with provider and model) on the usage row. They never add to
  `tokens_used`, so they never draw the quota or overage. The board spend gate is part of #329.
- Status 2026-11-06: #346–#348 and #350 are live. #350's live check needs a pilot's AWS
  account, because Onça's own account is rejected as a client account. #351–#353 are built.

### Evaluation harness

Fixed packs replayed in dry-run mode. Scores: citation validity (every claim resolves to a
pack fact), regulatory completeness against a checklist, dissent preservation, option
coherence, cost and latency. For historic episodes the answer key is what the bank's board
actually did, read from CVM board minutes (M1).

**Historic episodes (#339).** `src/board/episodes.py` + `scripts/build_board_episodes.py` →
`eval/board/episodes/{id}.json` (definitions in `episodes.json`, summary in `index.json`).
Eight episodes, all usable: the S&P (2015-09-09), Fitch (2015-12-16) and Moody's (2016-02-24)
sovereign downgrades and S&P's 2018-01-11 cut (focal Itaú, Bradesco, BB and Santander in
turn), the arrest of BTG's controller (2015-11-25), and the Cruzeiro do Sul RAET (2012-06-04),
the BVA intervention (2012-10-19) and the Rural liquidation (2013-08-02). Banco Santos (2004)
is excluded: the BCB register carries no detail for it and no M1 source but IF.data reaches
back that far. Each trigger was checked against a public source (Agência Brasil, the BCB
resolution-regime register).

Each episode is a **feed frozen at `as_of`** with the shape of `feed.json`, so the real
`build_pack` and proximity score run on it and `scripts/board_dry_run.py --scenario` replays
it. Point in time: IF.data is taken as public 90 days after the quarter end, the complaints
ranking 60 days after the period, prices up to the day before `as_of`, and the REF edition by
its conservative date. `lookahead()` lists any dated item after `as_of`, and an episode
with one is refused. The **answer key** is read after the event: the focal board's minutes in
the next 120 days, each tagged with a decision category from its subject (capital return,
capital raise, funding, strategic, independent review, governance, accounts, risk). The text
behind CVM's CAPTCHA is not read. It also holds the 5- and 30-day abnormal share reaction,
the next quarters' prudential points, rating filings after, and for failures the BCB regime.
The **rubric** per kind (sovereign, idiosyncratic, failure) is a curated topic list, marked
`curated`, for #340 to score against.

For failures, `pre_event_signal` records whether the frozen data already showed distress:
BVA (capital 1.3 pp below the minimum, ROE −34%) and Rural (−2.7 pp, ROE −83%) did in IF.data.
Cruzeiro do Sul's last public quarter looked healthy (its losses were a fraud), but its shares
fell 44% in the 60 days before the RAET, 29 points below the sector, so the signal is
`market` only. BTG's shares fell 26 points below the sector in five days; its board approved a
buyback, management changes and an independent committee within two weeks.

## Historic data (M1)

| Need | Source | Status |
|---|---|---|
| Rating events, material facts | CVM IPE yearly ZIPs (Fato Relevante, Comunicado ao Mercado) | **Built (#331).** 2014 onward in `board_history/cvm_ipe/{year}.json.gz`, financial institutions only (CNPJ root in the #330 map), owner at the meeting date. A foreign parent's relayed SEC forms and a BDR depositary's paperwork are dropped. **Insurers added (#388):** roots the registry files under `insurance` plus SUSEP's company list, tagged `regulator: "susep"`. A root in the dated insurer map (#389) is attributed by date (`attribution: "dated"`); holdings SUSEP does not supervise (BB Seguridade, Caixa Seguridade, Porto Seguro SA, SulAmérica SA) keep today's registry entity (`attribution: "registry"`). 2,520 insurer rows; bank rows unchanged at 13,498. SulAmérica SA's root was added to the registry in #389 (191 rows, to its 2022 merger). Still out: Hapvida and Odontoprev (no registry entity), the listed brokers (Wiz, Qualicorp, Alper; scope undecided), and Aliança da Bahia's 101 rows (no registry entity). The live reader still keeps 45 days |
| What boards decided (answer key) | CVM IPE board minutes (Ata de RCA) | **Built (#331).** "Reunião da Administração" / "Conselho de Administração", Ata and Sumário das Decisões: 5,560 rows 2014–2026, flagged `answer_key`. `events_for()` splits triggers from decisions |
| Agency rating actions | Banks' own CVM filings; S&P, Fitch, Moody's as link-only pointers | **Built (#332).** `board_history/rating_events/events.json`: 61 bank events 2014–2026 parsed from filing subjects (78 with insurers, #388: 17 for IRB Re) (agency, action, the rating and outlook when the subject names them). **Selection bias: 22 upgrades, 0 downgrades.** Banks announce good news as notices and stay silent on bad news, and the large banks file none at all. Filing texts sit behind CVM's CAPTCHA (never bypassed), so 50 events need a person to read the notch. Agency rows are curated pointers in `config/rating_pointers.json` (agency domain only, no text, nothing fetched). The downgrade episodes (2015–16, 2018) need those pointers. The Treasury's sovereign history is under a robots-disallowed path |
| Capital, liquidity, funding history | IF.data quarterly, COSIF monthly, KM1 | **Built (#333).** `board_history/prudential/`: per-period extracts and `series.json.gz` per entity, attributed by date through the #330 map. IF.data Resumo 2000-03 onward (`financeiro` basis before 2014-03, `prudencial` after) and Capital 2015-03 onward. COSIF 1999-01 onward in three file layouts and two charts of accounts (legacy, and Res. 4.966 from 2025-01; provisions are not comparable across the break, so each point records its `chart`). KM1 (LCR, NSFR) 2019 onward from DASFN, each file unrolled to five quarters. Ratio guards run on read (`series_for`). Banks not in the registry (Santos, Cruzeiro do Sul, BVA, Rural) are kept in the extracts and get no series; the #339 episodes read their points straight from the extracts under episode-local ids (`ep-…`, by CNPJ root), with no registry write |
| Price pass-through | BCB `taxaJuros` v2, Selic target (SGS 432) | **Built (#334).** `juros_history`: `board_history/juros/` (`daily/{year}.json.gz`, `monthly.json.gz`, `selic.json.gz`, `series.json.gz`). A new five-business-day window starts every business day (3,719 from 2012-01-02), so a non-overlapping chain of 745 weekly windows is kept: about 770 rows from about 170 lenders each, 29 products (PF/PJ), plus the monthly real-estate series (178 months). Attributed by date through #330. **Pass-through is a matched panel:** each legal entity is compared with itself, each end averaged over four weeks, and the market change is the median over lenders quoting at both ends. Medians of whoever filed measure composition, not pricing: through the 2021–22 hiking cycle they showed Bradesco −30pp and the market −11pp on personal credit while Bradesco's own bank rose from 94.5% to 107.7% (Bradescard quotes about 9%, and 18 more lenders joined the list). Product names are kept as published (no silent merge across renames). 32% of rows (137 lenders: BNB, Sofisa, XP, Portoseg, BASA, Agibank, Omni, many consumer-finance companies) are in the #330 map but have no registry entity; the series lists them with names and rebuilds from the stored windows, so a registry fix reaches every past week |
| Complaints per client | BCB complaints ranking (file service `www3.bcb.gov.br/rdrweb/rest/ext/ranking/arquivo`) | **Built (#335).** `reclamacoes_history`: `board_history/reclamacoes/{period}.json` + `series.json.gz`. Olinda serves the latest quarter only; the file service behind the ranking page has half-years 2014-S2 to 2016-S2 and quarters from 2017-Q1 (204 = not published; 2022-Q2 never was). 43 periods, 5,827 rows. **Three methodologies, recorded per row (`method`), not comparable:** `regulated_2014` (regulated founded complaints per million clients, to 2024-Q1), `transition_2024` (2024-Q2), `sampled_2024` (BCB samples and extrapolates founded complaints, from 2024-Q3). Size bands change too (two million clients → four million → Top 10 → Top 15 with payment institutions). Conglomerate rows have no CNPJ: matched by name against that quarter's IF.data register (#330 snapshots) to the leader's root, then the owner by date (Caixa matches as an institution); unmatched names fall back to the registry (`attribution: "name"`, 631 rows, all unique brands). 57% of rows attributed; misses are mostly automakers' banks and regional banks with no registry entity |
| Bank fees | Olinda `Informes_ListaTarifasPorInstituicaoFinanceira`, `Informes_ListaValoresDeServicoBancario` | **Snapshot started (#335).** `tarifas_history`: Olinda serves only the fees in force today, so history is dated snapshots (`board_history/tarifas/{date}.json.gz`, first 2026-11-16) diffed by `changes()`. `DataVigencia` dates the last change of each current fee (back to 1996), the only past signal. Scope: banks, credit-finance companies, payment institutions, direct-credit companies (597 institutions, 356 with a table); cooperatives out. The group reference's maximum is sometimes the filler 99,999.99, which inflates its average (`placeholder_max`). Monthly runs are not yet scheduled |
| Complaint microdata | consumidor.gov.br monthly files | Blocked on the `GOV_DADOS_TOKEN` (401) |
| Market reaction | B3 COTAHIST yearly files, CVM FCA securities table | **Built (#336).** `cotahist_history`: `board_history/b3/{year}.json.gz` + `series.json.gz`, 2000 onward, spot market standard lot, 56 issuers (banks, financeiras, Cielo, insurer and exchange holdings), 110 tickers, about 200k ticker-days. COTAHIST has no CNPJ: the ISIN's issuer code maps to a CNPJ through CVM FCA (2010 on, 10 s crawl delay) plus `config/b3_issuers.json` for issuers delisted before 2010 (Unibanco, Banespa, Nossa Caixa, BESC, Banestado, BEC, BEA, Sudameris, Real, Panamericano, Cruzeiro do Sul, Daycoval, Sofisa, BIC, BTG units), each root read from the IF.data register. Attributed by date through #330 (Banestado's shares are Itaú's from 2001, Unibanco's are Unibanco's until 2008-12). Prices are per share (`FATCOT`: 1,000-share quotes in the early 2000s) and **unadjusted**; day moves beyond ±40% are flagged `suspect_corporate_action` (149) and excluded from `reaction()`, which compares the entity's most traded ticker with the median of the other entities' over the same window (COTAHIST has no index). Probe: BTG −25% (−22pp vs the sector) around 2015-11-25. Unibanco, BASA, Banese, Alfa, Indusval, Sofisa, BNB and BIC have no registry entity yet (#394) |
| System stress context | BCB Financial Stability Report (REF) PDFs | **In the KB (#337).** `ref_history`: 37 half-yearly editions, 2008-11 to 2026-10 (none earlier at `/content/publicacoes/ref/{YYYYMM}/RELESTAB{YYYYMM}-refP[ub].pdf`). Each edition is cut into its sections by its printed table of contents (the PDFs have no bookmarks; the running header maps printed to PDF pages): 804 documents under `BCB-REF/{edition}/` in the KB bucket. Each document's sidecar names the edition and section (`title` "REF out/2015 (BCB) — 2 Sistema financeiro › 2.6 Testes de estresse de capital", `edition`, `section`, `kind: "system"`, no `entity`), so a 300-token chunk keeps both (the ADR 022 chunk-attribution trap). Stress-test sections, plus their methodology annexes and scenario boxes, carry `stress: true` (every edition has one; since 2021 the capital stress test is the third-level entry 1.2.5, below the usual cut). **Point in time:** `date` is the last day of the cover month, because the release day is not in the PDF and a pack must not cite a report published after its date. `board_history/ref/manifest.json` lists every section with its PDF pages; `stress_at(manifest, as_of)` returns the edition in force and its stress sections. The cover month, not the folder, dates an edition (folder 202510 is November 2025). The 2024-11 contents page extracts without its "Sumário" heading, so a header-less early page that lists entries is taken as the contents page. A contents page that lists nothing before a late section gets fixed page windows for the pages in front (`parse_mode: "partial"`); none does today. The script probes only months after the newest edition, so the next one (about April 2027) is a re-run |
| Entity continuity | Mergers and renames (HSBC→Bradesco, Citi retail→Itaú) | **Built (#330).** `bcb_entity_history`: IF.data `IfDataCadastro`, 2000-03 onward, `entity_history/cnpj_map.json`. Asset-only deals (Citi retail) need a curated override |
| Insurer ownership continuity | SUSEP SES economic groups (`Ses_grupos_economicos`) | **Built (#389).** `susep_entity_history`: 1999-06 onward, quarterly, `entity_history/susep_map.json.gz`, same shape as #330 so `entity_at` works unchanged, plus a `codes` index by SUSEP company code for the SES financial tables (#390). One 0.6 MB member read from the 580 MB `BaseCompleta.zip` by HTTP range requests. 453 companies, 231 with a CNPJ (SES has none; codes join SUSEP's current list, so companies that left the market are code-only). Groups map to registry entities through the curated `config/susep_groups.json` (40 of 122 groups; the alias index resolves none). `OUTROS GRUPOS`, `INDEPENDENTE` and blank are placeholders, never owners: the company is its own group. The BB-Mapfre group is unmapped before its 2018-12 unwind (split control) and maps to BB Seguridade after it, when only BB's companies remain under the code (a dated `from` entry). A partial newest month (late filers) is dropped. 80 named-group moves, e.g. HSBC→Bradesco 2016-09, the BB-Mapfre unwind 2018-12, Liberty→Talanx 2024-03 |
| Insurer financials | SUSEP SES `Ses_seguros`, `Ses_pl_margem`, `Ses_prov` | **Built (#390).** `susep_history`: `board_history/susep/series.json.gz`, per company and quarter (flows summed, stocks at quarter end) and per group (companies summed at each date, ratios recomputed), attributed by date through #389. Earned and written premium, claims, commercial expense, equity, adjusted equity, minimum required capital, provisions; loss, commission and solvency (adjusted equity / CMR) ratios. Premiums and claims 1995 onward, solvency 2016 onward (CMR), provisions 1999 onward. **Break 2013-12:** the FIP form moved claims from `sinistro_retido` to `sinistro_ocorrido` and took VGBL out of written premium; no row fills both claim columns, so claims sum both and each point records `basis` (`fip_pre2014`, `fip_2014`, `mixed` for 2013-Q4). Written premium is not comparable across the break; earned premium is. 22,180 company-quarters, 400 companies, 109 groups. Guards on read blank ratios under R$1 mi earned premium or with fewer than three months. Not covered: reinsurers' premiums (IRB Re has solvency only; reinsurance flows are in `SES_VALORESRESMOVGRUPOS`), health plans (ANS, not SUSEP), capitalização and pension flows (`Ses_Dados_Cap`, `Ses_Contrib_Benef`) |

**Scoring and release gate (#340).** `src/board/evaluate.py` scores one run against the pack
it ran on: the pack is rebuilt from its scenario file and must carry the run's sha256.
- *Citation validity:* every citation in every officer position and in the synthesis is a pack
  fact (bar 100%). Every figure the synthesis states (with a decimal or a unit) appears in the
  pack at the precision written (bar 80%). A number the model computed, such as a 24% cut
  from 5.8% to 4.4%, is listed as ungrounded for the reader, so the bar leaves room for that.
- *Regulatory completeness:* a fixed six-item checklist (capital, liquidity, credit, conduct,
  disclosure, supervisor), at least 4 of 6, plus the episode's curated rubric.
- *Dissent preservation:* each validated challenge reply is a dissent, equal to the reply and
  printed verbatim, red lines included, in the rendered board pack (bar 100%).
- *Option coherence:* 2–3 options, each traced to an officer's own option, distinct (word
  Jaccard ≤ 0.6), with at least one trigger. For episodes, the share of the board's real
  non-routine decision categories that some option covers, and whether a visible pre-event
  signal is named.
- *Cost and latency:* tokens ≤ 400k, ≤ 900 s, no call errors.

The rubric, answer-key and signal checks are **soft** (warnings): eight episodes cannot
calibrate them. The thresholds live in `config/board_eval_gate.json` (v1, fixed 2026-11-25
against the four synthetic dry runs of 2026-11-15 before any episode run). Changing one is a
reviewed commit with a `changes` line. `scripts/board_eval.py` writes
`eval/board/scores/<run_id>.json` and `summary.json`. `--record` stores each score in
OncaBoardRuns under tenant `_eval`: a run item plus `RUN#<id>#EVAL#v<gate>`, written once,
kept 365 days (public and synthetic data only). `--gate` exits 1 unless the latest run of
every synthetic scenario and every usable episode passes; that is the release gate for any
change to the runner, contracts or prompts. `scripts/board_dry_run.py --episode <id>` runs an
episode and prints its gate verdict.

First scores: the latest synthetic runs pass. Two older runs fail for real reasons. The 04:45
downgrade run predates option tracing (`derived_from`). The 07:30 competitor run's pack
differs from today's scenario, so its citations cannot be checked against the pack it read.
**Episode run (2026-11-25, owner-approved, Sonnet through the claude CLI on the subscription;
no Bedrock).** All 8 episodes pass and the **release gate v1 passes**.
- *Every episode:* 100% of citations valid, 94–100% of figures in the pack, 5–6/6
  regulatory items, dissents verbatim. Each run took 13–16 calls, 182k–388k tokens and
  220–590 s.
- *Failures:* BVA and Rural cover 4/4 of the rubric and name the prudential signal. Cruzeiro
  do Sul names the market signal but covers 1/4: its IF.data looked healthy because the
  losses were a fraud.
- *BTG:* 4/4 rubric, and its options cover all 4 decision categories the board actually took.
- *Sovereign episodes:* the options match 1–2 of the 2–3 decision categories. Three of the
  four briefs never say that a sovereign downgrade usually drags the bank's own rating (the
  sovereign ceiling). That is the main content finding, left to a prompt change.
- *Re-run:* the first S&P 2015 run ended partial on three provider "529 Overloaded" errors,
  and the gate failed it as designed. A re-run passed. The scorer always uses the latest run
  per scenario.

Out of scope: sovereign CDS, bank bond spreads and other paid market data (Bloomberg,
LSEG, ANBIMA curves). They are a stated blind spot of the brief.

The realistic historic test set is **5–8 episodes**: the 2015–16 and 2018 sovereign
downgrades and the bank downgrades that followed, plus the failures of Banco Santos,
Cruzeiro do Sul, BVA and Rural. That is enough to exercise the harness, not to calibrate a
customer-impact model statistically.

## Phasing (milestones)

- **Board M0 — Orchestrator (outside-in brief).** Stack, pack, contracts, Risco's brief
  role, phase runner, board pack, exposure, spend gate, synthetic dry run.
- **Board M1 — Historic data.** The backfills above, starting with the dated entity map.
- **Board M2 — Evaluation.** Synthetic scenario first, then historic replay with answer keys.
- **Board M3 — Client inside-out (gated on a pilot bank).** Internal pack upload (Sovereign
  SKU), the client's risk appetite in Risco, customer-impact model co-owned with the client,
  retention policy.

## Amendment 2026-11-14 (ADR 032)

The officers in a board run are the five /exec boards, by internal id:

| Board | Id | Role in the brief |
|---|---|---|
| Estratégia | `cso` | Diagnosis; ranks options by fit. Challenged by Risco. |
| Risco | `risk` | Capital, liquidity, NPL, fragility and regimes from public data (was the planned Financial officer, #325). Quantifies against peers. Challenges Estratégia. |
| Compliance | `cco` | Conduct and sanctions. Challenges Produto and Risco. |
| Produto | `cpo` | Rate and complaint evidence. |
| Regulatório | `cro` | Rules, deadlines, standards. Challenges Produto and Risco. |

#325 is closed as superseded. #342 now means adding the client's risk-appetite input to Risco.

## Amendment 2026-11-15 (owner review): whose board, and how far away is each competitor

Three assumptions were implicit in M0 and are now explicit.

1. **The brief serves the client's own board.** M0 had one `target`, so the dry run (#338)
   made the fictional Banco Aurora both the bank the facts were about and the bank whose
   board decides. A brief now has two roles:
   - **focal**: the client's own institution, whose board decides. One per tenant
     (`focal_entity` on the tenant config). It is never taken from a request.
   - **subject**: the entity or event prompting the question. It defaults to the focal
     institution ("we were downgraded") and may be a competitor ("Itaú cut card rates").
   The focal institution is set by the **customer onboarding process**: mostly automated,
   it runs once the customer has accepted the terms and approved the contract. That process
   is still to be designed (own story). A tenant without a focal institution cannot start a
   brief.
2. **Outside-in stays the boundary.** Officers infer only from public data about the focal
   institution, its competitors and the market. Internal data remains M3 (#341).
3. **Decisions revolve around competitor signals, weighted by distance.** M0 picked peers
   as "the largest in the same industry" (size only, measured from the target) and gave
   every peer fact the same weight. Instead:
   - **Proximity.** Code, not the model, computes a score from the focal institution to
     each competitor over dimensions the feed already carries:
     - size (assets, loan book, market share);
     - market overlap (industries, Product Radar products);
     - business model (lending mix, retail deposits vs wholesale funding);
     - prudential profile (Basileia, leverage, LCR and NPL bands);
     - competitive pressure (threat score and momentum).
     Weights live in a versioned config, so evaluation (#339/#340) can tune them.
   - **Pack.** Peers are the focal institution's nearest neighbours by proximity, plus the
     subject when it is a competitor. Each peer has an `f-proximity-<peer>` fact with its
     per-dimension components and cited inputs.
   - **Contracts and synthesis.** Every position is framed as "what this means for the
     focal institution". A competitor signal an option relies on carries its proximity;
     a distant competitor's move may be cited, with its distance stated.
   - **Board pack.** A competitor table (proximity and components). Dissents may contest
     a distance.

## Consequences

Positive:
- Reuses the officers, the act journal, decision governance and ETS/TDR; the new code is
  the orchestration, not plumbing.
- Every claim in a board pack traces to a frozen, hashed fact.
- The backfills also deepen the existing product (peer history, rate trends, complaint
  trends), independent of the orchestrator.

Negative and risks:
- One run is roughly 15–25 large-context model calls. Without a paying tenant it is pure
  cost; the spend gate is mandatory.
- Public data lags (quarterly, 30–90 days). A brief on a live downgrade reflects the last
  published quarter, and must say so.
- Registry duplicate ids (ADR 022 trap) can still mis-attribute peer facts, even with
  entity-filtered KB retrieval (#309); the pack builder must key by the dated entity map.
- Schema gaming: officers may fill fields without substance. The citation-validity score
  and the drop-uncited rule are the guard.
- Historic replay is small-sample. Results are a sanity check, not a calibration.

## Open questions

- Customer-impact scoring model (owned with the pilot client's Product and Compliance).
- Whether the challenge round is human-moderated in the first client release.
- Retention and access policy for board runs beyond the default 90 days.
