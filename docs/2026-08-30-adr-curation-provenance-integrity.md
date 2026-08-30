# ADR 018 — Curation Provenance, Write-Precedence & Continuous Integrity

Status: PROPOSED 2026-08-30. Relates to ADR 005/017 (entities registry), ADR 011
(discovery/enrichment), ADR 013 (classification attrs), ADR 014 (coverage-gap loop),
ADR 002 (registry as a commercial API product).

## Context

The DynamoDB entities registry is **the commercial asset** — "the single source of truth
for all per-entity curation; code fixtures are seed-only." It is written by four very
different actors with no shared governance:

- **fixture seed** (`entities.py` via `entity_registry.seed()`),
- **human / API curation** (`put_entity`, `set_industries`, review-queue promotions),
- **automated discovery/enrichment** (`discover_fiagro`/`discover_consorcio`,
  `accumulate_aliases`, `assign_ticker`, `set_parent`, FIAGRO/consórcio enrich),
- **inference from unstructured text** (news resolution, attribution).

Three production incidents this quarter share one root shape — **silent corruption of a
curated field by an automated writer, detected only when a human spotted a bad card:**

- **#52** — a feedback loop where discovery re-added `agri-funds` and fund tickers onto
  banks (bb/btg/xp) every run. We shipped two point-guards; the *general* rule was missing.
- **#50** — cross-entity mis-clustering (unrelated ICBC/BB/Mercantil signals fused under
  "Consórcio Magalu"); a `_soft_related` invariant, again found by eye.
- **BTG → FIAGRO / ADR-017** — institutions inheriting a fund industry via brand match.

We keep writing *local* guards. The registry has **no provenance** (you cannot tell whether
`btg.industries` came from the fixture, a human, or pollution), **no write-precedence** (an
automated write can overwrite a curated one), and **no continuous detector** (anomalies are
found manually). For a data-as-product business this is the highest-leverage gap.

## Decision

Make the registry a **governed store** with four capabilities. Each is additive and
backward-compatible with the current item shape.

### 1. Per-field provenance
Every mutable field carries an origin record, stored inline on the entity item under
`_prov`:

```
_prov: { industries: {source, confidence, set_at, actor?}, aliases: {...}, ticker: {...}, ... }
```

`source ∈ {fixture, curated, structured, discovery, enrich, inferred}`; `confidence` reuses
the existing tiers (`cnpj`/`curated`/…); `set_at` is an ISO timestamp. The registry's
single writers (`put_entity`, `set_industries`, `accumulate_aliases`, `assign_ticker`,
`set_parent`, `set_esg`) stamp provenance; callers pass a `source`.

### 2. Write-precedence (the general form of the #52 guards)
A precedence order governs whether a write applies, is **diverted to a review proposal**,
or is rejected:

```
fixture ≈ curated (human)  >  structured (CNPJ filing)  >  discovery/enrich  >  inferred (news)
```

- An **automated** write (discovery/enrich/inferred) to a field whose current provenance is
  **curated/fixture** does NOT apply — it becomes a `propose_review` entry (reusing ADR-014
  /ADR-011 review machinery). This is exactly what #52/ADR-017 needed generically: discovery
  can never demote a curated institution's `industries` or accrete a fund alias onto it.
- Automated-vs-automated: higher confidence wins; equal confidence appends (sets) or is
  proposed (scalars).
- Human/API writes always win and re-stamp provenance to `curated` (raising the lock).

### 3. Continuous integrity audit
A scheduled detector (a pipeline step / Lambda) scans the registry + latest feed for
**invariant violations** and emits durable *integrity findings*, routed to the existing
review surface (Pontos Cegos / coverage-gap loop) with a safe-remediation path:

- an entity with an institutional (non-leaf) industry that ALSO carries a leaf/fund
  industry from an automated source (the #52 signature);
- a card whose **primary entity is absent from its own narrative**, or that fuses ≥2
  different tracked entities' structured signals (the #50 signature);
- a fund ticker / CNPJ present as an alias on an institution;
- an entity with `confidence=cnpj` but no `cnpj_roots` (unbacked structured identity);
- a sub-entity whose `parent` is itself a leaf/fund (ADR-017 inversion).

Findings that map to a known-safe fix (strip a fund alias, drop a demoted industry) can be
auto-remediated under the same guardrails as ADR-014; the rest become review items / issues.

### 4. Change-log & rollback
An append-only mutation log (`LOG#{entity}#{ts}` items or an S3 journal): `{entity_id,
field, old, new, source, ts}`. It backs provenance, gives an audit trail for the commercial
registry, and enables targeted rollback of a bad automated run (the manual `set_industries`
cleanups we've done by hand become a one-command revert).

## Phasing

- **Phase 1** — provenance stamping on the write surface + the change-log (observability
  first; nothing is blocked yet).
- **Phase 2** — write-precedence: lock curated/fixture fields; divert conflicting automated
  writes to proposals. Retire the per-source point-guards (#52/ADR-017) once subsumed.
- **Phase 3** — the continuous integrity audit + its review/remediation surface.
- **Phase 4** — rollback tooling over the change-log.

## Consequences

- The registry-as-product gains an audit trail, provenance, and a governance boundary —
  table stakes for selling curated data and for Marketplace/in-account (#49) trust.
- The recurring silent-corruption class is **prevented by construction** (precedence) and
  **detected continuously** (audit), replacing whack-a-mole point-fixes.
- Cost: a provenance field per item + a modest log; write paths gain a source argument.
  All additive — existing consumers ignore `_prov`.
- Risk: over-locking could block legitimate automated enrichment. Mitigated by the
  precedence order (a higher-confidence structured source still wins) and by routing
  conflicts to review rather than dropping them.

## Alternatives considered
- **Keep shipping point-guards** — rejected: doesn't generalize; each new writer reopens the
  hole (as #52 proved after `37fb048`).
- **A separate provenance/lineage service** — deferred: inline `_prov` + a log is enough at
  this scale and keeps a single-read entity.
