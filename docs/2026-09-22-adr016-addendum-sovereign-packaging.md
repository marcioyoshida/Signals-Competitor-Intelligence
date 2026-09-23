# ADR 016 Addendum — Sovereign in-account packaging: the telemetry-off boundary,
## the resolve API, and the tier/plane collision (#49)

- Status: **Proposed** — 2026-09-22.
- Addends [ADR 016](2026-08-30-adr-distribution-three-tier.md) §③ Sovereign and its
  "Open decisions", and [ADR 005](2026-08-23-adr-private-tenant-in-account.md)
  (superseded by ADR 015/016 but its mechanics — in-account stack, resolve-by-API,
  the `tenant_s3` private lens — are carried forward unchanged and are the design
  this addendum builds on, not replaces).
- Triggered by [#49](../) (AWS Marketplace in-account distribution), the one open
  M3 item that isn't yet code — its own text says **"design-first"**. This is that
  design pass, answering the two questions #49 leaves open: what "telemetry off"
  means concretely, and what the boundary is between "runs in their account" and
  "we can still tell if they're licensed."
- Grounded in an audit of the current repo (2026-09-22), not a market document.
  Every claim below about what does or doesn't exist was checked against the code
  the same day this doc was written.

## Why this needed a design pass before any CDK work

`pricing.html` already publishes Sovereign — R$25.000/mês, "implantada diretamente
na conta AWS do cliente" — on the one credential-free page a prospect can reach,
and now (E2, #154) a prospect can reach it *with a real sample already in hand*.
The gap between "sold" and "built" got more exposed, not less, the moment self-serve
shipped. Before adding a single CDK construct, four things needed to be nailed down,
because each is expensive to unwind after a customer's account depends on it — the
same caution ADR 005 §Costs already named ("a hard runtime dependency", "widens the
release/compat surface").

## Finding 0 — what the audit actually found

Four concrete things, none of them hypothetical:

1. **The `/resolve` API does not exist.** ADR 005 §2 and ADR 016 §③ both write as
   if it's a settled, buildable contract ("keep the live resolve API (decided)").
   It isn't built. `registry_api.py` exposes `/reviews/{id}` (curation) and nothing
   resolve-by-name/cnpj/ispb. The synth-side functions it would front —
   `entities.resolve_by_cnpj`, `entities.resolve_entities` — are internal, not an
   API. #49's "wire the registry-resolve API boundary" is therefore not hardening
   an existing thing; it is building one from zero.

2. **`tenant_config`'s `plane` field is write-only.** `VALID_PLANES = ("portal",
   "saas", "marketplace")` exists, every tenant record carries a `plane`, and
   `_default_plane(tier)` sets it — but nothing in the codebase ever reads or
   branches on it. It was the delivery-mechanism axis ADR 015 designed and ADR 016
   inherited; today it is decoration.

3. **`tier="sovereign"` already means something, and it isn't "runs in the
   tenant's account."** `act_api.py` and `registry_api.py` both grant elevated
   operator capabilities to `tier ∈ {"operator", "sovereign"}` (cross-industry Ask,
   registry mutation) — and this is live, working code, exercised today by the
   seeded QA persona `("qa-test-admin@onca.example", "qa-internal-admin",
   "sovereign")` in `infra/app.py`. That tenant runs on **shared vendor infra**,
   same as every SaaS tenant — there is no in-account deployment mechanism for it
   to run anywhere else. So `tier="sovereign"` currently means *elevated privilege
   within the shared platform*, while ADR 016's "Sovereign" means *deployed inside
   the tenant's own AWS account, telemetry off*. Two different concepts, one field,
   one string value. If #49 ships without resolving this, the natural next bug is
   someone reading `tier == "sovereign"` and concluding a tenant is in-account when
   it's actually a shared-infra tenant with an elevated Cognito claim — exactly the
   kind of trust-boundary mistake ADR 005 was written to prevent, just moved one
   layer up into the entitlement model instead of the network model.

4. **One hardcoded vendor-account reference exists** — `src/synth/lambda_handler.py`
   `__main__`'s `--s3` debug path defaults `ONCA_DIGESTS_BUCKET` to
   `onca-digests-668449743071` (the vendor's own bucket) if unset. Low severity
   today — it's a local CLI convenience, never reached by the Lambda runtime, and
   would just fail on a permissions error in a tenant's own execution role rather
   than actually cross an account boundary — but it is the one place in the repo
   where a vendor resource name is baked in rather than read from config, and it
   is exactly the shape of mistake a telemetry-off promise can't tolerate a second
   occurrence of. Flagged as a build-gate item below, not urgent-fixed here.

## Decision 1 — telemetry-off is structural (locality), not a flag

ADR 016 §③ already says the right thing ("non-observation is the product") but
doesn't say *how* that's achieved. The audit answers it: **every telemetry/
engagement code path — `engagement_log.record_engagement`, the `/api/act`
`record_engagement` intent, the dashboard's §E/§H beacons — is unchanged between
SaaS and Sovereign.** What changes is *whose DynamoDB table and whose Lambda* it
writes to. In SaaS, that's vendor-hosted infra the vendor can query. In Sovereign,
the identical code runs inside the tenant's own account against the tenant's own
`OncaCurationLog`/engagement table — the vendor has no credential that reaches it,
so it has no visibility, by construction. **Telemetry-off falls out of deployment
locality for free; it is not a separate mode to build, test, or drift out of sync.**

This is good news (nothing to build) with one hard requirement: it only holds if
**zero** code path references a vendor-account resource by identity rather than by
config. Finding 0.4 is the one violation found; it must be fixed (default to
raising, not to a vendor bucket name) before this decision can be signed off as
actually true rather than aspirationally true. The build-gate in Decision 5 is
what keeps it true going forward.

## Decision 2 — separate hosting locality from entitlement privilege

Stop overloading `tier` for two unrelated things. Going forward:

- **`plane`** (already exists, currently inert) becomes the field that answers
  "where does this tenant's stack run" — `portal` (Entry static), `saas` (shared
  multi-tenant), `marketplace` (in-account, ADR 015's name carried through
  unchanged in code even though ADR 016 renamed the *tier* to Sovereign — no
  rename needed, `marketplace` already means exactly the right thing here).
  **This is the field #49's packaging work gates on.** `plane == "marketplace"`
  is the only correct way to ask "is this an in-account deployment" — not `tier`.
- **`tier`** stays what ADR 002/024 already use it for: pricing/entitlement depth
  (`entry` | `saas` | `sovereign`) — which modules and which signal depth a tenant
  is licensed for. A tenant can be `tier="sovereign"` (licensed for the deepest
  derived graph/inference layer) while `plane="saas"` (still running on shared
  infra) during onboarding, before their in-account stack is provisioned — this
  is in fact the natural bridge path ADR 005 §5 already describes ("a tenant
  graduates SaaS → Private by deploying the CDK stack... an identical contract
  either way"). The two fields being independent is what MAKES that bridge
  representable instead of a special case.
- The elevated-privilege check in `act_api.py`/`registry_api.py` should move off
  `tier` entirely once this ships — a Cognito **group** (`operator`/`admin`) is
  already part of that same OR-condition and is the correct mechanism (a role
  grant, not a purchase). `_ELEVATED_TIERS` including `"operator"` — a value that
  was never in `VALID_TIERS` to begin with — is itself a small tell that tier was
  already being used as a privilege channel it was never modeled for.

This is a data-model and one-line-check migration, not a rearchitecture: existing
rows get `plane` populated correctly (most are `plane="portal"` by the current
default, which is wrong for the seeded `sovereign`-tier QA persona — a one-time
backfill), and the two or three call sites gating on `tier == "sovereign"` for
privilege switch to checking the Cognito group instead. Doing this now, before
the first real Sovereign deployment exists, costs an afternoon. Doing it after
costs a support incident.

**Done 2026-09-23** (`src/dashboard/act_api.py`, `src/dashboard/registry_api.py`,
`infra/app.py`'s new `OncaGroupOperator` Cognito group): the `plane` half of this
migration turned out to be a non-issue on inspection — the live seeded
`qa-internal-admin` row already carried `plane="saas"` (`_default_plane`'s
existing `"portal" if tier == "entry" else "saas"` rule already gets a
`sovereign`-tier row right; no backfill was actually needed there. The REAL,
confirmed live bug was the privilege check itself: `aws cognito-idp
list-groups` on the deployed pool showed **no `operator` or `admin` group
existed at all** before this commit, meaning the QA admin persona's — and
every future `sovereign`-tier tenant's — elevated registry-write/`/api/act`
access came *entirely* from `tier == "sovereign"`. Any tenant simply
*provisioned* at the sovereign pricing tier (a purchase) got operator
capabilities on shared SaaS infra, with no role grant involved at all. Fixed
by removing `_ELEVATED_TIERS` from both modules (elevation now checks
`_ELEVATED_GROUPS.intersection(identity.groups)` only), standing up a real
`operator` Cognito group, and attaching the QA admin persona to it so it keeps
working. Live-verified post-deploy by invoking the deployed
`OncaRegistryApi` Lambda directly with two synthetic JWT-shaped events:
`custom:tier=sovereign` alone now correctly gets `403`; `cognito:groups=
operator` gets through — confirming the fix is live, not just unit-tested.

## Decision 3 — the `/resolve` API contract

Adopt ADR 005 §2's contract as-specified; this addendum only makes the parts ADR
005 left implicit concrete enough to build against.

**Built and live-verified 2026-09-22** (`src/dashboard/resolve_api.py`,
`infra/app.py`'s `OncaResolveApi` block, `tenant_config.py`'s new
`resolve_caller_role_arn` field): shipped as an AWS_IAM Lambda Function URL
(not API Gateway — `src/synth/resolver.py`'s SigV4 signing was corrected from
its placeholder `service="execute-api"` assumption to `service="lambda"`),
deliberately NOT behind CloudFront since the caller is a tenant's own Lambda
in a different account, not a browser. The handler reuses
`resolver.resolve_known_id` for the actual lookup rather than re-implementing
it — this Lambda's environment never sets `ONCA_RESOLUTION_MODE`, so it runs
in `registry` mode, the exact same single-`get_item` code path `entities.py`
uses internally. Live-verified end-to-end against the deployed endpoint with a
throwaway probe IAM role (assumed, signed a real request, deleted after): a
known alias correctly resolved (`200`), an unknown identifier correctly missed
(`404`), an unregistered/mismatched tenant was refused (`403`), and a
zero-identifier request was rejected (`400`). One real trap surfaced during
that verification, worth carrying forward: **a Lambda Function URL created
after October 2025 requires BOTH `lambda:InvokeFunctionUrl` AND
`lambda:InvokeFunction`** granted to the caller — granting only the first
produces a bare AWS-edge 403 that never reaches the handler and is
indistinguishable from a SigV4 signature bug until you know to check for it;
documented in both `infra/app.py` and `resolve_api.py`'s docstring so the next
real tenant onboarding doesn't rediscover it the hard way. No real Sovereign
tenant is onboarded yet, so the function's resource policy currently grants
nothing to any external account — that's the deliberate, correct state (per
`infra/tenant_stack.py`'s same "not deployed anywhere by this commit"
caution), not a gap.

```
POST /resolve
  Request:  { name?, cnpj_root?, ispb?, ticker? }   -- resolve-by-known-id/-name only
  Response: { entity_id, display_name, canonical_id, industries[], confidence }
```

- **No list/scan/batch.** One lookup, one result. A request with no match returns
  `404`, not a fuzzy nearest-neighbor — a Sovereign tenant guessing its way to
  enumeration is the one failure mode that actually ships the moat.
- **Auth: per-tenant IAM role, not a shared API key.** Cross-account, so this
  should be a resource-based Lambda/API-Gateway policy trusting a specific role
  ARN in the tenant's account (SigV4-signed calls), one role per Sovereign tenant.
  A shared long-lived secret is the wrong shape for a boundary explicitly designed
  to survive being the *only* thing crossing it — if it leaks, it's the whole
  moat's front door, not one tenant's. This also gives revocation a single,
  unambiguous lever: detach the trust policy statement.
- **Rate limit + breadth-anomaly detection per tenant role**, exactly as ADR 002
  Phase D specifies. A Sovereign tenant's own synth calling `/resolve` at a rate
  consistent with "processing today's narrative volume" looks nothing like a bulk
  scrape attempt; the anomaly detector's threshold is the enforcement, not a
  static per-minute cap.
- **What "metering" means here, given ADR 024's flat license fee.** ADR 005 calls
  `/resolve` "the natural metering + entitlement chokepoint" — but ADR 024 already
  decided Sovereign is a **flat R$25k/month license, not usage-billed**, and the
  pricing amendment separately parked all consumption-metering work as a standing
  decision. So metering here means **entitlement (is this tenant's role still
  authorized to call at all) and anomaly/support observability (are they calling
  in a shape consistent with normal operation)** — never a line item on an
  invoice. Worth stating explicitly so nobody builds a usage-billing pipeline onto
  this endpoint by inference from the word "metering" in ADR 005.
- **Offline resilience is the encounter-only TTL cache**, per ADR 005 §2 —
  unchanged by this addendum. Cap size, TTL it, never a bulk pull.

## Decision 4 — packaging shape, named honestly

`OncaPrototypeStack` is one 3,500-line CDK class today, in one account. #49 needs
a **second, parameterized stack variant** deployable into a tenant account,
carrying only the ADR 005 §1 table's right-hand column (ingest of public sources,
synth/swot/threads/feed, the tenant's own KB, the dashboard) — never the registry,
never discovery. This is a real extraction project against a stack that was never
factored for it, not a config flag on the existing one. Concretely, in order:

1. **Split `OncaPrototypeStack` into a shared construct library + two stack
   entry points** (vendor stack — unchanged, keeps the registry/discovery/all
   current tenants; tenant stack — new, parameterized by tenant bucket list +
   vendor `/resolve` endpoint + the per-tenant IAM role from Decision 3).
2. **Build `src/ingest/tenant_s3.py`** (the private-S3 lens ADR 005 §3 specifies)
   — net-new, not an extraction. **Done 2026-09-22**: `collect()` enumerates
   configured tenant buckets/prefixes (`ONCA_TENANT_PRIVATE_BUCKETS`), diffs
   via `src/diff/engine.py`'s seen-set (keyed by bucket/key **and etag**, so an
   object edited in place re-ingests instead of vanishing into the seen-set
   forever), and normalizes to the same raw-doc shape every other
   `src/ingest/*` source produces; `ingest()` hands the new docs to
   `raw_writer.write_raw_documents` against `ONCA_RAW_BUCKET` — in a tenant
   deployment, `infra/tenant_stack.py`'s `OncaTenantRawBucket`, never the
   vendor's, by config rather than a mode flag. Both functions no-op (return
   `[]`) when unconfigured, so the module is inert in every existing SaaS/
   vendor deployment today. Scoped to plain-text-ish formats
   (`.txt`/`.md`/`.csv`/`.json`) for this pass — PDF/DOCX/XLSX need real
   extraction, not a `.decode()`, and are left for a follow-on. Not wired into
   any Lambda yet (none exists to wire it into — see `infra/tenant_stack.py`'s
   docstring); this module is the ingester itself, callable once a tenant
   ingest Lambda exists.
3. **Flip `entities.py`'s resolution mode** — internal registry lookup (vendor
   stack, unchanged) vs. `/resolve` HTTP call (tenant stack) — behind one seam,
   so synth code above it never branches on which plane it's running in.
   **Started 2026-09-22** (`src/synth/resolver.py`): the seam covers exactly the
   known-id lookups (`resolve_by_cnpj`, and by construction `name`/`ispb`/
   `ticker` once a caller needs them) that map onto a single `get_item` / a
   single `/resolve` call. It does **not** cover `resolve_entities` — that
   function scans the FULL alias corpus against free text, and `/resolve`'s
   response deliberately withholds the alias set (Decision 3), so there is
   nothing safe to cache and match against remotely. **This is a real,
   unresolved product gap, not an implementation detail**: a Sovereign tenant's
   own locally-ingested public-source narratives cannot be entity-tagged by
   free-text mention at all under the current `/resolve` contract — only
   documents carrying a structured identifier the tenant already extracted
   (CNPJ, ticker) can resolve. `resolve_entities` degrades to returning no
   matches in remote mode (one loud warning, not a crash, not a silent guess).
   Closing it needs a different mechanism — e.g. the tenant's own local, non-
   registry candidate-name extraction (a generic NLP pass, not the moat), each
   candidate then resolved one at a time through the same seam — which is new
   design work, not implied by "flip the mode," and isn't scoped here.
4. Pin a supported stack version per ADR 005 §Costs ("the resolve contract is the
   compatibility boundary") — decide the versioning/upgrade story before the
   first tenant deploy, not after two tenants are on different versions.
   **Done 2026-09-22** (`docs/tenant-stack-versioning.md`): what's actually
   pinned is the `/resolve` contract shape, not the stack's internals — the
   only thing that crosses the account boundary and can therefore break a
   deployed tenant without a redeploy. `infra/tenant_stack.py`'s
   `TENANT_STACK_VERSION` and `src/synth/resolver.py`'s
   `RESOLVE_CONTRACT_VERSION` move together (currently `1.0.0`/`1`), a
   semver policy defines what's MAJOR/MINOR/PATCH, the version is tagged +
   output on every tenant stack and sent (inside the SigV4-signed request) on
   every `/resolve` call, and upgrades ride the normal `cdk deploy` — every
   stateful resource is already `RemovalPolicy.RETAIN`, so no migration
   tooling is needed. The support-window length (how many MAJOR versions the
   vendor serves concurrently) is deliberately left undecided until Decision
   3's endpoint exists and there's a real second version to weigh against the
   first, rather than guessed at with zero tenants live.

This is the multi-week item flagged when this addendum was proposed. Nothing here
shortens it; naming the four steps is what makes "how far along is #49" answerable
without re-deriving the plan from scratch each time someone asks.

## Decision 5 — "telemetry-off, verified" is a check, not a checklist line

A per-account deploy runbook that says "confirm telemetry is off" as a manual
step will pass review once and rot silently after. Decision 1 already establishes
telemetry-off is structural — so verification should be an automated assertion
that the structure holds, run against every tenant-stack synth/deploy:

- **A static scan** (grep-class, cheap, runs in CI) that fails the build if any
  `src/` module references a vendor-account resource identity — bucket name,
  table name, account ID, hardcoded ARN — rather than reading it from
  environment/config. This directly closes Finding 0.4's category of bug, not
  just that one instance.
- **A synth-time assertion** on the tenant CDK stack: enumerate every IAM
  principal/resource policy the stack grants, and fail if anything reaches
  outside the tenant's own account except the one `/resolve` role trust
  relationship from Decision 3. This is the CDK-level version of ADR 016's "one
  governed, audited egress" — turned from a sentence into a test that runs on
  every deploy, not a property someone remembers to eyeball.

Both are small (a day or two each) relative to Decision 4's scope, and they are
the difference between "we believe telemetry is off" and "we can prove it on every
deploy" — the second is what a compliance-bound Sovereign buyer is actually paying
the premium for.

## Answering ADR 016's open decisions, as far as this addendum can

- **Vertical map** — unaffected by this addendum; still banking / investment-
  banking / M&A-private-markets / advisory as the Sovereign default, confirmed
  unchanged. `private-markets` remains not-GA-ready (#119) and therefore not
  sellable at any tier including Sovereign until that clears — this addendum
  doesn't touch that gate.
- **Entry billing shape** — out of scope here; that's an Entry/M2 question, not
  Sovereign/M3.
- **True air-gap** — still deferred, unchanged. No buyer has required it; building
  the scoped-snapshot resolver speculatively would trade a real, working single-
  egress design for a harder-to-verify one, against a requirement nobody has
  stated. Revisit only if a Sovereign prospect actually asks.

## Consequences

**Positive**
- Decision 1 means the hardest-sounding requirement ("telemetry off") costs
  approximately nothing to build correctly — it was already true by construction,
  modulo one small fix and an automated check to keep it true.
- Decision 2 catches a real entitlement-model bug (tier/plane collision) before
  it ships load-bearing, while it's still a data-model migration instead of a
  production incident on a paying Sovereign account.
- Decision 3 gives #49 a contract to build against instead of a one-line mention
  in another ADR — the request/response shape, the auth mechanism, and what
  "metering" does and doesn't mean are all now decided, not implied.
- Decision 4 turns "package it for Marketplace" into four ordered, estimable
  steps instead of one large undifferentiated task.

**Costs / risks (honest)**
- Decision 4 is still a multi-week build; this addendum de-risks and sequences
  it, it does not shrink it.
- The per-tenant IAM role model in Decision 3 means onboarding a Sovereign tenant
  has a real cross-account IAM setup step (trust policy on both sides) — this is
  the "higher-touch onboarding" ADR 005 already priced in, made concrete.
  `resolve_entities` full-scan performance is a separate known issue (see
  `onca-entity-discovery-generalized`) that the `/resolve` API build should not
  inherit unexamined — worth a perf pass when Decision 3 is actually implemented,
  not assumed fine because the internal function already exists.
- Decision 2's migration touches live entitlement-checking code
  (`act_api.py`/`registry_api.py`); it should ship with the same fail-closed
  discipline those modules already follow, and a rollback path if the group-based
  check regresses the QA sovereign persona's access.

## Alternatives considered

- **Build the CDK tenant stack first, resolve tier/plane semantics later.**
  Rejected — the entitlement collision in Finding 0.3 is cheap to fix now and
  expensive once a real tenant's account depends on `tier` meaning one specific
  thing.
- **Treat "telemetry off" as a checklist item in the deploy runbook.** Rejected —
  ADR 005 already flagged "support into a black box" as a named cost; a promise
  the vendor can't independently verify on every deploy is worse than an honest
  "we don't currently check this."
- **A shared API key for `/resolve` instead of per-tenant IAM roles.** Rejected —
  simpler to build, but a single leaked key compromises every Sovereign tenant's
  egress at once and gives no clean per-tenant revocation lever; wrong trade for
  the one endpoint this whole tier's trust model rests on.
- **Build the true air-gap variant now, since Sovereign buyers are compliance-
  sensitive.** Rejected per ADR 016 — no buyer has asked, and building it
  speculatively enlarges the moat-exposure surface (a synced snapshot is a bigger
  target than one governed live call) for a requirement that may never come.

## Build deltas (against ADR 005/016 + current repo)

- `src/dashboard/registry_api.py` (or a new `resolve_api.py`) — the `/resolve`
  endpoint itself (Decision 3), net-new.
- `src/dashboard/tenant_config.py` — stop `tier` from gating elevated privilege;
  backfill `plane` correctly for existing sovereign-tier rows (Decision 2).
- `src/dashboard/act_api.py`, `src/dashboard/registry_api.py` — elevated-access
  checks move from `tier == "sovereign"` to Cognito group only.
- `src/synth/lambda_handler.py` — remove the hardcoded vendor bucket default
  (Finding 0.4); fail loud instead.
- `src/ingest/tenant_s3.py` — net-new, per ADR 005 §3.
- `src/synth/entities.py` — resolution-mode seam (internal vs `/resolve`), per
  ADR 005 §Build deltas, unchanged by this addendum.
- `infra/` — split into shared constructs + vendor stack (unchanged) + new
  parameterized tenant stack; a CI check for hardcoded vendor-account references;
  a synth-time egress assertion on the tenant stack (Decision 5).
- Docs: this file supersedes nothing — ADR 016 §③ and ADR 005 stand; this is the
  concretization layer between them and #49's implementation.
