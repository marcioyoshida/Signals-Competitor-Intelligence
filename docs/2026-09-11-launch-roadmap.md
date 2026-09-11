# Launch roadmap — 2026-09-11

Companion to [ADR 024](2026-09-11-adr-launch-readiness.md) (what GA means and why the sequence
is design-partner-first). This is the *dated* plan; `docs/2026-08-16-roadmap.md` remains the
living depth/backlog log.

Durations are working-day estimates for a single builder; they are deliberately conservative
about the parts that depend on **external parties** (AWS SES review, Marketplace review, buyer
calendars), because those, not the code, set the critical path.

## Where we actually are

Live and verified as of today: daily orchestrated pipeline (ingest → synth → beliefs →
detectors → feed), 16 covered industries, 114 entities, 594 feed cards, 8 strategy frameworks,
grounded Q&A + write-capable Agent API + 4 officer personas, `/exec` officer dashboards with
decision capture, ADR-018 curation governance (provenance → precedence → audit → rollback),
three delivery planes with a fail-closed per-tenant read boundary, weekly CSO brief push.
1110 tests green.

**The product is sellable. The company around it is not yet.** Everything below is about the
second half.

---

## M0 — Launch gate (blockers). Target: 2026-09-26

Nothing here is research; it is a fixed list. No paying tenant before all five are done.

| # | Item | Est. | Note |
| - | ---- | ---- | ---- |
| G1 | Freshness SLO + pipeline-failure alarms (#109) | 1–2 d | CloudWatch alarms on Step Functions `ExecutionsFailed` and on `feed.json` age; alarm → the same channels as the weekly brief |
| G2 | Registry PITR + a rehearsed restore (#110) | 0.5 d | PITR on the entities/tenant/journal tables + one written, actually-executed restore drill |
| G3 | SES production access + a real end-to-end brief send (#111) | 1 d + AWS review | Request is the long pole; also verify or park the Teams webhook shape |
| G4 | Trust pack — 4 one-pagers (#112) | 2 d | Reuses what is already true in the repo — this is writing, not building |
| G5 | Published price for Entry / SaaS / Sovereign (#113) | 1 d | Decision, then a page. Blocked on nothing but a decision |

## M1 — Design partners. Target: 2026-10-17

- **P1 (#114) — Onboarding runbook** (2 d): the operator path end-to-end — watchlist → registry
  curation pass → `provision_tenant.py` → Cognito user → first login → first weekly brief.
  Written as a checklist someone else could run.
- **P2 (#116) — Sector readiness pass per partner** (2–3 d each): before a demo, run the ADR-018
  integrity audit + coverage check on *that buyer's* sector and fix what it finds. This is the
  moat; it does not generalize and should not be skipped.
- **P3 — 2–3 SaaS design partners signed** (calendar-bound): banking / fintech / seguros are
  the deepest sectors today and should be pitched first.
- **P4 — Feedback loop** (continuous): every partner question that the agent cannot ground is
  already auto-captured by the ADR-014 coverage-gap loop. Triage that queue weekly — it is the
  highest-signal roadmap input we will ever get, and it is free.

## M2 — Entry self-serve. Target: 2026-11-14 (gated on M1 pricing validation)

- **E1 (#115)** Signup → payment → entitlement → login, with no operator in the loop (wires the parked
  storefront/Stripe work to `put_tenant_config`).
- **E2** Entry-plane conversion surface: a public sample of the entry-tier verticals.
- **E3** Self-serve support path (docs + a single contact channel).

Explicitly **deferred until M1 validates pricing.** The US-entity question (LLC/EIN/banking) is
an E1 blocker only — it does not block M0 or M1.

## M3 — Enterprise. Target: 2026-12+

- **#49** AWS Marketplace in-account packaging (tier-1 delivery plane `marketplace`).
- Sovereign-plane hardening: per-account deploy runbook, telemetry-off verification.

---

## Depth backlog — explicitly NOT launch-gating

These make an already-sellable product better and should run *behind* M0/M1, not ahead of it:

- **Surfacing** (#81–#93): route existing derived state onto officer panels. #92/#93 are
  `blocked` on sources — leave blocked, do not force.
- **Decision loop** (#94–#100): outcome review, auto-drafted decisions, ADR-018 governance over
  decisions, cross-tenant precedent.
- **Ingestion depth** (#14 stages, #101–#107, #63, #26, #19, #104): breadth of entity/product
  discovery. #104 (Receita CNPJ via BigQuery) is unblocked *technically* but needs a GCP
  project + service account from the owner.
- **PROPOSED-only ADRs**: 022 (prudential + FinBERT), 023 (Basel/BCBS). Strong differentiators,
  but they are the *next* product story, not this launch — and per ADR 024 they are roadmap,
  never demo.

## Risks

1. **Single-builder concentration.** Every milestone above is serialized through one person.
   The trust pack and the onboarding runbook are the two items that most reduce this risk, which
   is part of why they are in M0/M1.
2. **External review timelines** (SES, Marketplace) are unknowable from here. G3 should be
   *requested first, on day one of M0*, and the rest of M0 built while it queues.
3. **Corpus drift during selling.** Ingestion sources are third-party and change without
   notice; G1's alarms are what turns that from an embarrassment into a ticket.
4. **Demoing depth we cannot sustain per sector.** P2 exists to prevent exactly this.
