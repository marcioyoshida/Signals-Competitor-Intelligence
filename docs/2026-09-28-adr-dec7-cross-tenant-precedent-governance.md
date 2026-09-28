# ADR — DEC-7: cross-tenant anonymized precedent: consent and governance (#100)

- **Status:** Proposed (2026-09-28). Nothing is built. This ADR is the prerequisite #100 names:
  "consent/governance design first". Every decision below is the owner's to accept.
- **Extends:** ADR-021 §F (the decision corpus: "tenant experience is isolated by default ... with
  cross-tenant learning only on explicit, anonymised opt-in"), DEC-1..DEC-6 (#94–#99, closed),
  ADR 015/016 (telemetry is the axis: Portal on, Marketplace/Sovereign **off**), ADR-018 (governance).

## 1. The measured starting point

Live `OncaDecisionLog` (registry table, `DECISION#` items), 2026-09-28:

| | count |
|---|---|
| decisions | 21 |
| attributed to a customer tenant | **0** (all written by `operator`, `tenant` unset) |
| with an observed outcome | **0** (all `pendente`) |

There is no corpus to anonymize. Any safe aggregation threshold (§4) is unreachable today, and
it stays that way until design partners record decisions and review their outcomes (DEC-1). **DEC-7
is therefore gated on data, not effort.** Building the pool now would ship an empty, untested
privacy surface.

## 2. What a "precedent" is, and what may cross a tenant boundary

A precedent is a closed decision: `{trigger, industry, officer, recommendation, verdict,
rationale, references, outcome, outcome_note, timing}`. Most of it is tenant-confidential:

| Field | Crosses the boundary? | Why |
|---|---|---|
| tenant, actor, device, context/evidence ids | **Never** | identifies the tenant or a person |
| free text: `rationale`, `outcome_note`, `recommendation` text | **Never verbatim** | re-identification and strategy leakage; the cheapest leak |
| trigger **kind** (e.g. `reg_change`, `outage`, `pricing`) | Yes, as a category | public-signal class |
| **public** trigger entity (the competitor/regulator in the signal) | Yes | it is public record already in the shared corpus |
| industry, officer | Yes | coarse |
| verdict (`approve`/`reject`/`defer`), outcome class (`bom`/`neutro`/`ruim`) | Yes, **only aggregated** | the actual learning signal |
| timing | Only bucketed (week), only aggregated | exact times fingerprint a tenant |

So a shared precedent is never one tenant's decision. It is an **aggregate pattern**:
"for `reg_change` triggers in `banking` seen by the CRO, `approve` → outcome `bom` in 7 of 9
decisions, across ≥5 tenants". No text, no single decision, no tenant.

## 3. Consent model (proposed)

1. **Off by default, per tenant.** A tenant contributes only after a signed contract clause (the
   DPA annex) **and** an in-product switch set by a tenant admin. Either one missing means no
   contribution. The switch is journaled (ADR-018 provenance: who, when).
2. **Contribute and consume are separate switches.** Consuming the pool never requires
   contributing. A tenant that won't share still benefits once the pool exists, which is what makes
   early opt-ins cheap to ask for.
3. **Revocable, prospectively and retroactively.** Revocation removes the tenant's contributions
   from the next pool build. Aggregates are rebuilt from source, never updated incrementally, so
   withdrawal is real and not a promise.
4. **Sovereign (in-account) tenants never phone home.** ADR 015 makes non-observation the paid-for
   value. They may *consume* a signed, versioned precedent pack shipped with their updates. They may
   *contribute* only through an **explicit export**: a file their admin generates in their account,
   reviews and sends. There is never an automatic channel.
5. **LGPD basis:** legitimate interest is weak for a competitor-intelligence pool. Use **contract +
   consent**, name Onça as controller of the aggregate, and publish the aggregation rules (§4) in
   the privacy page as a buyer-facing commitment.

## 4. Anonymization rules (the pool builder's invariants)

- **k-tenant threshold:** a pattern is published only when **≥5 distinct contributing tenants**
  back it (k=5; tenants, not decisions: one big tenant can't dominate). Below k, the cell is
  suppressed, not rounded.
- **Dominance rule:** no single tenant contributes more than 50% of a published cell's decisions.
- **Coarsening before counting:** week buckets, category triggers, outcome classes. There are no
  exact counts under 10: publish "5–9".
- **No free text, ever,** including LLM paraphrases of rationales. A paraphrase is still a leak.
- **Rebuild from scratch** on every build; it is **deterministic** and **diffable**, and each build
  has a manifest listing contributing tenants, held privately and audited.
- **Queries can't be composed into a difference attack:** consumers get whole published cells only,
  with no filtering API over the pool. Two cells that differ by one tenant would reveal that tenant.

## 5. Where it would live (when the gate opens)

- A separate `OncaPrecedentPool` store, written only by an offline builder Lambda that reads
  opted-in tenants' closed decisions. It is never written from the request path.
- Retrieval: DEC-4 ranking (#97) gains a **second, clearly labelled source**: "padrão de mercado
  (n≥5 instituições)", always below the tenant's own precedents and cited as a pattern, not a case.
- Governance: the builder is an ADR-018 writer (provenance + journal). The kill switch is an SSM
  flag that empties consumption instantly.

## 6. Activation gate (proposed)

Build DEC-7 only when **all** of these hold:
1. ≥**5 tenants** have opted in to contribute (§3.1).
2. Those tenants hold ≥**50 decisions with a reviewed outcome** (DEC-1), across ≥3 industries.
3. The owner has accepted this ADR, and the DPA annex has been reviewed by counsel.

Until then #100 stays open as **deferred-on-data**, and this ADR is the reference for the consent
clause in design-partner contracts. Asking early is what makes (1) reachable.

## 7. Decisions for the owner

- (a) Accept the "aggregate pattern only, never a decision" definition (§2)?
- (b) k=5 tenants and the 50% dominance rule (§4)?
- (c) Sovereign: consume via a signed pack, contribute only via a reviewed export (§3.4)?
- (d) Add the contribute/consume clause to the design-partner contract template now?

## 8. Rejected alternatives

- **Share individual anonymized decisions** (redacted text). Rejected: free-text redaction is not
  anonymization in a market with ~30 relevant institutions. The rationale alone identifies the bank.
- **Opt-out instead of opt-in.** Rejected: it contradicts ADR-021 §F and would poison buyer trust in a
  regulated segment.
- **Differential privacy noise on counts.** Deferred: at a handful of tenants the noise needed swamps
  the signal. Revisit when k-cells are routinely ≥20.
