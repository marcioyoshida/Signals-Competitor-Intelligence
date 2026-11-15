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
| Entity continuity | Mergers and renames (HSBC→Bradesco, Citi retail→Itaú) | **Built (#330).** `bcb_entity_history`: IF.data `IfDataCadastro`, 2000-03 onward, `entity_history/cnpj_map.json`. Asset-only deals (Citi retail) need a curated override |

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
