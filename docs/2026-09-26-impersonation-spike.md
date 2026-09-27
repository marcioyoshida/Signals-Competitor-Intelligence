# Brand-impersonation radar spike: findings (#160)

- **Date:** 2026-09-26
- **Window:** 2026-08-27 to 2026-09-26 (30 days)
- **Issuers:** Nubank, Inter, PicPay, Mercado Pago, C6 Bank (the five retail issuers in `scripts/spikes/cpo_radar/subjects.json`)
- **Code:** `scripts/spikes/impersonation/`. Read-only: no AWS writes, no infra, no deploys. Nothing was reported, flagged or contacted.
- **Verdict:** **NO-GO.** The spike found **0 hand-verified active impersonations** across the 5 issuers in the window. The kill criterion, fixed in the issue before any code was written, is "fewer than 2 → no-go".
- **C6 cluster:** it is **dormant, not an alert.** The evidence strongly favours machine-created channels that copy the app-store listing, which fits Google Ads' Google-owned auto-video channels. No channel in the cluster carries any harm signal. The one part that is not proven: Google does not document how it names these channels, and unlisted ad videos cannot be seen through the public API (§2).

## 1. What was run

| Step | Script | Output (`out/`, git-ignored) | YouTube units |
|---|---|---|---:|
| Alert rule, written **before** any data pull | `rules.py` | — | 0 |
| C6 triage: `search type=channel` on the exact store titles of all 5 issuers plus 3 controls (iFood, Shopee, Kwai); `channels.list` (snippet, statistics, brandingSettings, status, topicDetails, contentDetails); uploads-playlist read for every look-alike. The PicPay Google Play title was searched too. | `triage_c6.py` (PicPay Play title in `sweep_recent.py`) | `triage.json` | 1,160 |
| Look-alike and harm scan per issuer: channel search on the brand and on `"<brand> suporte"`, plus two in-window video searches on offer terms. Hydrate every channel, apply rule L, read uploads, hydrate in-window videos, apply H1–H4 and W. | `scan_harm.py` | `scan.json` | 2,123 |
| Second sweep: newest in-window videos per brand (`order=date`), to catch new look-alikes that relevance ranking buries | `sweep_recent.py` | `sweep_recent.json` | 507 |
| Hand verification: fresh channel and video fetch for every rule proposal and every support-named look-alike | `verify.py` | `verify.json` | 18 |
| App Store look-alike apps (iTunes Search API, `country=br`, limit 200 per term) | `appstore_lookalikes.py` | `appstore_lookalikes.json` | 0 (keyless) |
| Quota meter: every call is logged before it is sent, with a hard stop at 5,000 | `common.py` | `quota.json` | — |

The key is Onça's own: Secrets Manager `signalscompetitor/onca/api-key`, field `YOUTUBE_API_KEY`. It is loaded at runtime and kept only in memory. A grep of `out/` for key material returns 0 hits. Nova Lite was **not used**: the deterministic rule plus hand review was enough at this volume.

## 2. Triage verdict: the C6 cluster

**Hypothesis under test (benign):** Google Ads App campaigns auto-create a YouTube channel named after the app.

### Evidence

1. **The pattern is not specific to C6, or to banks.** "Exact title" means a non-official channel whose normalised title equals the store title.

   | Store title searched | Results | Exact-title channels | Of those, 0 subs / 0 videos / 0 views | Creation dates (top) |
   |---|---:|---:|---:|---|
   | C6 Bank: Cartão, conta e mais! (2 pages) | 100 | **58** | 58 | 2025-07-16 (31), 09-08 (5), 05-09 (4), 09-16 (4) |
   | Inter: Conta, Cartão e Pix | 50 | **32** | 29 (3 have 1 subscriber, still 0 videos) | 2025-09-09 (8), 05-02 (4), 09-10 (3), 05-09 (3) |
   | Mercado Pago: banco digital | 50 | **22** | 22 | 2025-07-12 (7), 07-17 (5), 02-28 (3), 02-05 (3) |
   | Nubank: Conta, Cartão e mais | 50 | 4 | 4 | 2025-02-18 (3), 06-09 (1) |
   | PicPay: Conta, Cartão e Pix | 50 | 3 | 2 (1 has 1 subscriber) | 2025-05-02 (3) |
   | Banco PicPay: Cartão, Pix e + (Play title) | 50 | 0 | — | — |
   | **Control:** iFood: pedir delivery em casa | 50 | **25** | 24 (1 has 1 subscriber) | 2025-09-03 (5), 11-27 (4), 11-07 (3) |
   | **Control:** Shopee: Compre de Tudo Online | 50 | 4 | 3 | 2025-03-19 (2), 08-11 (2) |
   | **Control:** Kwai - Vídeo & Bônus Diário | 3 | 0 | — | — |

   Unrelated large apps show the same shape, with iFood at 25 channels. This is what you would expect from an ad-platform process that runs for any app buying installs. It is not what you would expect from a fraud ring aimed at banks.

