# Tenant-stack versioning policy

ADR 016 addendum (2026-09-22), Decision 4 step 4. ADR 005 §Costs names the risk
this closes: "shipping the pipeline as tenant-deployable CFN widens the
release/compat surface (per-tenant version skew). Pin a supported stack
version; the resolve contract is the compatibility boundary." This doc is that
policy, decided before the first real tenant deploy rather than after two
tenants are on different versions.

## What's actually pinned, and why it's the contract, not the stack

`infra/tenant_stack.py`'s `TENANT_STACK_VERSION` and `src/synth/resolver.py`'s
`RESOLVE_CONTRACT_VERSION` move together — currently `1.0.0` / `1`. They are
**not** a version of the CDK code as a whole; they version the one thing that
crosses the account boundary and therefore the one thing that can actually
break a deployed tenant: the `POST /resolve` request/response shape (ADR 005
§2, ADR 016 addendum Decision 3):

```
Request:  { name?, cnpj_root?, ispb?, ticker? }
Response: { entity_id, display_name, canonical_id, industries[], confidence }
```

Everything else inside the tenant stack — table names, KB chunking config,
IAM policy structure, even which resources exist — can change freely between
tenant deployments without breaking anyone, because none of it is a contract
between two accounts. A tenant simply redeploys the current CDK code and picks
up those changes; there's nothing on the other side of an account boundary to
go stale. The `/resolve` contract is different: the vendor and every deployed
tenant must agree on it simultaneously, across accounts, without a shared
deploy — that's the actual compatibility surface, and the version number
exists to make disagreement about it detectable instead of silent.

## Semver rules

- **MAJOR** — the `/resolve` request or response shape changes in a way an
  older client can't parse or an older server can't serve (a field renamed or
  removed, a new required request field, a response field's type changing).
  Requires a coordinated rollout: the vendor endpoint must support both the
  old and new MAJOR version for the length of the support window below before
  the old one is dropped.
- **MINOR** — additive, backward-compatible changes to the contract (a new
  optional request field, a new optional response field existing clients can
  ignore). Old clients keep working unmodified; new clients can opt in.
- **PATCH** — no contract change at all. Internal stack/resolver refactors,
  bug fixes, doc updates. Never requires a tenant to redeploy on any
  particular timeline.

`TENANT_STACK_VERSION` and `RESOLVE_CONTRACT_VERSION` bump together on every
MAJOR/MINOR change (they're the same underlying contract, expressed on two
sides of the account boundary); PATCH changes to the stack that don't touch
the contract may leave `RESOLVE_CONTRACT_VERSION` untouched.

## How the version travels

- `infra/tenant_stack.py` tags every tenant stack `onca-tenant-stack-version`
  and emits it as the `TenantStackVersion` CfnOutput — visible in the
  tenant's own CloudFormation console without asking them to inspect code.
- `src/synth/resolver.py`'s `_sign_and_post` sends
  `X-Onca-Resolve-Contract-Version` on every `/resolve` call, inside the
  SigV4-signed request (not added after signing) — a value in transit can't
  be downgraded without invalidating the signature.

## Support window and enforcement (design, not yet enforced)

**Not yet built**, because the endpoint it applies to (Decision 3) doesn't
exist: once the vendor's `/resolve` API is live, it should read the
`X-Onca-Resolve-Contract-Version` header and:

- Serve any MAJOR version still inside its support window normally.
- Reject a MAJOR version outside the support window with a clear `4xx` and an
  explanatory body — never silently mismatch request/response shape against
  an old client, which fails as a confusing downstream synth error instead of
  an honest "please upgrade."
- The support window itself (how many MAJOR versions stay served
  concurrently, how long a deprecated MAJOR stays up before being cut) is a
  support/ops decision to make when there's a second real MAJOR version to
  weigh against the first — pre-deciding a number now, with zero tenants and
  one MAJOR version in existence, would be guessing. What's decided here is
  that the version is visible and checkable on both sides; how long "old"
  stays served is deliberately left open until it's a real tradeoff.

## Upgrade procedure

A tenant upgrades by redeploying the CDK stack with the newer code — same
`cdk deploy` flow as first install, same account, same context values. Every
stateful resource in `infra/tenant_stack.py` uses `RemovalPolicy.RETAIN`, so a
redeploy never risks tenant data; CloudFormation only touches resources whose
definition actually changed. No separate migration tooling exists or is
needed for MINOR/PATCH bumps. A MAJOR bump additionally requires confirming
(once Decision 3 ships) that the vendor endpoint still serves the tenant's
current contract version, or scheduling the upgrade before the deprecation
window's `4xx` cutover.
