# ADR 022 — Pilot findings: balancete/resultados soundness flow + local FinBERT-PT-BR

Date: 2026-09-06. Companion to [ADR 022](2026-09-06-adr-financial-soundness-prudential-ingestion.md).
Reproducible via [`scripts/pilot_soundness_finbert.py`](../scripts/pilot_soundness_finbert.py).

## Goal

Prove the ADR 022 flow end-to-end on real data before building the pipeline: (1) a soundness
**trajectory** from BCB structured financials, and (2) **FinBERT-PT-BR run locally** to extract a
per-entity financial **tone** — the two halves fused into one signal.

## What ran

- **Environment.** Python 3.14 / CPU. `torch 2.14.0+cpu` (cp314 wheel exists), `transformers 5.16.1`,
  `pandas 3.0.5`. FinBERT-PT-BR (`lucas-leme/FinBERT-PT-BR`, ~440 MB) downloaded and ran **locally** —
  no endpoint, no cloud call. This confirms §3 is buildable as a local/batch step.
- **Data.** Real BCB **IF.data** `Relatorio='T'`, four quarters **202506 → 202603** (1 yr), for five
  prudential conglomerates by their real `CodInst`: Itaú `C0010069`, Banco do Brasil `C0049906`,
  BTG `C0049944`, XP `C0052120`, Nubank `C0052058`. No fabricated numbers.
- **Half 1 — numeric trajectory.** Extracted Ativo Total / Carteira de Crédito / Depósitos per
  quarter → YoY moves + the **crédito/ativo** direction (the leading-indicator slope, §2/Tier B).
- **Half 2 — financial tone.** FinBERT-PT-BR scored deterministic pt-BR statements **of the real
  figures** (net tone = P(POSITIVE) − P(NEGATIVE) ∈ [−1, 1]).

## Results (real IF.data, 202506→202603)

| Entity | Ativo YoY | Crédito YoY | crédito/ativo Δ | FinBERT tone | Read |
|---|--:|--:|--:|--:|---|
| Nubank | +29.9% | +31.2% | +0.3 pp | **0.559** | credit-led expansion, exposure rising |
| BTG Pactual | +27.0% | +18.6% | −1.6 pp | 0.406 | fast growth, credit share easing |
| XP | +16.5% | +3.6% | −1.3 pp | 0.402 | asset growth, not credit-led |
| Itaú Unibanco | +9.4% | +6.3% | −1.1 pp | 0.406 | steady |
| Banco do Brasil | +6.8% | **−0.6%** | **−3.2 pp** | **−0.03** | deleveraging credit / defensive |

The trajectory **discriminates**: it separates the credit-led expanders (Nubank, BTG) from the
credit-deleveraging incumbent (Banco do Brasil), and the tone signal moves with the hard numbers
(highest for Nubank, flat/negative for BB) — the leading-indicator behaviour ADR 022 promised, on
real data.

## Smoke test — FinBERT discriminates tone correctly

- "lucro recorde e forte expansão da carteira de crédito" → **POSITIVE** 0.87 ✓
- "a inadimplência disparou e o índice de Basileia caiu abaixo do mínimo" → **NEGATIVE** 0.84 ✓
- "o patrimônio líquido permaneceu estável" → POSITIVE 0.86 ✗ (see caveat)

## Honest caveats (what the pilot did *not* prove)

1. **Quarterly, not monthly.** IF.data is quarterly, so this validates the trajectory *mechanics* and
   the FinBERT flow — **not** the monthly cadence. Production **Tier B** is the genuinely monthly
   COSIF **balancete** (doc 4010); wiring that source is the next step.
2. **Tone corpus is fact-paraphrases, not filings.** The scored sentences are deterministic
   statements of the real IF.data figures, used to validate the §3 **wiring**. In production FinBERT
   reads the **Pilar 3 / results-release TEXT**; that corpus still needs to be ingested and the model
   validated against it.
3. **The model leans positive on neutral phrasing** ("estável" → POSITIVE). This is exactly the
   pt-BR calibration risk ADR 022 flagged — hence the **shadow-first** rollout: compute and store the
   tone, but do not surface it until calibrated against real release language.
4. **Cost not yet measured.** Local CPU run only; the SageMaker Batch Transform per-run cost (§3/§4)
   is still to be measured on first cloud run — no figure is asserted.

## Follow-up: extracting Basileia / solvency + probing liquidity

Reproducible via [`scripts/pilot_basileia_solvency.py`](../scripts/pilot_basileia_solvency.py).

**Solvency IS fully extractable** from IF.data **Relatório 5 "Informações de Capital"**
(`TipoInstituicao=1`): Índice de Basileia, Capital Nível I (Tier 1), Capital Principal (CET1),
Razão de Alavancagem, Índice de Imobilização, plus the RWA breakdown (crédito/mercado/operacional).
Values are stored as fractions → ×100 for %. Band from the regulatory floor (8% + 2.5% conservation
buffer ≈ 10.5% practical min): `<10.5 frágil / <13 atenção / else sólido`.

Real trajectory, 202509→202603 (`Relatório 5`):

| Entity | Basileia % | Tier 1 % | CET1 % | Alav. % | Imob. % | Band | Basileia trajectory (Δpp/yr) |
|---|--:|--:|--:|--:|--:|---|---|
| BTG Pactual | 15.91 | 12.44 | 11.36 | 10.16 | 33.6 | sólido | 15.55→15.91 (**+0.36**) |
| Santander | 15.15 | 12.27 | 11.24 | 6.99 | 15.1 | sólido | 15.21→15.15 (−0.06) |
| Nubank | 15.10 | 12.77 | 11.27 | — | 5.6 | sólido | 14.59→15.10 (+0.51) |
| Bradesco | 14.90 | 11.97 | 10.15 | 6.29 | 27.7 | sólido | 15.85→14.90 (−0.95) |
| Itaú | 14.77 | 13.40 | 11.97 | 6.48 | 20.7 | sólido | 16.40→14.77 (**−1.63**) |
| XP | **11.93** | 10.01 | **7.17** | 3.67 | **47.0** | **atenção** | 13.94→11.93 (**−2.01**) |

Discriminates on solvency: **XP flags "atenção"** — Basileia 11.93% eroding −2.01pp/yr, CET1 only
7.17%, imobilização ~47% (near the 50% limit); Itaú is "sólido" but trending down −1.63pp. This is
the leading-indicator capital signal, on real data.

**Liquidity is NOT in IF.data.** Verified: the Relatório catalog has no liquidity report, and
Resumo/Capital carry no LCR/NSFR/liquidez column. True liquidity is published by BCB separately and
lives in the banks' **Pilar 3** reports → it comes from **ADR 022 Phase 6** (Pilar 3 PDFs via the
Bedrock+KB path) or a dedicated LCR source, *not* this flow. A balance-sheet liquidity **proxy**
(TVM + disponibilidades vs passivo exigível) is derivable but must be labelled inference, never
presented as the LCR.

## Conclusion

The ADR 022 approach is **validated as buildable and useful**: FinBERT-PT-BR runs locally on 3.14/CPU,
the soundness trajectory is real and discriminating, and the fused numeric+tone signal behaves as a
leading indicator. Green-light Phase 1–2 (Tier A ratios + Tier B monthly balancete), then §3 FinBERT
as the shadow Batch Transform step, per the ADR phasing.
