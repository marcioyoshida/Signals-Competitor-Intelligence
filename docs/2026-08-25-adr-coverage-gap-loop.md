# ADR 014 — Coverage-gap loop: self-improving data coverage from unanswered questions

- Status: **Accepted & implemented (v1)** — 2026-08-25. Owner-requested.
- When the agent ([ADR 010](2026-08-25-adr-agent-chat-ui.md)) can't answer an *in-domain*
  question, treat it as a data-coverage signal and drive it through:
  **issue creation → assert if current data is enough → decide remediation → implement
  the safe ones → test → confirm & close.**
- Code: `src/synth/coverage.py`, `tests/test_coverage.py`; capture wired into
  `src/dashboard/agent_ask.py`; surfaced in `src/dashboard/feed_builder.py`
  (`feed.json.coverage_gaps`); infra grants the agent the digests bucket.

## Context

The agent grounds only on what the data *scopes*. Its honest "não tenho esse dado" is a
precise, valuable signal: it names exactly where coverage is missing. Rather than lose that
signal, capture it and close the loop back into ingestion/curation.

## Decision

A six-stage loop, split between an always-on **capture** (in the request path) and an
out-of-band **remediation** driver (schedulable / CI / manual):

1. **Capture (issue creation).** In `agent_ask`, an **in-domain** answer with no grounding
   (`grounded=False`, not a refusal) is folded into a durable `coverage_gaps/index.json`
   (deduped by normalized question; counts recurrences). Best-effort, never breaks the
   response.
2. **Assert if data is enough (triage).** `triage(q)` classifies the gap:
   - `curation_gap` — a tracked entity is missing a *curated attribute we can derive*
     (ownership/ticker/industries) → **auto-fixable**.
   - `discovery_gap` — names an entity **not in the registry** → propose discovery (ADR 011).
   - `ingestion_gap` — asks for a **data type no source ingests** (certifications, ESG,
     headcount…) → needs new code → issue.
   - `retrieval_gap` — data likely in KB but retrieval missed → tune grounding.
3. **Decide remediation.** Each class carries a concrete recommendation.
4. **Implement the safe ones (`safe_autofix`).** The loop **auto-applies only bounded,
   reversible, data/curation** remediations — the registry backfills
   (`backfill_ownership`/`backfill_tickers`/`backfill_curation`). These "deploy" instantly
   because the registry is the live source of truth (no code deploy).
5. **Test / confirm (`verifier`).** Re-ask the live agent; if it now grounds, the gap is
   **resolved & closed**.
6. **Confirm & close / else propose.** Anything not auto-resolved gets a **GitHub issue**
   (deduped by a `coverage-gap-id:` marker, label `coverage-gap`) + `status=proposed`, and
   is surfaced in `feed.json.coverage_gaps` for the war room. Both sinks (store + GitHub).

## Autonomy boundary (deliberate, load-bearing)

The owner asked for "full auto-implement + deploy". The loop honours that **for the safe
action space** (stage 4: data/curation, reversible, instantly live). It **does NOT**
autonomously write and CDK-deploy *new ingestion code* from a free-text question:
`AUTO_CODEGEN = False` is a hard constant, and code-requiring remediations become a
spec + issue for human approval + the existing CI/CD.

Rationale: an unattended "LLM writes arbitrary ingestion code → auto-deploy to prod" path,
triggered by untrusted free-text, is a self-inflicted supply-chain risk — a crafted
question could steer generated code straight into the production pipeline. Maximal
autonomy where it's safe (data), a human gate only at the ship-new-code-to-prod boundary.
This is reversible to widen later, but the default is safe.

## Consequences

- The tool now **learns its own blind spots**: every unanswered question is captured,
  triaged, and either auto-fixed (curation) or tracked to an issue (ingestion/discovery).
- Cost is trivial (one small S3 doc; remediation runs out-of-band). No new attack surface
  beyond the agent already having the digests bucket.
- The safe-autofix set grows naturally as new curated attributes/backfills land
  (ownership/ticker today; the ADR-013 recipe makes adding more cheap).

## Status / next steps

Implemented v1: capture (deployed), triage, safe-autofix + re-verify, gh issue, feed
surface. Next: (1) a dashboard "Lacunas" tab with a one-click "remediar" per gap; (2)
schedule `run_pipeline` (EventBridge) or run it as a pipeline step; (3) widen the
safe-autofix set (news_term fixes, enable-known-source); (4) an evidence-backed detector
path for `ingestion_gap` classes (still issue-gated for the code itself).
