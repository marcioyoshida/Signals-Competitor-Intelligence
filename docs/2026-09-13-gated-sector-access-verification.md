# Gated sector access & officer scoping — live verification (2026-09-13)

Confirms the per-tenant read boundary (ADR 016) actually holds for **every**
officer persona (CSO/CRO/CCO/CPO), not just the top-level feed — the gap fixed
in `_rescope_executive` (2026-09-12, commit `aa87ade`) after the industry-sector
scoped sessions work. Run against the live `OncaFeedApi` Lambda
(`OncaPrototypeStack-OncaFeedApi086A4381-BG0SDNWBQ32T`) with synthetic
JWT-authorizer-shaped payloads (`{"custom:tenant": ..., "custom:tier": "saas"}`)
— the same code path a real Cognito-authenticated request hits, not a unit-test
double.

## Method

For each real tenant row in `OncaTenantConfig`, invoked the feed Lambda directly
and checked, for all four officers, that `executive.<officer>.by_industry` keys
(excluding the synthetic `__all__` aggregate) equal exactly that tenant's
licensed `modules` — no more, no less.

## Result

| Tenant | Tier | Licensed modules | CSO sectors seen | CRO sectors seen | CCO sectors seen | CPO sectors seen | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `acquiring` | saas | acquiring | acquiring | acquiring | acquiring | acquiring | ✅ exact match |
| `fintech` | saas | fintech | fintech | fintech | fintech | fintech | ✅ exact match |
| `insurance` | saas | insurance | insurance | insurance | insurance | insurance | ✅ exact match |
| `wealth-management` | saas | wealth-management | wealth-management | wealth-management | wealth-management | wealth-management | ✅ exact match |
| `onca-saas-pilot` | saas | acquiring, banking | acquiring, banking | acquiring, banking | acquiring, banking | acquiring, banking | ✅ exact match (multi-vertical) |
| `onca-entry-pilot` | entry | agri-funds, betting, consorcio | (same 3) | (same 3) | (same 3) | (same 3) | ✅ exact match (entry-tier multi) |
| `onca-mkt-pilot` | sovereign | banking | banking | banking | banking | banking | ✅ exact match (sovereign plane) |
| `no-such-tenant` (unprovisioned) | — | — | — | — | — | — | ✅ `403 {"error":"no entitlement"}` |

Zero leakage in any cell — no tenant's officer view ever showed a sector outside
its own `modules`, across single-vertical, multi-vertical, entry-tier, and
sovereign-plane tenants alike.

## Additional checks

- **Weekly CSO brief scoping** (`executive.cso.weekly.by_industry`, the data
  `weekly_digest.py` sends): checked on `onca-saas-pilot` — keys are exactly
  `{acquiring, banking, __all__}`. The tenant-scoped `__all__` here is an
  aggregate over *that tenant's own licensed sectors only* (rebuilt by
  `build_executive` from the already-scoped feed), not the full 17-sector
  corpus `__all__` — this is the correct, intentional semantics, but worth
  flagging explicitly since the key name is identical in both the scoped and
  unscoped feed.
- **Public Entry feed** (`feed.entry.json`, no auth, serves the shared Entry
  Portal): `executive.<officer>.by_industry` for all four officers contains
  exactly the 5 entry-tier industries (`agri-funds, betting, consorcio, crypto,
  real-estate-funds`) — none of the 12 higher-tier sectors leak into the
  logged-out public surface.
- **CCO signal-state caveat** (#118) survives scoping: spot-checked `fintech`'s
  CCO block, `signal_state: "sufficient"` present and correctly attached to the
  scoped `by_industry` entry, not lost in the rebuild.

## What this does not cover

- Client-side `/exec` (v3) behavior — this verification is server-side only
  (the Lambda response itself). The v3 dashboard's sector-picker filtering
  (`LICENSED` array, `licensedIndustries(DATA)`) was implemented and code-reviewed
  in the 2026-09-12 work but not re-exercised here; it consumes exactly the
  response shape verified above, so a correct server response is the
  precondition, not a substitute, for a correct client render.
- The grounded Q&A agent's (`/api/ask`) own module-narrowing (`agent_ask.py`,
  ADR 019) was not re-tested here — it has a separate, already-tested scoping
  path (`_scope_cards_to_modules`, `tests/test_tenant_config.py`).
