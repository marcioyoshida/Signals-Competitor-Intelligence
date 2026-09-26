# CPO Product Radar spike: findings (#158)

- **Date:** 2026-09-26
- **Window:** 2026-08-27 to 2026-09-26 (30 days)
- **Products:** Nubank, Inter, PicPay, Mercado Pago, C6 Bank
- **Code:** `scripts/spikes/cpo_radar/`. Read-only and offline. No infra, registry, DynamoDB writes or deploys.
- **Verdict:** **GO, at exactly the threshold (3/5).** Nubank, Inter and PicPay each have a hand-verified, in-window, CPO-actionable product change that Onça's feed did not surface. Mercado Pago and C6 have none. See §5 for why this is a narrow GO.

## 1. What was run

| Step | Script | Output (`out/`, git-ignored) |
|---|---|---|
| Subjects (aliases, iOS app IDs, official YouTube channels, fee-table CNPJs, Onça entity IDs) | `subjects.json` (checked in) | — |
| YouTube Data API: official-channel uploads (`playlistItems`) plus creator search (`search`, `q=<primary alias>`, `regionCode=BR`, `relevanceLanguage=pt`, 2×50 results), then hydration (`videos`) | `pull_youtube.py` | `youtube.json`, `youtube_quota.json` |
| Apple customer-reviews RSS (`sortby=mostrecent`, ≤10 pages) plus iTunes lookup (current version, release notes) | `pull_appstore.py` | `appstore.json`, `appstore_meta.json` |
| BCB fee tables (olinda OData, PF and PJ per CNPJ) | `pull_bcb_fees.py` | `bcb_fees.json` |
| Nova Lite classification in Bluefin's `analyze.py` shape, retargeted: `relevant / event / sentiment / feature / why` with official-account anchors, 10 items per call, 4 threads | `classify.py` | `classified.json`, `nova_usage.json` |
| Yield table plus per-product baseline: daily counts per (product, event); z-score against the product's own 30-day mean; spike = count ≥3 and z ≥2 | `baseline.py` | `yield.json`, `spikes.json` |
| Current-feed comparison: live `feed.json` from the dashboard bucket, plus 29 days of `onca-digests/narratives/` synced locally and grepped | manual | `feed.json`, `narratives/`, `feed_subject_cards.json` |

The YouTube key is Bluefin's `creatorradar/bluefin/api-key` (field `YOUTUBE_KEY`). It is loaded at runtime and is never printed or written anywhere. A grep of `out/` for key material returns 0 hits.

## 2. ADR-0003 admission verdict per source

Bluefin ADR-0003 checklist: (1) public or official API; (2) terms allow automated access at our cadence; (3) no PII beyond what is public; (4) provenance recorded.

| Source | 1 | 2 | 3 | 4 | Tier | Verdict |
|---|---|---|---|---|---|---|
| **YouTube Data API v3** | ✅ official API | ✅ under the YouTube API Services Terms. Quota is 10k units/day; a run costs ~1,018 (§6). API data that is stored must be refreshed or deleted within 30 days. | ✅ public channel/video metadata only; no comments pulled | ✅ `url`, `video_id`, `channel_id`, `fetched_at` | 🟢 Green-keyed | **ADMIT.** Condition: Onça gets **its own key/GCP project** for production. Bluefin's quota is shared with its impersonation detector (Bluefin ADR 0004), so borrowing it was acceptable for a spike only. |
| **Apple customer-reviews RSS** | ✅ public, keyless, published by Apple | ✅ at daily cadence (~30 requests/run, paced 1 s). ⚠️ The endpoint is legacy and undocumented, so it can be retired without notice. | ✅ the reviewer nickname is **deliberately not stored** (`author: null`) | ✅ `review_id`, `app_id`, `fetched_at` | 🟢 Green | **ADMIT, with a fragility caveat.** Hard cap of 500 most-recent reviews: PicPay hit the cap at 2026-08-28 (1 day short of the window), so a daily cadence is needed for high-volume apps. |
| **BCB fee tables** (`olinda.bcb.gov.br/olinda/servico/Informes_ListaTarifasPorInstituicaoFinanceira/versao/v1/odata/`) | ✅ official open data | ✅ | ✅ institutional data only | ✅ | 🟢 Green | **ADMIT, but not as a radar source.** The API is **live (HTTP 200, 2026-09-26)**. Sibling services `Informes_ListaValoresDeServicoBancario` and `Informes_ListaTarifaPorValores` are also live. Yield in the window was **0**, see §3. Use it as a monthly diff inside the existing pricing lens. |
| Google Play | — | — | — | — | 🟡 Amber | Out of scope by spec. There is no official API for other companies' apps. |

