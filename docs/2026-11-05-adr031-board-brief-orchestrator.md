# ADR 031: Board brief orchestrator (multi-officer debate over MCP)

- Status: proposed, 2026-11-05. Epic #321; milestones Board M0–M3 (#322–#344).
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
| Replay / evaluation harness | None. |
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
3. **Add a Financial officer now** (public prudential data, ADR 022). **Add Enterprise Risk
   only with a client's risk appetite** (Phase M3). The new risk role must not reuse the
   `cro` alias, which already means Regulator in URLs and deep links; its short alias is
   `risco`.
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
3. **Challenge round.** Regulator and Compliance challenge Product and Financial options;
   Strategy is challenged by Compliance (by Enterprise Risk once it exists). One reply each.
4. **Quantify.** Financial attaches public-metric deltas against peers; Product attaches
   rate and complaint evidence; Strategy ranks options by fit.
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
{"officer": "cso|cro|cco|cpo|cfo|risco", "diagnosis": "string", "constraints": [],
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
- Status 2026-11-06: #346 is live but dark. #347 adds the store, the intents and the SDK in the
  Lambda asset. The account-page UI is #348.

### Evaluation harness

Fixed packs replayed in dry-run mode. Scores: citation validity (every claim resolves to a
pack fact), regulatory completeness against a checklist, dissent preservation, option
coherence, cost and latency. For historic episodes the answer key is what the bank's board
actually did, read from CVM board minutes (M1).

## Historic data (M1)

| Need | Source | Status |
|---|---|---|
| Rating events, material facts | CVM IPE yearly ZIPs (Fato Relevante, Comunicado ao Mercado) | `cvm_ipe.py` reads 45 days; backfill from 2014 |
| What boards decided (answer key) | CVM IPE board minutes (Ata de RCA) | Same dataset, category not read yet |
| Agency rating actions | S&P, Fitch, Moody's sites | Link-only pointers (date, notch, outlook). Terms restrict text reuse. |
| Capital, liquidity, funding history | IF.data quarterly, COSIF monthly, KM1 | Latest only; backfill with `as_of` |
| Price pass-through | BCB `taxaJuros` v2 | Daily series goes back to 2012-01-02 (probed 2026-11-05); backfill |
| Complaints per client | BCB complaints ranking | Olinda serves 2026 only (probed 2026-11-05); older quarters need the BCB site files |
| Bank fees | BCB fee tables | Not ingested |
| Complaint microdata | consumidor.gov.br monthly files | Blocked on the `GOV_DADOS_TOKEN` (401) |
| Market reaction | B3 COTAHIST yearly files | Not ingested |
| System stress context | BCB Financial Stability Report (REF) PDFs | Not ingested (KB) |
| Entity continuity | Mergers and renames (HSBC→Bradesco, Citi retail→Itaú) | Prerequisite: a dated CNPJ↔entity map |

Out of scope: sovereign CDS, bank bond spreads and other paid market data (Bloomberg,
LSEG, ANBIMA curves). They are a stated blind spot of the brief.

The realistic historic test set is **5–8 episodes**: the 2015–16 and 2018 sovereign
downgrades and the bank downgrades that followed, plus the failures of Banco Santos,
Cruzeiro do Sul, BVA and Rural. That is enough to exercise the harness, not to calibrate a
customer-impact model statistically.

## Phasing (milestones)

- **Board M0 — Orchestrator (outside-in brief).** Stack, pack, contracts, Financial
  officer, phase runner, board pack, exposure, spend gate, synthetic dry run.
- **Board M1 — Historic data.** The backfills above, starting with the dated entity map.
- **Board M2 — Evaluation.** Synthetic scenario first, then historic replay with answer keys.
- **Board M3 — Client inside-out (gated on a pilot bank).** Internal pack upload (Sovereign
  SKU), Enterprise Risk officer, customer-impact model co-owned with the client, retention
  policy.

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