2. **The titles copy the store listings exactly, including each store's casing.** Google Play titles were read on 2026-09-26 as a one-off manual check:
   - **C6:** 20 channels are titled `C6 Bank: Cartão, conta e mais!`, which is the **App Store** casing. 38 are titled `C6 Bank: Cartão, Conta e Mais!`, which is the **Google Play** title verbatim. Both casings are spread across the same date range.
   - **Nubank:** all 4 are titled `Nubank: conta, cartão e mais`. That is the **Play** title verbatim; the App Store title is `Nubank: Conta, Cartão e mais`.
   - **Mercado Pago:** all 22 are titled `Mercado Pago: banco digital`, which is the App Store casing (Play has `Banco Digital`).
   - **No variations:** across all 8 searches there are 148 exact-title channels and 252 other results. **None of the 252 other results has both 0 subscribers and 0 videos.** Every "empty" channel carries a byte-exact store title. People who set up look-alikes by hand produce typos, spacing and emoji variations; these channels have none.

3. **The channels are completely empty in the same way.** All 148 exact-title channels share these properties: empty `description`, no handle (`customUrl`), no `country`, no `defaultLanguage`, no banner, `brandingSettings.channel` holding **only** `title`, no `topicDetails`, and an identical `status` (`isLinked: true`, `privacyStatus: public`). None of them has a public uploads playlist: `playlistItems.list` on `contentDetails.relatedPlaylists.uploads` returns **HTTP 404 for all 148**.

4. **They were created in batches** on shared dates, across issuers and controls alike (for example, C6 31 on 2025-07-16 and iFood 5 on 2025-09-03). All of them date from 2025, with none from 2026.

