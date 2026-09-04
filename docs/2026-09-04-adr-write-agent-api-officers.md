# ADR 020 — Write-capable Agent API + four specialist officer agents

- Status: **PROPOSED** — 2026-09-04. Owner-requested (issue #20). Extends
  [ADR 010](2026-08-25-adr-agent-chat-ui.md) — which shipped the **read-only** half of the
  Agent API (`/api/ask/`, grounded/cited Q&A) — with (a) a **write-capable** surface that
  *acts on the system* and (b) a **persona layer of four specialist "officer" agents**.
- Builds on: Cognito identity + per-tenant/tier read boundary (ADR 002 Phase D — the JWT
  authorizer + `onca-tenant-config`), provenance + append-only
  curation log + rollback ([ADR 018](2026-08-30-adr-curation-provenance-integrity.md)),
  the review-gated proposal system + coverage-gap loop
  ([ADR 014](2026-08-25-adr-coverage-gap-loop.md)), the strategy/belief store
  ([ADR 004](2026-08-22-adr-competitive-thesis-swot.md) /
  [ADR 006](2026-08-23-adr-strategy-frameworks-beyond-swot.md)), regulatory change
  intelligence ([ADR 009](2026-08-25-adr-regulatory-change-intelligence.md)), the
  `/api/*` = Lambda-URL + CloudFront-behavior pattern, and the Step Functions pipeline.

## Context

ADR-010 delivered the **read/analyze** half of an Agent API: `OncaAgent` answers natural-
language questions grounded in the tool's own data, cited, scope-gated. Issue #20's
remaining scope is the **write-capable** half — an agent surface that *acts on the system*:
trigger a pipeline run, mutate the entity registry, accept/reject a review proposal, set a
watch, roll back a bad write. Writing changes the risk profile entirely — authorization,
audit, blast radius, reversibility — which is exactly why ADR-010 left it out.

Two things make this tractable now, and one thing the owner wants added:

1. **The safe write primitives already exist.** We are not inventing mutation paths — we are
   putting authorization + audit in front of the ones we already trust: `curation_admin.py`
   (history / rollback / `revert_entity_since` / audit), `entity_registry.propose_review`,
   the Step Functions `StartExecution` (pipeline trigger), the review-proposal queue, and the
   coverage-gap loop (which already remediates under human review, `AUTO_CODEGEN=off`).
2. **The governance rails already exist.** ADR-018 gives per-field provenance, an append-only
   `OncaCurationLog`, write-precedence, and rollback; ADR-002 gives a verified identity +
   tier boundary. A write API rides these.
3. **The owner wants the agent surface organized as four specialist *officer* agents**, each
   a domain persona with its own grounded lens and a bounded action set — a **Strategic
   Officer**, a **Regulator Officer**, a **Compliance Officer**, and a **Product Officer**.
   This makes the product legible to a buyer ("my regulatory officer flagged a rule change
   with market-wide blast radius") and maps cleanly onto the axes we already compute.

## Decision

Add a **write-capable Agent API** as a thin, authorized, audited orchestration layer over the
existing safe primitives, and a **persona layer of four officer agents** on top of it. Reads
stay `/api/ask`; writes go through a typed, allow-listed action catalog — an officer can only
emit catalog actions, so an arbitrary-mutation surface never exists by construction.

### 1. Trust boundary & the write contract

- **Authorization.** Reuse the Cognito JWT + tenant/tier boundary (ADR-002 Phase D). Reads
  stay tenant-scoped; **writes require an elevated capability** — an `operator`/`sovereign`
  tier claim or a per-action grant — and are **fail-closed** (no identity ⇒ no write).
- **Audit.** Every write is journaled to the append-only `OncaCurationLog` (ADR-018) with
  `actor = {officer, identity, request_id}`, the before/after, and the outcome
  (`applied` | `proposed` | `blocked`). One **idempotency key** per request.
- **Two execution classes** (the propose-vs-apply classifier):
  - **Auto-apply** — ONLY reversible, idempotent, low-blast actions (trigger a run, set a
    watch flag, roll a field back over the journal).
  - **Review-gated (propose)** — everything high-stakes (registry industry/parent change,
    bulk mutation, any outward-facing send) is written as a **proposal** to the existing
    review queue; a human or an elevated tier promotes it. This mirrors the discipline
    already in place (discovery proposals, SWOT vetting, coverage-gap `AUTO_CODEGEN=off`).
- **No fabrication.** Write arguments are grounded and validated exactly like read answers;
  an action asserting a fact must cite it. The write path inherits ADR-010's grounding
  contract.

### 2. API shape

- Keep `POST /api/ask` (read, ADR-010). Add **`POST /api/act`** —
  `{officer, intent, args, idempotency_key}` → validate → authorize (tier/scope) →
  (auto-apply | propose) → journal → typed result. Optionally per-officer routes
  `/api/officer/{role}` that fuse a specialized Ask (the officer's read persona) with that
  officer's action catalog.
- Each officer **plans** with a bounded LLM call but can only **emit catalog actions** (a
  typed allowlist). Free-form mutation is impossible; the model chooses among pre-approved,
  parameter-validated actions.

### 3. The four specialist officer agents

Each officer = a **domain lens** (its grounding sources) + a **bounded action catalog**.
These are **runtime product agents acting *within* Onça** — distinct from the dev-time Claude
Code subagents in `.claude/agents/` (`product-strategy`, `data-integrity`, …) that help
*build* Onça, though the personas deliberately rhyme.

- **Strategic Officer (CSO)** — competitive strategy.
  - *Grounds on:* the ameaça×expansão position map, SWOT/TOWS/Porter and the 8 frameworks
    (ADR-006), threat/momentum, `feed.groups`.
  - *Reads:* "where is competitor X gaining?", a war-room brief, thesis synthesis.
  - *Actions:* propose/curate a belief bullet (review-gated → ADR-004 vetting), request a
    pipeline run, tag a competitor thesis, open a strategic watch on an entity/segment.

- **Regulator Officer (CRO)** — the regulatory axis (ADR-009).
  - *Grounds on:* the Phase-A change list, the §2 section diff, the §3 change-record
    (blast-radius / difficulty), deadlines, the `regdocs/` store.
  - *Reads:* "what changed this week and who's hit?", a compliance-deadline calendar.
  - *Actions:* set a watch on an instrument, request a regdoc fetch/diff or a change-record
    draft, acknowledge/route a change, propose a monitoring rule for a regulator/segment.

- **Compliance Officer (CCO)** — integrity/governance + risk lenses.
  - *Grounds on:* integrity findings (ADR-018), sanctions (CEIS/CNEP, #60), antitrust
    (CADE, #61), corporate distress (ADR-012), the curation log.
  - *Reads:* the integrity audit, "any sanction / antitrust / distress hit on the roster?".
  - *Actions:* accept/reject an integrity remediation, **roll back a bad write**
    (`revert_entity_since` / `rollback_field`), flag a sanction/distress hit for review, run
    the integrity audit.

- **Product Officer (CPO)** — product/market + coverage.
  - *Grounds on:* the coverage-gap queue (ADR-014), the CVM/BCB coverage map (#2), discovery
    proposals + radar-score (#14), product-strategy JTBD.
  - *Reads:* "where are our blind spots?", a coverage / market-fit assessment.
  - *Actions:* triage/open a coverage-gap ticket, approve/reject a discovery proposal,
    prioritize a source/detector, propose a new vertical (ADR-019).

### 4. Orchestration (later phase)

A thin **chief-of-staff router** classifies an incoming intent (reuse ADR-010's scope-gate +
a cheap router model) and dispatches to the right officer; officers may **hand off** (the
Regulator Officer routes a market-wide blast-radius change to the Compliance Officer). Every
dispatch and hand-off is journaled.

## Guardrails (unchanged product discipline)

- Fail-closed authorization; a per-tier action allowlist; writes require an elevated
  capability.
- Auto-apply ONLY when reversible + idempotent + low-blast; everything else is proposed.
  Every action journaled to `OncaCurationLog` (actor, before/after, outcome).
- No fabrication; grounded/cited like reads. High-stakes actions (registry mutation, run
  trigger, external send) are **never silent** — proposed, or elevated-tier + audited.
- Cost/rate bounds on Bedrock planning and on run triggers; idempotency keys prevent
  double-application.
- Officers can only emit **catalog** actions — the mutation surface is a fixed allowlist,
  not open text.

## Consequences

- A new write risk surface — **contained** by authorization + audit + propose-vs-apply and by
  **reusing the existing safe primitives** (no new mutation paths, no new trust decisions).
- The persona layer makes the tool legible to specialized, senior, regulated-industry buyers
  and maps 1:1 onto the axes/ADRs we already ship.
- More Bedrock cost (per-officer planning) — bounded; the read path (ADR-010) is unchanged.

## Status / phasing

PROPOSED. Implementation order when picked up:

1. **`/api/act` framework** — the tier authz gate + `OncaCurationLog` audit + idempotency +
   the propose-vs-apply classifier, exposing the existing safe primitives (trigger run,
   accept/reject proposal, rollback). **No officers yet** — prove the write contract first.
2. **The four officer personas** — each a specialized read (an Ask persona over its lens) +
   its bounded action catalog, review-gated.
3. **Auto-apply safe classes + the chief-of-staff router + inter-officer hand-off.**

Related: issue #20; supersedes nothing (extends ADR-010). Depends on ADR-018 (audit/rollback)
and ADR-002 (identity/tier).
