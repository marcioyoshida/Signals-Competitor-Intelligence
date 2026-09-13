# ADR 024 — Launch readiness: what "GA" means for Onça, and the gate that defines it

- Status: **PROPOSED** — 2026-09-11. Written from a product review of the whole build to date,
  not from a single feature request.
- Relates to [ADR 002](2026-08-18-adr-commercial-multitenancy.md) (registry as a commercial
  product), [ADR 015/016](2026-08-30-adr-distribution-three-tier.md) (Entry / SaaS / Sovereign
  planes), [ADR 018](2026-08-30-adr-curation-provenance-integrity.md) (curation governance),
  [ADR 021](2026-09-04-adr-executive-flow-officer-dashboards.md) (the officer surface a buyer
  actually logs into).

## Context

The **analytical product is far ahead of the commercial product.** What exists and is live:
a daily orchestrated pipeline, ~16 covered industries, 114 entities, 594 feed cards, eight
strategy frameworks, a grounded Q&A agent, a write-capable Agent API with four officer
personas, per-officer sectorial dashboards with decision capture, a governed registry with
provenance/precedence/audit/rollback, three delivery planes with a fail-closed per-tenant read
boundary, and push delivery of a weekly CSO brief.

What does **not** exist, and is what actually stands between this and revenue:

| Gap | Evidence in repo / account |
| --- | --- |
| No self-serve path from "interested" to "entitled tenant" | `scripts/provision_tenant.py` is an operator CLI; Cognito users are hand-created (`infra/app.py`); Stripe lives in a separate storefront project, still TEST mode, provisioning stubbed |
| Push delivery can't reach a real customer | `src/dashboard/weekly_digest.py`: SES account is in **sandbox** (recipient must be verified); the Teams payload shape is documented as **unverified** |
| A silently failed pipeline is indistinguishable from a quiet market | `infra/app.py` has a CloudWatch **dashboard** but **zero alarms**; no freshness SLO on `feed.json` |
| The commercial asset has no backup | no `point_in_time_recovery` on the entities/registry table, whose own ADR calls it "the single source of truth" |
| Nothing to answer a regulated buyer's first three questions | no LGPD/data-processing note, no source-licensing statement, no security overview |
| Prices are undecided | ADR 015/016 define *planes*, never *price points* |
| Marketplace listing | issue #49, open |

None of these are research problems. They are a fixed, enumerable amount of work — which is
exactly why they should be written down as a gate rather than discovered one customer call at
a time.

## Decision

**1. GA is defined per *plane*, not for the product as a whole.** The three planes have
genuinely different readiness bars, and conflating them has been delaying all three:

- **Entry (shared portal, entry-tier verticals)** — the *self-serve* plane. Its gate is
  commercial: signup → payment → entitlement → login, with no operator in the loop.
- **SaaS (per-tenant scoped feed)** — the *design-partner* plane. Its gate is operational:
  freshness SLO + alarms + backup + a trust pack + a named onboarding runbook. Operator-led
  provisioning is acceptable here and is **not** a GA blocker.