5. **Google documentation.** Google Ads Help, "About auto-generated video ads" (https://support.google.com/google-ads/answer/16430641), says, quoting: *"When the system first creates your auto-generated videos, it also creates a Google-owned channel to house all of your auto-generated videos."* It also says: *"This channel is separate from any advertiser-owned YouTube channel that may be linked to your Google Ads account"* and *"You won't have the ability to manage or control this channel."* Its availability table lists **App campaigns**, Performance Max and Demand Gen. A related page describes a Google-managed "House channel" for Performance Max (https://support.google.com/google-ads/answer/14528532).

   **Not found:** I found **no** Google documentation that says the channel is **named after the app** or that the videos are **unlisted**. A search-engine summary claimed they are unlisted, but the page I fetched does not say so. The naming link is inferred from evidence 2, not documented.

### What stays unverified

- **Whether ads reference unlisted uploads on these channels.** The public API cannot show unlisted videos, so the 404 on the uploads playlist is consistent with "only unlisted videos" and also with "no videos at all". The way to close this is a manual check of C6's advertiser page in the Google Ads Transparency Center, which was not done.
- **Whether any channel is linked to the official brand account.** No public API field exposes that link. `isLinked: true` means the channel is tied to a Google account, not to the brand.

### Verdict

**Dormant auto-created look-alikes, not impersonation.** The benign hypothesis explains every observation: the controls, the dual-store casing, the identical empty metadata, the batch dates, and the documented Google-owned channel for App-campaign auto-videos. No channel in the cluster has a single harm signal. A production detector should **suppress** the pattern: exact store title, 0/0/0, no description, no handle, uploads playlist 404.

## 3. Alert rule (fixed before the data pull, `rules.py`)

A non-official channel is an **active impersonation** only if **all** of the following hold:

- **L, look-alike.** The channel is not on the issuer's official allowlist, **and** its title or handle contains a brand alias (ignoring accents, case and spaces), or its name similarity to the official channel or App Store title is at least 0.75.
- **H, at least one active harm signal:**
  - **H1:** offer terms in the channel description or in an in-window video: *suporte, atendimento, central de ajuda/atendimento, desbloqueio, empréstimo, fale conosco, recuperar conta, liberar limite/conta/pix, pix na hora, receba via pix, chave pix*.
  - **H2:** a phone or WhatsApp number, or a `wa.me` link.
  - **H3:** a link to a domain that is neither the issuer's official domain nor a mainstream platform. Link shorteners count.
  - **H4:** an upload surge: 3 or more uploads in the window from a channel that had none before, or that was created in the window.
- **W, in the window.** The harm-bearing video was published in the window, or the channel was created in it. A description-only signal on an older channel is reported as "undated" and **not counted**.
- **V, hand-verified.** A fresh API fetch shows that the channel **presents itself as the issuer or its support**, not as an independent creator or reviewer, and the harm signal is quoted from that fetch.

Anything that meets L without H + W + V is a **look-alike**. A look-alike with 0 subscribers and 0 videos is **dormant**.

## 4. Yield per issuer (YouTube)

Look-alikes are the union from triage, scan and sweep. "Rule proposals" are channels that passed L+H+W mechanically. "Verified" means they also passed V.

| Issuer | Look-alikes | of which dormant | Look-alikes with a harm signal (any date) | Rule proposals (L+H+W) | **Verified active impersonations** |
|---|---:|---:|---:|---:|---:|
| Nubank | 48 | 6 | 3 | 2 | **0** |
| Inter | 35 | 29 | 0 | 0 | **0** |
| PicPay | 24 | 7 | 1 | 0 | **0** |
| Mercado Pago | 67 | 22 | 8 | 1 | **0** |
| C6 Bank | 59 | 58 | 0 | 0 | **0** |
| **Total** | **233** | **122** | **12** | **3** | **0** |

### The 3 rule proposals: all rejected at V

| Channel | Why the rule fired | Hand verification (fresh `channels` + `playlistItems` + `videos` fetch) | Result |
|---|---|---|---|
| `UCFblyM901GXIQ5QCEfg92JA` "Nubank Codigo" (@nubankcodigo, created 2026-09-20) | H4: 5 uploads on the day it was created | All 5 videos are titled `"20 de setembro de 2026"`, with empty descriptions and category 22 (People & Blogs). The thumbnails are personal shorts (a football player, a child's selfie). The channel makes no claim to Nubank beyond its name, and there is no offer, contact or link. | Look-alike name, **not** an impersonation. The thumbnails were deleted after viewing. |
| `UCXTh0lOSA5iWg1UhFivy7yw` "SUPORTE_NUBANK" (@suporte_nubank) | H1 `"Suporte"` in the in-window video `"Suporte nubank warzone"` (2026-09-18) | The other in-window videos are `"Melhores Momentos Suporte_Nubank Warzone"` and `"SUPORTE_NUBANK RECEBENDO ELOGIOS"`. This is a Warzone gamer tag, with an empty description. | Look-alike name, **not** an impersonation. |
| `UCHDvjDAgeRS7ZYgZLeAYiRQ` "MUNDO DIGITAL MP" (12.3k subs) | H2 `"WhatsApp"` and H3 `mpago.li/…` in in-window videos | Its self-description is independent: *"O canal MUNDO DIGITAL MP foi criado com o intuito de ajudar e fornecer mais informações"*. It covers Bradesco, DMCard, Nubank and Recarga Pay too. "WhatsApp" occurs in MP's own product copy (*"Venda por WhatsApp e redes sociais"*), and `mpago.li` / `mpago.la` are **Mercado Pago's own referral shorteners**. | A creator using referral links. **Not** an impersonation. The fire was a rule false positive. |

### Look-alikes worth noting that the rule did not count

These are not impersonations by the rule, and are listed here for completeness.

- `UCMhQi8xf5w0Q-W_riKapp5w` "nubank Pix" (created 2026-03-23): its videos are titled *"O NUBANK VAI TE PAGAR TODO MÊS R$ 975.45"* and *"LUCRE COM O SIS SISTEMA"*. This is scam-flavoured and claims the brand in its name, but it has 0 subscribers, 4 views, and all uploads on 2026-03-23. That is **outside the window**, and no H1–H3 term matches.
- `UCjF38SrSm1WfCKjentE1rsw` "Suporte Nubank" (created 2026-04-22): a name that claims support, with 16 uploads titled only with dates, the last on 2026-06-20. There is no in-window content and no description.
- **Mercado Pago:** several channels from 2017–2020 (for example `UCjUeKuaP4tZzG-206nsAhUA`, `UCvr6bfb4IKWbmeA-Wmt2wVg`) have descriptions that sell "Maquininha" with `goo.gl` or `bit.ly` links. These are reseller or affiliate pages with no in-window activity, so they are undated look-alikes.
- **Official sub-brands missing from the allowlist:** "Nubank Ultravioleta" (`UCVxYmVbcXxf-0HTJSlXW7yw`, 619k subscribers, links only to `nubank.com.br`), "Building Nubank" and "Nubank Parque". These are Nubank's own channels. As in Bluefin ADR 0015, the official set has to hold **several accounts per issuer**, or the detector accuses the brand's own channels.

### Precision lessons for any future rule

1. Official domain lists must include the issuer's shorteners (`mpago.li`, `mpago.la`) and careers or blog domains.
2. H1 needs the offer to be addressed to the viewer. A gamer tag or product copy that contains "suporte" or "WhatsApp" is not an offer.
3. A channel title that claims support ("Suporte <brand>") should feed H1. The fixed rule reads only descriptions and videos. This gap did not change the count here, because both such channels had no in-window content.
4. Store-title auto-channels need explicit suppression (§2).

## 5. App Store look-alike apps (iTunes Search API)

| Issuer | Apps searched | Look-alike apps (brand in name or seller, non-official seller) | In window | Harm |
|---|---:|---:|---:|---|
| Nubank | 168 | 0 | 0 | — |
| Inter | 153 | 1: `6701993119` "Pay Inter" by PAY INTER SERVICOS LTDA (Finance, released 2024-09-25, 0 ratings) | 0 | none. A namesake company, not a look-alike of Banco Inter. |
| PicPay | 87 | 0 | 0 | — |
| Mercado Pago | 170 | 0 | 0 | — |
| C6 | 162 | 0 | 0 | — |

The only apps in the Brazilian App Store carrying these brands are published by the official sellers. That includes C6 Yellow, PicPay Empresas and Inter Empresas. Apple's review process appears to keep this surface clean. **Yield: 0.**

## 6. Admission verdicts (Bluefin ADR-0003 checklist)

The checklist asks four things: (1) is the source public or an official API; (2) do its terms allow automated access at our cadence; (3) does it avoid PII beyond what is public; (4) is provenance recorded.

| Source | 1 | 2 | 3 | 4 | Tier | Verdict |
|---|---|---|---|---|---|---|
| **YouTube Data API v3** (Onça key) | ✅ | ✅ within quota. The 30-day refresh-or-delete rule applies to stored data. | ✅ public channel and video metadata only | ✅ `fetched_at`, IDs | 🟢 Green-keyed | **Admissible, but not worth running for this lens.** Yield was 0 verified, and a daily scan would cost about 2,000 units: 4 searches per issuer, each 100 units. That is 20% of the key's quota, on top of the CPO radar's ~510. |
| **iTunes Search API** | ✅ documented Apple API, keyless | ✅ at about 10 calls per run, paced at 1 s | ✅ app metadata only | ✅ | 🟢 Green | **Admissible, yield 0.** |
| Google Play store pages | ⚠️ no official API for third-party apps | ❌ | — | — | 🟡 Amber | **Not admitted.** Seven public listing pages were read once, by hand, only to confirm the Play titles for the triage. |
| **CERT.br** (https://stats.cert.br/phishing/) | ✅ | — | — | — | — | **No feed exists.** It publishes aggregate statistics by sector only, with no URLs and no per-brand breakdown. |
| **OpenPhish community feed** (https://openphish.com/terms.html) | ✅ public | ❌ the terms prohibit commercial use, *"including … threat intelligence, detection … customer protection"* | ✅ | ✅ | 🔴 | **Reject.** The commercial feed is paid. |
| **PhishTank** (https://www.phishtank.com/register.php) | ✅ | ❌ new-user registration has been closed since 2020, so no key can be obtained | — | — | 🔴 | **Reject (unavailable).** |
| **Google Safe Browsing API** (https://developers.google.com/safe-browsing/v4/usage-limits) | ✅ | ❌ *"for non-commercial use only"* | ✅ | ✅ | 🔴 | **Reject.** The commercial alternative, Web Risk, is a paid lookup service, not a feed of new look-alike domains. It was not tested. |
| Certificate Transparency logs (for example crt.sh) | ✅ public | ✅ | ✅ | ✅ | 🟢 | **Not tested.** It is public and admissible for *discovering look-alike domains*, but it is not a phishing feed and would need the same L+H rule. The third-party GitHub "BR-Phishing-Feed" (CT plus LLM filtering) is unofficial and would not be admitted as-is. |

**Conclusion: no official phishing-domain feed is admissible.** Every free feed found is non-commercial or closed, and CERT.br publishes statistics only.

## 7. Quota and cost

| Item | Spike total |
|---|---|
| YouTube Data API | **3,808 units** (cap 5,000): 35 `search` = 3,500, 255 `playlistItems` = 255, 33 `channels` = 33, 20 `videos` = 20. By phase: triage 1,160, scan 2,123, sweep 507, verification 18. The run finished well before the CPO radar's 08:30 UTC slot needs its ~510. |
| iTunes Search / lookup | free (about 20 calls) |
| Nova Lite | **not used: 0 tokens, US$ 0** |
| AWS | one Secrets Manager read, with no writes |

Estimated steady-state cost if the lens were built: about 2,000 YouTube units/day for 5 issuers, plus about 100–300 for hydration and uploads. The spend is quota, not money, and it would compete directly with the CPO radar on a 10k/day key.

## 8. GO / NO-GO

**NO-GO.** The kill criterion was stated in #160 before the build: *stop if, across the 5 retail issuers, fewer than 2 active impersonations (by the rule above) are found in 30 days.* The spike found **0**:

- **233 look-alike channels** in total. 122 are dormant; the large clusters for C6, Inter and Mercado Pago are store-title auto-channels (§2).
- **3 mechanical rule proposals**, all rejected at hand verification: a gamer tag, a kid's shorts channel and a referral creator.
- **0 look-alike apps** on the App Store.
- **No admissible phishing-domain feed.**

The criterion is not moved. The CRO `/exec` panel and the new-impersonation alert from the issue's "if go" branch are **not** recommended.

What would reopen this: evidence of impersonation happening on surfaces this spike could not reach, such as Instagram, WhatsApp, Google Play or look-alike domains. The CT-log domain route (§6) is the only admissible candidate identified. It would need its own spike with the same fixed kill criterion.

## 9. Reproduce

```bash
cd scripts/spikes/impersonation
PY=../../../.venv/bin/python        # AWS_PROFILE=my2027 (default in common.py)
$PY triage_c6.py && $PY scan_harm.py && $PY sweep_recent.py
$PY verify.py UCFblyM901GXIQ5QCEfg92JA UCXTh0lOSA5iWg1UhFivy7yw UCHDvjDAgeRS7ZYgZLeAYiRQ UCVxYmVbcXxf-0HTJSlXW7yw \
              UCjF38SrSm1WfCKjentE1rsw UCvYff1FFK4-4nCzoc8vQmVA UCfl9tL1ax3riY02P8E6Ef7w UCMhQi8xf5w0Q-W_riKapp5w
$PY appstore_lookalikes.py
```

Delete `out/quota.json` before a fresh run: the meter is cumulative and enforces the 5,000-unit cap.