## 3. Yield per product × source

"Relevant" is the Nova Lite verdict. "Change-tagged" counts relevant items tagged `launch`, `feature`, `price` or `outage`. "Verified events" counts **distinct** real product changes confirmed by hand (§4), whether or not Onça already had them.

| Product | Source | Mentions | Relevant % | Change-tagged | Verified events |
|---|---|---:|---:|---:|---|
| Nubank | YouTube (26 official + 93 creator) | 119 | 84.0 | 83 | Ultravioleta Protegido (free protection cut); NuCoin relaunch |
| Nubank | App Store | 274 | 98.2 | 112 | Pix outage 2026-09-04 (23 low-star Pix reviews that day vs ~1/day) |
| Nubank | BCB fees | 1 row | — | 0 in window | latest `DataVigencia` 2021-01-09 |
| Inter | YouTube (6 official + 98 creator) | 104 | 96.2 | 68 | Priority Pass restricted to lounges; BDR programme discontinued (official channel) |
| Inter | App Store | 300 | 99.3 | 79 | **iOS v26.16 login outage 2026-09-23** (49 reviews ≤2★ on 09-23, 44 on v26.16; surrounding days 1–9/day) |
| Inter | BCB fees | 93 rows | — | 0 in window | latest 2024-07-27 |
| PicPay | YouTube (0 official in window + 96 creator) | 96 | 85.4 | 70 | **Central de Cashback** (cashback → card limit, 102% CDI) |
| PicPay | App Store | 500 (cap) | 99.4 | 7 | none; complaint days 09-10 and 09-20 are diffuse (support, unrequested insurance), with no single cause |
| PicPay | BCB fees | 74 rows | — | 0 in window | latest 2023-12-06 |
| Mercado Pago | YouTube (0 official in window + 100 creator) | 100 | 95.0 | 78 | none verified (see §4 misses) |
| Mercado Pago | App Store | 161 | 96.9 | 31 | none; the Apple Pay gap is a persistent request, not a change |
| Mercado Pago | BCB fees | 0 rows | — | — | the IP and SCFI publish no fee rows |
| C6 | YouTube (17 official + 85 creator) | 102 | 85.3 | 58 | Alymente acquisition (entry into benefits) |
| C6 | App Store | 45 | 100 | 11 | none; the 2026-09-10 login/update cluster is only 4 reviews |
| C6 | BCB fees | 84 rows | — | 0 in window | latest 2026-06-08 |

**Precision caveat.** Nova Lite's `relevant` rate is high (84–99%), but `feature` is badly over-assigned on YouTube. Creator tutorials ("como aumentar limite", "como pagar boleto") and brand content (Nubank Parque ads with 14M views) get tagged `feature`. Of 357 change-tagged YouTube items, only about **8 distinct real changes** exist, so the precision of the change tag is in the single digits. There is also a geography leak: 3 Spanish-language Mexico/Argentina Mercado Pago videos were marked relevant. A production lens needs (a) a "new change vs evergreen how-to" gate in the prompt, (b) language/region filters, and (c) event clustering across creators. The baseline spike detector (App Store `outage` z = 5.1 for Nubank on 09-04 and z = 5.35 for Inter on 09-23) is what surfaced both outages. It was the most reliable signal in the spike.

## 4. Hand-verified hits vs the current feed

Onça's comparison baseline is the live `feed.json` (generated 2026-09-26) **plus** every narrative in `onca-digests/narratives/2026-08-27 … 2026-09-26`. The feed itself only carries 14 days. I grepped the narratives for event keywords and read the Nubank, Inter and PicPay narratives for 09-22 to 09-26 in full.

### Counting hits: in window, CPO-actionable, verified, NOT surfaced by Onça