- **Sovereign / Marketplace (in-account)** — the *enterprise* plane. Its gate is packaging
  (#49) and is explicitly **last**; it should not be worked before a SaaS design partner is live.

**2. Launch sequence is Design-Partner-first, not self-serve-first.** Two or three paying (or
formally committed) SaaS design partners come before the Entry self-serve funnel, because the
corpus/curation quality — the actual moat — is only provable against a named buyer's sector,
and because Entry's funnel work is wasted if the value proposition is still being tuned.

**3. Five things are declared hard GA blockers for *any* paying tenant**, regardless of plane:

1. **Freshness SLO + alarm.** `feed.json` must be alarmed on staleness and the pipeline on
   failure. An intelligence product that quietly stops is worse than one that is honestly down.
2. **Registry backup (PITR) + a restore rehearsal.** ADR 018 gave us rollback of a *field*; it
   does not survive loss of the *table*.
3. **Deliverable push.** SES production access, so the weekly brief reaches a customer inbox.
4. **Trust pack.** One page each: what we ingest and under what licence, what we store, LGPD
   posture, auth/isolation model. Regulated FS buyers ask on the first call.
5. **Published price.** A tier the buyer cannot price is a tier they cannot buy.

**4. Honest-scope rule for the launch narrative.** We sell what is live and cited. Coverage
gaps (`industry_coverage_gaps`, currently `closed-pension`), `blocked`/`source-needed` issues
(#93, #92, #101, #104), and PROPOSED-only ADRs (022 prudential/FinBERT, 023 Basel/BCBS) are
**roadmap, never demo**. This is the same anti-fabrication discipline that already governs the
narratives, applied to the sales surface.

## Consequences

- The backlog splits cleanly into **launch-gating** and **depth**. Most open issues (SURF-*,
  DEC-*, #14 discovery stages) are *depth*: they make an already-sellable product better. They
  should not be worked ahead of the five blockers.
- Entry self-serve gets deliberately deferred, which means the storefront/Stripe work stays
  parked until a design partner validates pricing. The user's missing US entity therefore stops
  being a launch blocker — it is only an Entry-plane blocker.
- We accept operator-led onboarding for SaaS. That caps early tenant count (a feature at this
  stage: each one is a design partner, not a ticket).
- Saying "GA per plane" means we can honestly say **the SaaS plane is GA** while Entry is not,
  instead of a vague "beta" that undersells a genuinely deep product.

## Alternatives considered

- **Self-serve Entry first.** Rejected: cheapest tier, hardest funnel, and it would validate
  pricing against the buyer segment with the least budget and the least need for a moat.
- **Marketplace first** (#49). Rejected as sequencing: AWS Marketplace amplifies an existing
  motion; it does not create one, and listing review is slow to iterate against.
- **Declare GA now.** Rejected: the five blockers are each a plausible first-week incident,
  and the first incident with a regulated buyer is disproportionately expensive.
- **Keep building depth until the product "feels" ready.** Rejected — that is the failure mode
  this ADR exists to name. The analytical surface has outrun the commercial one for weeks.

## Coverage-gated GA sector list (2026-09-12)

`#116` (per-sector readiness) asked for a repeatable audit; this is its first real run,
pulled straight from the live `feed.json` (`as_of` 2026-09-09) via
`executive.{cso,cro,cco,cpo}.by_industry` — no synthetic numbers. It answers a question ADR 024
didn't yet: **which of the 17 covered sectors can actually be demoed or sold today, and to which
officer persona.**

| Sector | CPO maturity | CSO (cards/moves/alerts) | CRO (reg items / prudential) | CCO (integrity / reputation) | Tier |
| --- | --- | --- | --- | --- | --- |
| banking | 84 | 173/95/90 | 26 / ✅ | 0/13 | **GA-ready** |
| fintech | 80 | 109/33/54 | 24 / ✅ | 0/13 | **GA-ready** |
| investment-banking | 70 | 76/61/37 | 6 / ✅ | 0/4 | **GA-ready** |
| agri-funds | 65 | 41/27/30 | 4 / ✅ | 0/1 | Adequate (CSO/CRO only) |
| betting | 63 | 60/5/1 | 4 / ❌ | 0/0 | Adequate, CSO-only, thin signal |
| acquiring | 60 | 44/13/25 | 6 / ✅ | 0/3 | Adequate (CSO/CRO only) |
| advisory | 57 | 36/23/8 | 4 / ✅ | 0/0 | Adequate (CSO/CRO only) |
| crypto | 57 | 37/8/0 | 4 / ✅ | 0/0 | Adequate, zero alerts |
| financial-data-analytics | 54 | 36/15/9 | 4 / ✅ | 0/0 | Adequate (CSO/CRO only) |
| insurance | 53 | 51/10/19 | 4 / ✅ | 0/1 | Adequate (CSO/CRO only) |
| asset-management | 52 | 39/35/19 | 4 / ✅ | 0/2 | Adequate (CSO/CRO only) |
| real-estate-funds | 52 | 27/11/7 | 4 / ❌ | 4/0 | Adequate (CSO/CCO only) |
| wealth-management | 48 | 23/19/10 | 4 / ✅ | 0/1 | Adequate (CSO/CRO only) |
| private-markets | 46 | 9/5/1 | 4 / ❌ | 0/0 | **Not ready** |
| consorcio | 37 | 34/8/7 | 22 / ✅ | 0/1 | Adequate (CSO/CRO only) |
| securitization | 25 | 6/2/2 | 4 / ❌ | 0/0 | **Not ready** |
| closed-pension | 23 | 4/0/0 | 4 / ❌ | 0/0 | **Not ready** (0 tracked entities) |

Findings that change what "per-sector readiness" means:

1. **Richness is one axis, not four.** CPO maturity, CSO volume, and CRO regulatory depth move
   together — there is no sector rich for one officer and starved for another. Sector tiering
   can be a single number, not a per-officer matrix.
2. **CCO is starved almost everywhere, structurally — not a data-quality bug.** Integrity
   findings are 0 in 16/17 sectors (only `real-estate-funds` has any); reputation rows only
   populate meaningfully in banking/fintech, because the consumidor.gov.br index only covers
   retail-facing regulated institutions. A CCO-persona pitch outside banking/fintech is
   currently pitching an empty panel.
3. **CRO's prudential column (Basileia/NPL) is correctly scoped, not a gap.** `✅` tracks BCB/
   COSIF-reporting institutions; `❌` (betting, real-estate-funds, private-markets,
   securitization, closed-pension) reflects that those sectors have no such regulatory filing to
   ingest. Do not reopen this as a missing-source ticket.
4. **Bottom tier is a genuine go-to-market signal**, not a display artifact: `closed-pension`,
   `securitization`, `private-markets` are thin across every officer and every dimension.

Issues filed to act on this: **#117** (encode the tier list as the sales/demo gate), **#118**
(CCO per-sector data starvation — expand sources or add an honest "insufficient signal"
caveat), **#119** (decide invest-vs-exclude for the bottom-tier sectors).

## Roadmap

Issues filed: **#109** (SLO/alarms, closed), **#110** (PITR), **#111** (SES), **#112** (trust
pack), **#113** (pricing), **#114** (onboarding runbook), **#116** (per-sector readiness —
first pass above), **#117**–**#119** (readiness follow-through), **#115** (Entry self-serve,
deferred), **#49** (Marketplace, last).

The dated, gated plan lives in [`docs/2026-09-11-launch-roadmap.md`](2026-09-11-launch-roadmap.md).
