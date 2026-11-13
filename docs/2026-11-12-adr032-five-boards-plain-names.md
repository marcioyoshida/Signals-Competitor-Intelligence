# ADR 032: Five boards with plain names (Estratégia, Risco, Compliance, Produto, Regulatório)

- **Status:** Accepted (owner, 2026-11-12)
- **Amends:** ADR 020 (officer personas) and ADR 021 (Executive Flow, officer dashboards)

## Context

The /exec dashboard had four boards named by C-suite acronyms (CSO, CRO, CCO, CPO), and that
caused two problems:

1. **The acronyms misread.** In a Brazilian bank, "CRO" means *Chief Risk Officer*, the
   risk-management head under CMN 4.557. Onça's CRO was the *regulatory* board (`officers.py`
   role `regulator`). A risk head would assume the board was theirs, then find rule-making on it.
2. **Supervision outcomes sat on the wrong board.** The compliance band "Fiscalização & sanções"
   mixed two different things:
   - resolution regimes (liquidação, intervenção, RAET, cassação), which are prudential
     outcomes;
   - conduct penalties (BCB PAS, CVM sancionador, SPA, Procon, CEIS/CNEP).

   Under CMN 4.595/2017, compliance risk is the risk of a sanction for non-compliance, so only
   the second kind is compliance. The first is risk.

## Decision

Users see **five boards with plain names**, in this order:

| Board | Internal id | What it holds |
|---|---|---|
| Estratégia | `cso` | climate, threats and opportunities, competitive map, momentum, financial strength, Porter, Four Corners, **PESTLE** (moved from Compliance) |
| Risco *(new)* | `risk` | prudential soundness (Basel, LCR, PDD, NPL, fragility; moved from Regulatório), **resolution regimes** (from Compliance), insolvency and distressed counterparties, **Pix data incidents** (operational risk) |
| Compliance | `cco` | **conduct sanctions** (enforcement minus `RESOLUTION_KINDS`), **BCB PAS sanctions** (moved from Regulatório), international sanctions lists, Ibis crypto partners, integrity, reputation |
| Produto | `cpo` | unchanged |
| Regulatório | `cro` | sector events, international standards, impact and reach, timeline, **article-level diff** (moved from Compliance), deadlines and open BCB consultations |

- **Internal ids stay.** The decision log, KB precedents, act API, push preferences and
  `feed.executive` blocks keep `cso`, `cro`, `cco` and `cpo`, so history and integrations keep
  working. Only `risk` is new, and it uses the same id as its role name.
- **No acronyms in user-facing text.** This covers tabs, titles, officer personas, digest
  subjects, public pages and the URL hash, which now uses `#estrategia`, `#risco`,
  `#compliance`, `#produto` and `#regulatorio`. The old `#cso`/`#cro`/`#cco`/`#cpo` links still
  resolve.
- **Executive Flow routing.** Confirmed insolvency and official resolution regimes route to
  Risco, which hands them off to Compliance for the entity flag. Conduct enforcement stays on
  Compliance.
- **Risco headline.** It counts distinct *institutions* that are under a BCB regime, in
  insolvency, or composite-fragile. One institution in two of these states counts once.

## Consequences

- **Ask and act APIs.** Both accept `risk`, and the chief-of-staff router sends solvency,
  liquidity, regime and insolvency questions to it. Compliance's cues no longer include
  `risco` or `distress`.
- **Tenant scoping.** `_rescope_executive` rebuilds the whole block from the scoped feed, so the
  new block is tenant-scoped without extra code.
- **Lovable mirror.** The mirror of /exec (`on-a-landing`) still shows four boards with
  acronyms, and needs the same change before the next design round.