| # | Product | Event (date) | Radar source | Hand verification | Onça feed |
|---|---|---|---|---|---|
| 1 | **Nubank** | Free Ultravioleta digital-transaction protection **ended**; replaced by paid **Ultravioleta Protegido**, R$ 19,99/month. Rollout 2026-09-23/24; old protection ends by October. | YouTube creator, 2026-09-24: https://www.youtube.com/watch?v=bgYZNFwgzAI ("Email Nubank Confirmou Novo Corte! O Fim da Proteção no Ultravioleta") | The video title names the change but the description has no detail, so I corroborated it independently. Press dated 2026-09-23 (https://newsrondonia.com.br/economia/2026/09/23/nubank-passa-a-cobrar-seguro-que-era-beneficio-do-ultravioleta/) confirms product name, price, coverage and rollout date. | **Not surfaced.** `ultravioleta`, `protegido`, `19,99` and `proteção de transações` each have 0 hits in feed and narratives. |
| 2 | **Inter** | **iOS app v26.16 locked users out** on 2026-09-23: login failure with error **AL-903** / "Test Mode em produção". Hotfix v26.16.1 released the same day at 20:02Z. | Apple reviews RSS for app 839711154: https://itunes.apple.com/br/rss/customerreviews/page=1/id=839711154/sortby=mostrecent/json (store page https://apps.apple.com/br/app/id839711154) | Read the reviews for that date. There are 49 reviews rated ≤2★ that day (44 on v26.16), against 1–9 on each surrounding day. 16 describe being unable to log in, 15 blame the update, and only 2 quote "test mode" / AL-903 verbatim (counts rechecked by the parent session against `out/appstore.json`), e.g. "Após a atualização 26.16 não consigo mais logar". iTunes lookup confirms `currentVersionReleaseDate` 2026-09-23T20:02:05Z for 26.16.1, and a later review reads "App voltou a funcionar após a atualização". No press coverage was found, so the reviews are the primary evidence. | **Not surfaced.** Inter narratives for 09-23 to 09-26 cover BDRs, an event and CEO remarks; `test mode` and `AL-903` have 0 hits. |
| 3 | **PicPay** | **Central de Cashback** launched. It pools cashback in a cofrinho earning 102% CDI, which can be converted to **PicPay Card limit**, LATAM Pass miles or subscriptions. Announced 2026-09-22; miles conversion from 09-15, gradual rollout. | YouTube creator, 2026-09-26: https://www.youtube.com/watch?v=DT8-ZFSdMWM | The description matches the product mechanics. Independent press dated 2026-09-22 with a quote from PicPay VP Alexandre Moshe: https://www.letsmoney.com.br/fintech/picpay-central-cashback/ | **Not surfaced.** `central de cashback` has 0 hits. PicPay narratives for 09-22 to 09-26 cover BCB rates, insurance, Pix savings and the stock. |

**Count: 3/5 products** (Nubank, Inter, PicPay).

### Verified, but Onça already had them (not counted)

| Product | Event | Where Onça had it |
|---|---|---|
| Nubank | Pix outage 2026-09-04 (App Store spike, z = 5.1) | narratives 2026-09-05 and 09-06 `cand-ent-nubank` ("instabilidades no pix … cerca de mil reclamações") |
| Nubank | NuCoin loyalty relaunch: 5 tiers, weekly draws per R$100 on credit. Official blog 2026-09-23: https://blog.nubank.com.br/nucoin-programa-de-beneficios-do-nubank/ | **Partially.** 09-23 narrative has "promessa de premiar a fidelidade"; 09-24 has "sorteio de R$ 500 mil para cada R$ 100 em crédito". Garbled and not named, but present, so conservatively **not counted**. |
| Inter | Priority Pass restricted to lounges only (Prime/Win), ~2026-09-21 | narrative 2026-09-22 `cand-ent-inter` |
| Inter | BDR programme discontinued (official channel videos 09-11/14/15) | SEC-EDGAR-driven narratives from 2026-09-03 onward |
| Mercado Pago | Cofrinho 140% CDI | narrative 2026-09-03, plus an August narrative. The launch **predates the window**. |
| C6 | Alymente acquisition, entry into meal/food benefits | narratives 2026-09-03 and 09-05 |

### Candidates that failed verification (count as misses)

| Product | Candidate | Why it fails |
|---|---|---|
| Mercado Pago | Credit-limit **cut wave** (≥7 creator videos, 09-02 to 09-15, reporting an in-app "redução de limite" notice) | Only creators and individual Reclame Aqui cases report it. MP describes limit review as continuous and individual, and I found no official or press confirmation of a policy change. Onça's feed says the opposite ("ampliando o limite", 09-26). Plausible but unproven, so it is a miss. |
| Mercado Pago | "Nova linha de crédito para comprar no ML" (09-07, 09-10) | The video descriptions contain only referral links, with nothing verifiable. |
| Mercado Pago | Card-statement UX regression in v2.454.1 (history and future invoices removed) | Only 2 reviews (09-20, 09-21). A real signal, but not a spike. |
| C6 | Release notes v2.162.2 (2026-09-24): Pix Automático management; PJ "novidades para receber" | Official, but generic. Pix Automático is an industry rail from 2025, and the note doesn't say what changed. Not CPO-actionable as written. |
| C6 | App login/update failures 2026-09-10 (z = 4.8) | 4 reviews in total, below any sensible alert floor. |
| C6 | "Tag C6: novas regras e custos" (09-24) | A tutorial; no dated change is identified. |
| Inter | Poupança Mais Limite +10% bonus ends 2026-10-13 (creator, 09-22) | Creator claim only. A web search found only undated product pages describing the 10% and no official notice. Unverified. |
| PicPay | Epic card forced upgrade, R$ 49,90/month, zero limit (09-10) | Complaint narrative based on Reclame Aqui screenshots; no dated product change confirmed. |

## 5. Why this is a narrow GO

- **3/5 meets the bar with no margin.** One of the three hits (PicPay) was caught by the radar 4 days after the press release, and only by a single referral-driven creator channel.
- **The two sources earn their keep in different ways.** App Store reviews plus the per-product baseline gave the cleanest, earliest, primary-source signal: a login outage that no news source covered. YouTube creators gave coverage of pricing and benefit changes (Ultravioleta, Priority Pass, cashback), but with heavy noise.
- **Onça's Google-News-driven narratives already catch more than expected.** Six verified events were already in the feed. The radar's incremental value is concentrated in (a) **app-quality incidents** and (b) **benefit/pricing fine print** that trade press ignores.
- **Mercado Pago and C6 are weak for this lens.** Neither official channel posted product content in the window (MP BR: 0 uploads; C6: macro and brand shows). For MP, creator coverage is dominated by limit folklore and referral links.
- **BCB fee tables add nothing at 30-day resolution.** The latest effective date for any subject is 2026-06-08, and Nubank and Mercado Pago have no rows at all.

## 6. Cost per run

| Item | Per run |
|---|---|
| YouTube Data API | **1,018 units**: search 1,000 (5 × 2 pages × 100), playlistItems 5, videos 13. About 10% of the default daily quota. One-time subject setup cost ~210 more (handle and channel lookups). |
| Nova Lite (`amazon.nova-lite-v1:0`, on-demand, us-east-1) | **184 Converse calls, 199,711 input + 91,449 output tokens ≈ US$ 0.034** for 1,801 mentions. 0 failed batches; 14 items unscored (partial JSON). |
| Apple RSS + iTunes lookup | free: 28 RSS pages + 5 lookups |
| BCB olinda | free: 18 calls |
| S3 reads (feed + narratives, ~9.6 MB) | negligible |

At a daily cadence this comes to about US$ 1/month in Nova plus a free YouTube quota. The binding constraint is the YouTube quota, not money.

## 7. Follow-up (draft issue for the lens, not filed)

The GO rule calls for a follow-up issue. It is **drafted here and not filed**, pending the owner's call on a 3/5 result.

> **CPO Product Radar lens: ingester, /exec CPO panel, weekly digest**
> - Ingester `cpo_radar` (daily): Apple reviews RSS (daily, to beat the 500-review cap), YouTube official uploads (cheap) and creator search (≤1k units/day, **own key**). Subjects come from the registry, starting with the 5 products in `scripts/spikes/cpo_radar/subjects.json`.
> - Classifier: keep the Bluefin shape, but add an `is_new_change` gate (evergreen tutorial vs dated change), a language/region filter, and cluster multiple creators into one event before it reaches a card.
> - Baseline: per-product daily counts by event; alert on App Store `outage`/`complaint` with count ≥10 and z ≥3, carrying the dominant app version and the error strings.
> - Surface: a `product_radar` panel in `executive.build_cpo` (events with source links) and a weekly CPO digest; `/api/ask` grounding over the same cards.
> - Explicitly not included: BCB fee tables as a radar source (keep them in the pricing lens monthly), Google Play (Amber), and impersonation (a separate CRO/CISO issue; the spike found 6 zero-subscriber "C6 Bank: Cartão, Conta e Mais!" channels created 2025-07-16).
> - Re-measure after 30 days live. If fewer than 3/5 products produce an unsurfaced verified hit again, retire the YouTube creator leg and keep App Store only.

## 8. Reproduce

```bash
cd scripts/spikes/cpo_radar
PY=../../../.venv/bin/python   # AWS_PROFILE=my2027 (default in common.py)
$PY pull_youtube.py && $PY pull_appstore.py && $PY pull_bcb_fees.py
$PY classify.py && $PY baseline.py
aws s3 cp s3://oncaprototypestack-oncadashboardsitefdbca924-eu9minmw6ljv/feed.json out/feed.json
aws s3 sync s3://onca-digests-668449743071/narratives/ out/narratives/ --exclude "*" --include "2026-08-2[7-9]/*" --include "2026-08-3*" --include "2026-09-*"
```
