# Regulator coverage audit: end to end, per regulator (2026-09-27)

**Why:** incident #173. MP 1.394 banned online betting and Onça showed nothing for two days. Betting "had coverage" only in the sense that search terms existed. This audit looks for the **next** betting-style blind spot: a regulator's material act that never reaches CRO, CCO or Ask, even though config makes coverage look present.

**Method, per act:**
1. **Ground truth** from the regulator's own publication (DOU `s=todos`, CVM site, BCB).
2. **Ingest:** raw corpus `s3://onca-raw-668449743071/`, digests `lambda-digests/` (249 runs, 2026-08-27 → 09-27), Bedrock KB `MHFHPHYIQH`.
3. **Classify:** `federal_acts`, `regulatory._DOMAINS`, `sector_events`.
4. **Surface:** `feed.executive.cro|cco`, `feed.feed`, `feed.sector_events`.
5. **Ask:** live Lambda `OncaAgent`, one natural officer question per act.

**Constraints.** Read-only throughout: nothing was fixed, deployed or written to AWS.

**Live replay.** The DOU filter chain was replayed live on 2026-09-27 against today's search results:
- the 19 registry topic phrases, plus the 18 entity terms the live Lambda actually searches. Those entity terms are `ONCA_FATOS_WATCHLIST` + `ONCA_COMPETITORS` + registry `fatos_terms()`, which contains only BB Seguridade, Caixa Seguridade, Cielo and Porto Seguro.
- Each result was tagged with the reason `fetch_dou` would keep or drop it.

Of 377 search hits:
| | Topic phrases | Entity terms |
|---|---|---|
| Kept | 46 | 24 |
| Dropped by the organ allowlist | 89 | 142 |
| Dropped by the topic-organ scope | 76 | n/a |

---

## 1. Summary matrix

Verdicts: ✅ OK · 🟡 partial (stops at the stage named) · ❌ MISSED. "n/a" = stage not reached.

| Regulator | Act (date) | Ground truth | Ingest | Classify | Surface (CRO/CCO/feed) | Ask | Verdict |
|---|---|---|---|---|---|---|---|
| **BCB** | Res. BCB 589: PSAV/crypto framework (23/09) | ✅ | ✅ bcb_normativos | ❌ card domain "Câmbio" → banking/IB, not crypto | ❌ crypto CRO; press "BC veda transações de criptoativos…" stuck *pending* | 🟡 right text, `grounded=false` | 🟡 classification |
| **BCB** | Res. BCB 588: AML, Circ. 3.978 (23/09) | ✅ | ✅ | ❌ `industries=[]`, `low` | 🟡 generic card; not in CCO | 🟡 vague ("houve uma mudança") | 🟡 classification |
| **BCB** | Atos 1.389/1.390: liquidação of Trustee and Banvox DTVM (03/09) | ✅ | ✅ | ❌ low / no industry | 🟡 one fused card 09-03 (with FII dividends), no industry | ❌ declined: KB skipped by the "extrajudicial" cue | ❌ Ask |
| **CMN** | Res. CMN 5.343: FIDC (24/09) *(control)* | ✅ | ✅ | ✅ securitization | ✅ lifecycle + change cards | n/a | ✅ |
| **CVM** | PAS judgment: Banco Master/Vorcaro, R$ 203 mi (08/09) | ✅ | ❌ no CVM sanctions source (news only, 7 outlets) | n/a | 🟡 `news_corroborated` 09-09, not alert, no CCO | ❌ declined | ❌ |
| **CVM** | Ofício Circular CVM/SSE 3/2026: FIDC performance fee (11/09) | ✅ | ❌ no CVM normative source | n/a | ❌ | ❌ declined | ❌ |
| **CVM** | SRE stop order: OPEA CRI 554ª emissão (~03/09) | ✅ | ❌ (news 2 outlets; `cvm_ofertas` sees only new ids) | n/a | ❌ | not asked | ❌ |
| **SUSEP** | Res. Susep 96 & 97: vesting reversal (DOU 17/09, EXTRA_D) | ✅ | ❌ topic-scope drop | (would be insurance/high) | ❌ | ❌ declined | ❌ |
| **SUSEP/CNSP** | Consulta Pública 6/2026: draft CNSP rule amending CNSP 432 (capital) (17/09) | ✅ | ❌ topic-scope drop | n/a | ❌ | ❌ declined (irrelevant citations) | ❌ |
| **Congress** | LC 237: IRPJ/CSLL relief for local reinsurers (15/09, EXTRA_C) | ✅ | ❌ organ drop ("Atos do Poder Legislativo") | (would be insurance/high) | ❌ | ❌ declined | ❌ |
| **PREVIC** | Portaria Previc 728: Plano ASG (DOU 17/09, EXTRA_D) | ✅ | ❌ topic-scope drop (news 09-25 arrived, "irrelevant") | (would be closed-pension/high) | ❌ | ❌ **wrong**: answers with Portaria 661 via an Itaú card | ❌ + misleading |
| **PREVIC** | Portaria Previc 661: EFPC segmentation 2027 (28/08) | ✅ | 🟡 via the "ITAU UNIBANCO" term; KB title-only | ❌ bound to Itaú | ❌ | ❌ misdescribed | 🟡 |
| **ANS** | RN 679 (mammography coverage, 14/09); RN 680 (22/09) | ✅ | ❌ ANS not in organs; no ANS normative source | n/a | ❌ | ❌ declined | ❌ (moderate materiality) |
| **CADE** | AC 08700.008630/2026-94: StoneX → control of **Banco Travelex** (edital 14/09, approved 15/09) | ✅ | ❌ CADE lens: 1 distinct AC in 30 days | n/a | ❌ | ❌ declined | ❌ |
| **CADE** | AC 08700.008684/2026-50: BTG + CM Hospitalar (approved, DOU 23/09) | ✅ | 🟡 only via the generic DOU lens ("BTG PACTUAL"), title-only KB | ❌ not antitrust | ❌ | (covered by the question above) | 🟡 |
| **COAF** | Decisões COAF 29–37/2026 (28/08): PAS vs non-FS obligated parties | ✅ | ❌ topic-scope drop | n/a | n/a | n/a | — low materiality for Onça; AML for FS = BCB 588 |
| **SPA** | MP 1.394 + despachos (25/09) *(re-verify)* | ✅ | ✅ | ✅ betting/critical | ✅ sector event, reg_threat 90 | ✅ cited DOU | ✅ |
| **SPA** | Editais de citação (22–23/09) | ✅ | ✅ | ✅ betting/medium | 🟡 no CCO | ✅ cited | ✅/🟡 |
| **Other betting enforcers** | MPDFT Portaria 1.045: inquérito civil vs **BETBOOM** (DOU 25/09); Portaria MJSP 1.287: forfeiture of blocked illegal-bet funds (04/09) | ✅ | ❌ organ drop (hit by the betting phrases) | n/a | ❌ | ✅ (not about these) | ❌ |
| **Presidência** | MP 1.393: Desenrola 3.0 (25/09) | ✅ | ✅ | 🟡 securitization only (0 active entities), not banking/fintech | 🟡 event on an uncovered industry; banking reg_threat 25 | ✅ cited KB | 🟡 classification |
| **ANPD** | not examined in depth | — | organ absent, no source | — | — | — | note only |

**Score (19 act-rows):**
- **OK: 3** (MP 1.394, CMN 5.343, SPA editais);
- **partial: 7**;
- **missed: 9**.

**Every insurance, pension, CVM and CADE act missed.** Each one is covered on paper: the organ is in `RELEVANT_ORGANS`, a topic phrase exists in `INDUSTRY_TOPICS`, and the source row shows "live". None of it reached the corpus.

---

## 2. Per-act evidence

### BCB / CMN

**A1: Resolução BCB 589 (23/09/2026).** Amends Res. BCB 520, the PSAV (virtual-asset service providers) framework.
- **Official act:** https://www.in.gov.br/web/dou/-/resolucao-bcb-n-589-de-23-de-setembro-de-2026-734471404
- **Ingest:** ✅ `s3://onca-raw-668449743071/BCB/bcb:Resolução BCB:589.txt`, with ementa. The KB ranks it #1 (0.92).
- **Classify:** `federal_acts.classify` → `crypto/high` (correct). **But** the reg card `regulatory-res-bcb-589`:
  - has domain **"Câmbio & mercado aberto"**, `affected_industries=[banking, investment-banking]`;
  - its narrative describes *operações compromissadas*, because it was fused with BCB Comunicados 46007/46008 (`source_ids`).
  - So the crypto CRO never sees it.
- **Sector event:** the press confirms a ban-type change. `sector_events/latest.json` holds a **pending** crypto report: "Banco Central veda transações de criptoativos com empresas sem autorização no Brasil" (Terra, 09-25). The official act is `high` and not an MP/Lei/Decreto, so it cannot open an event (`sector_events.py:517-527`). The pending item waits for a second outlet.
- **Ask** (CRO, "O Banco Central mudou alguma regra para as prestadoras de serviços de ativos virtuais…"): the content is correct, but it comes back `grounded=false` with `citations=[]`. The model wrote `[card_id: kb:0]`, which `validate_citations` does not parse.

**A2: Resolução BCB 588 (23/09).** Amends Circular 3.978, the AML/CFT policy for every BCB-authorized institution, consórcio administrators included.
- **Official act:** https://www.in.gov.br/web/dou/-/resolucao-bcb-n-588-de-23-de-setembro-de-2026-734504944
- **Ingest:** ✅ raw `BCB/bcb:Resolução BCB:588.txt`.
- **Classify:** `federal_acts` → `industries=[]`, `severity=low` ("no covered industry or entity"). AML is not an industry, and nothing tags it as a compliance topic.
- **Surface:** lifecycle card "Setor financeiro" (banking/fintech/insurance; consórcio omitted). It is **not** in CCO `change_diff`.
- **Ask** (CCO): "FATO: Houve uma mudança recente nas regras de prevenção à lavagem de dinheiro do Banco Central. [kb:0]". It is cited, but content-free.

**A3: Atos do Presidente 1.389 / 1.390 (03/09).** Liquidação extrajudicial of Trustee DTVM and Banvox DTVM, linked to the Banco Master case.
- **Official acts:** https://www.in.gov.br/web/dou/-/ato-n-1.389-de-3-de-setembro-de-2026-730242867 and https://www.in.gov.br/web/dou/-/ato-n-1.390-de-3-de-setembro-de-2026-730233297
- **Ingest:** ✅ raw `BCB/bcb:Ato do Presidente:1389.txt` / `1390`, plus Comunicados 45865/45866. News: 3 outlets.
- **Classify:** low, no industry. `investment-banking` has no topic spec, and "liquidação extrajudicial" is not in `_BAN` or `_REVOKED_AUTH` (`federal_acts.py:114-121`).
- **Surface:** one `regulatory_fusion` narrative (`narratives/2026-09-03/cand-bcb:Comunicado:45866.json`). It is fused with an FII-dividend headline and carries no industries.
- **Ask** (CCO, "O Banco Central decretou alguma liquidação extrajudicial recentemente?"): declined in 0.5 s. **Root cause:** `"extrajudicial"` is in `_DISTRESS_CUES` (`agent_ask.py:227-229`), so `answer()` skips KB retrieval entirely (`agent_ask.py:806-807`). The KB holds the Ato at score 0.85. **Any question about a BCB liquidation disables the KB.**

**Control: Res. CMN 5.343 (FIDC, 24/09).** ✅ lifecycle and change cards (`securitization`), sector-event pending corroborated by the press ("CMN veda a FIDCs aplicação em créditos judiciais…").

### CVM

**No ingester exists for CVM normative acts, Ofícios-Circulares, stop orders or PAS judgments.** Checked by grep: nothing in `src/ingest` references `conteudo.cvm.gov.br`, `gov.br/cvm`, "Ofício Circular" or "sancionador". The latest Resolução CVM is 246 (DOU 03/08), so no new rule fell inside the window, but the gap is structural.

**B1: PAS judgment of 08/09/2026.** Fines totalling R$ 203 mi against Banco Master (em liquidação), Daniel Vorcaro and others.
- **Official source:** https://www.gov.br/cvm/pt-br/assuntos/noticias/2026/cvm-aplica-multas-que-somam-mais-de-r-200-milhoes-em-caso-envolvendo-banco-master
- **Ingest:** news only (G1, Estadão ×2, CBN, O Globo…, `company=Banco Master`).
- **Surface:** `narratives/2026-09-09/cand-ent-banco_master.json` is `news_corroborated`, threat 0.438, not an alert. It never reached CCO. The KB holds no news, so KB retrieval for the fine returns CVM fund stubs.
- **Ask** (CCO, "A CVM aplicou alguma sanção recente…"): "Não tenho esse dado na base da Onça", with 16 irrelevant BCB Comunicado citations attached.

**B2: Ofício Circular CVM/SSE 3/2026 (11/09).** FIDC performance fee may not be passed to the consultant.
- **Official source:** https://www.gov.br/cvm/pt-br/assuntos/noticias/2026/area-tecnica-da-cvm-orienta-sobre-impossibilidade-de-vinculacao-da-taxa-de-performance-de-fidc-a-remuneracao-da-consultoria
- **Onça:** not in the corpus, the digests or the KB.
- **Ask** (CRO): declined, with fund-registry citations.

**B3: SRE suspension of the OPEA Securitizadora CRI 554ª emissão.**
- **Official source:** https://www.gov.br/cvm/pt-br/assuntos/noticias/2026/suspensa-oferta-de-certificados-de-recebiveis-imobiliarios-cri-da-opea-securitizadora-s-a
- **Onça:** news only (Valor Investe, tradersunion, 09-03). `cvm_ofertas` does `detect_new` on the offer id, so a status change is never an event. Entity `opea_sec` exists, but no card was produced. `sector_events.assess_headline` → "no sector named".

### SUSEP / CNSP

`RELEVANT_ORGANS` includes SUSEP and CNSP, but only entity names reach them. The insurance topic phrases "seguros privados" and "resseguro" match every SUSEP act, because the text says "SUPERINTENDÊNCIA DE SEGUROS PRIVADOS". Every such hit is then dropped by the topic-organ scope, because `NORMATIVE_ISSUERS` holds CNSP but **not SUSEP** (`registry.py:201-209`).
- **Replay:** "seguros privados" returned 20 hits, 17 from SUSEP, **0 kept**. "resseguro" returned 20 hits, 16 from SUSEP, 0 kept.
- **No CNSP resolutions** appeared in the window. The SUSEP collegiate now issues "Resolução Susep" (post-LC 213/2025).

**C1: Resolução Susep 96 and 97 (14/09, DOU 17/09 DO1_EXTRA_D).** Reversal of provision balances where participants did not meet vesting, in collective insurance/EAPC plans signed before 2017. Material for insurers and open pension.
- **Official acts:** https://www.in.gov.br/web/dou/-/resolucao-susep-n-96-de-14-de-setembro-de-2026-732448943 · …-97-…-732449182
- **Onça:** not in the corpus, the digests or the KB (KB top hits: MP 1.394 / MP 1.393).
- **Ask** ("A Susep publicou alguma norma nova para seguradoras…"): declined.
- **Offline classifier check:** `federal_acts.classify` on the real text → `insurance/high`. **The classifier would work; ingest never delivers.**

**C2: Edital de Consulta Pública 6/2026/SUSEP (17/09, DO3).** A draft CNSP resolution amending CNSP 432: technical provisions, capital, minimum required capital, investments.
- **Official act:** https://www.in.gov.br/web/dou/-/edital-de-consulta-publica-n-6/2026/susep-732198360
- Hit by "resseguro", dropped by the topic scope.
- **Ask:** "Não tenho esse dado…", citing two unrelated SUSEP authorization Portarias bound to SANTANDER.
- **Press:** the 09-27 sector-news run brought related SUSEP headlines ("Susep abre consulta de reporte ESG…", "Susep vai mudar regras para resgates e prêmios na capitalização"). `sector_events` judged them "irrelevant: no change vocabulary", so they became news items only.

**C3: Lei Complementar 237 (15/09, DO1_EXTRA_C).** Includes the IRPJ/CSLL relief for local reinsurers among the exceptions to the tax-benefit rules.
- **Official act:** https://www.in.gov.br/web/dou/-/lei-complementar-n-237-de-15-de-setembro-de-2026-731891987
- The "resseguro" phrase **found it** and the organ allowlist dropped it: its organ is **"Atos do Poder Legislativo"**.
- Only the Presidência veto despacho was ingested (`low`, no industries).
- `federal_acts` on the real text → `insurance/high`. It would open a sector event, being a top-level instrument.
- **Ask:** declined.

### PREVIC / CNPC

**D1: Portaria Previc 728 (16/09, DOU 17/09 DO1_EXTRA_D).** Plano ASG: ESG risk assessment in EFPC investments.
- **Official act:** https://www.in.gov.br/web/dou/-/portaria-previc-n-728-de-16-de-setembro-de-2026-732448864 (EXTRA_D, "Diretoria de Normas")
- **Ingest:** the topic phrase "previdência complementar" returned 20 hits (9 PREVIC), **0 kept**. PREVIC is not in `NORMATIVE_ISSUERS`, and the 20-result page only reaches back to 09-21. Not in the KB.
- **Press:** news arrived 09-25 ("Previc publica diretrizes para fundos de pensão incorporarem riscos ESG…", tagged `closed-pension`). `sector_events` judged it irrelevant: "publica diretrizes" is not change vocabulary.
- **Ask** ("A Previc publicou alguma nova regra para os fundos de pensão recentemente?"): **misleading**. It answered that Previc published **Portaria 661**, which "pode impactar as operações de fundos de investimento", citing card `cand-ent-itau-unibanco`, and added an "inference" about blast radius.

**D2: Portaria Previc 661 (26/08, DOU 28/08).** Updates the EFPC segmentation for supervision in 2027.
- **Ingest:** only because the DOU search term "ITAU UNIBANCO" hit it.
- **Binding:** `company` = the search term (`dou.py:289`), so the metadata says `"name": "ITAU UNIBANCO"`. Portarias 625/627/655/682 are likewise bound to BRADESCO.
- **KB:** the doc is `"Portaria N° None\n\nPortaria Previc Nº 661…"`, title only (the pre-#174 template).
- **Industry status:** closed-pension is `covered: false`, with 0 active entities.

### ANS

**E1: RN ANS 679 (10/09, DOU 14/09)** expands mandatory coverage (digital mammography). **RN 680 (18/09)** updates the Rol/DUT.
- **Why it matters:** health insurers sit inside `insurance` (Bradesco Saúde, SulAmérica Saúde, Porto Seguro Saúde, 188 insurance entities).
- **Onça:** ANS is not in `RELEVANT_ORGANS`, and the only ANS source is the IGR complaints ranking (reputation).
- **Ask:** declined.
- Moderate materiality. This is a scope decision, but today "insurance coverage" silently excludes the health regulator.

### CADE

**F1: AC 08700.008630/2026-94.** StoneX Participações acquires **control of Banco Travelex S.A.** (banks, CNAE 6422-1).
- **Official acts:** filed in Edital 691 (DO3 14/09, https://www.in.gov.br/web/dou/-/edital-n-691-de-11-de-setembro-de-2026-731517468); approved in Despacho SG nº 1.231 (DO1 16/09).
- **Registry:** both parties are entities (`stonex`, `stonex-corretora`, `travelex`).
- **CADE lens yield:** in 115 runs over 30 days, the digest `cade` section held **one distinct AC** (B3, 08700.012323/2025-27).
- **Replay of `cade.fetch_atos` on today's search:** 36 acts, **parties empty on all 16 editais**, garbled on the despachos. The Travelex approval came out as "Covetrus, Inc. e Cencora, Inc. … Nº 1.231 - Ato … Requerent".
- **KB:** holds Edital 691 only as a snippet centred on the "BTG PACTUAL" term, so the StoneX/Travelex AC in the same edital is cut off.
- **Ask** (CSO, "O CADE aprovou alguma aquisição recente envolvendo bancos ou corretoras?"): declined.

**F2: AC 08700.008684/2026-50.** BTG Pactual + CM Hospitalar, approved (Despacho SG nº 1.247, DOU 23/09).
- It arrived only through the generic DOU lens (term "BTG PACTUAL"): `DOU/dou:despachos-sg-de-22-de-setembro-de-2026-733462366.txt`, title-only.
- It was not an antitrust signal, and no card was produced.

### COAF / UIF

- **Acts in window:** Decisões COAF 29–37/2026 (DO1 28/08) are sanction judgments against non-financial obligated parties (e.g. a jeweller). They were dropped by the topic scope ("Sistema Financeiro Nacional" hit them).
- **Materiality:** low for Onça's buyers. The FS AML rule of the month is BCB 588 (A2), which ingests but classifies as `low` with no industry.
- **Mechanism risk:** COAF acts reach Onça only if they name one of the 18 DOU entity terms.

### SPA (re-verify) and other betting enforcers

- **MP 1.394:** ✅ end to end.
  - Ask (CRO): "Houve alguma mudança regulatória no setor de apostas?" → MP 1.394, 83 entities, critical, cited to the DOU act URL.
- **SPA editais de citação (09-22/23):** ✅ ingested (betting/medium).
  - Ask (CCO): "Alguma operadora de apostas foi alvo de processo sancionador…?" → cites the editais (IRMÃOS MARCONI & CIA, ROMARIO CARNEIRO).
- **New finding: non-regulator enforcement against betting operators is dropped.** Both acts below were **hit by the betting phrases** and dropped by the organ allowlist:
  - **MPDFT Portaria 1.045** (DOU 25/09, organ *Ministério Público da União/MPDFT*): inquérito civil against **BETBOOM LTDA** for targeting gamblers with ludopatia. https://www.in.gov.br/web/dou/-/portaria-n-1.045-de-27-de-agosto-de-2026-734424210
  - **Portaria MJSP 1.287** (DOU 04/09, organ *MJSP/Gabinete do Ministro*): procedure for forfeiting blocked funds of illegal betting (Lei 14.790 art. 21-A). https://www.in.gov.br/web/dou/-/portaria-mjsp-n-1.287-de-2-de-setembro-de-2026-730240793
- **Saturation:** "Secretaria de Prêmios e Apostas" already returns a full 20-result page spanning only 09-17 → 09-25. See root cause R3.

### Presidência / Congress

- **MP 1.393 (Desenrola Brasil 3.0, 25/09):** ✅ ingested (full text, KB #1 at 0.84).
  - **Classification:** `securitization` only, from lead vocabulary "renegociação de dívidas" and "crédito responsável". Securitization is `covered: false` with 0 active entities; the sector event shows `n_affected 4`.
  - **Surfacing:** banking and fintech, which run the program, show `reg_threat 25.0` and no event.
  - **Ask** ("O Desenrola Brasil 3.0 traz alguma mudança regulatória para os bancos?"): correct and cited (KB). So Ask recovers what the CRO misses.
- **Laws:** every law in the window is published under **"Atos do Poder Legislativo"**, not "Atos do Poder Executivo":
  - LC 236 (04/09), LC 237 (15/09), Lei 15.504 (15/09), Leis 15.507–15.521 (22–25/09);
  - Decretos Legislativos appear under **"Atos do Congresso Nacional"**.
  - **Neither organ is in `RELEVANT_ORGANS` or `NORMATIVE_ISSUERS`, and neither appears anywhere in `src/`.**
  - In this window only LC 237 was FS-material.
  - The **conversion law of MP 1.394 or MP 1.393**, or an Ato Declaratório of the Congress on an MP losing validity, will publish under these organs. **Onça will not see what happens to the betting ban.**
- **Bills (PLs) in Câmara/Senado:** no source at all.

### ANPD

Not examined in depth. The organ is absent from the allowlists, and no ANPD source exists. It is relevant to fintech, financial-data-analytics and banking (Open Finance data).

---

## 3. Root causes (file:line)

| # | Root cause | Where | Acts affected |
|---|---|---|---|
| **R1** | **Laws are never ingested.** `RELEVANT_ORGANS` and `NORMATIVE_ISSUERS` have "Atos do Poder Executivo" (MPs, Decretos) but not **"Atos do Poder Legislativo"** (Leis, LCs, including MP conversion laws) or **"Atos do Congresso Nacional"** (Decretos Legislativos, MP-expiry declarations). The comment at `registry.py:202` claims `# MPs, Decretos, Leis`, and the #175 ticket repeats "MPs, Decretos e Leis are all published … under Atos do Poder Executivo". Both are wrong. | `src/ingest/dou.py:40-60`; `src/ingest/registry.py:201-209` | LC 237; the future conversion of MP 1.394/1.393 |
| **R2** | **Sector regulators excluded from the topic-organ scope.** Every topic phrase is scoped to `NORMATIVE_ISSUERS` (`registry.py:354-356`), which lists CMN, CNSP, CNPC, MF/GM, SPA and the Presidência, and not **SUSEP, PREVIC, CVM, BCB Diretoria Colegiada, COAF or CADE**. The insurance, pension, asset-management and securitization phrases therefore can't keep their own regulator's acts. Replay: 8 of 19 phrases kept **0** acts this month ("previdência complementar" 0/20, "securitização" 0/10, "administradoras de consórcio" 0/13, "crédito consignado" 0/8, "Lei nº 11.795" 0/1, "criptoativos" 0 results). | `src/ingest/registry.py:201-209, 354-356`; applied at `src/ingest/dou.py:144` | SUSEP 96/97, CP 6/2026, Previc 728 |
| **R3** | **One page per query, no pagination (≤20 results).** `_fetch_query` issues one request, and `fetch_dou` parses one page per (term, section). With `s=todos`, a 30-day lookback is fiction for busy terms, and backfills lose everything past result 20. Oldest result per page in the replay: "BANCO DO BRASIL" 09-25 (2 days), "CREDITAS" 09-25, "seguros privados" 09-21, "previdência complementar" 09-21, "Secretaria de Prêmios e Apostas" 09-17. A burst of more than 20 matching acts between runs is silently lost. | `src/ingest/dou.py:132-136, 247-258` | Any burst (post-ban SPA acts, CADE editais) |
| **R4** | **The DOU entity watch is 18 names,** and 8 of them return 0 hits (ITAUSA, NU INVEST, RECARGAPAY, CLOUDWALK, INFINITEPAY, BB SEGURIDADE; NOMAD/Cielo/Caixa Seguridade kept 0). Organ-listed regulators with no normative topic path (SUSEP, PREVIC, CVM, COAF) are therefore reached only when an act names one of these few banks. None of the 83 betting operators, pension funds, gestoras, crypto or consórcio names is searched. | `src/ingest/lambda_port.py:606-638` (fatos watch → `dou_terms`); `dou.py:91` `max_terms=25` | CVM/PREVIC/SUSEP operator-level acts |
| **R5** | **No CVM normative or enforcement source.** There is no ingester for Resoluções, Ofícios-Circulares, Deliberações/stop orders or PAS judgments, and `cvm_ofertas` delta is new-id only, so suspensions are invisible. | `src/ingest/` (absent); `src/ingest/cvm_ofertas.py` (detect_new) | B1, B2, B3 |
| **R6** | **The CADE lens is structurally near-dead:** | | F1, F2 (1 distinct AC in 30 days) |
| | a single quoted query over `("do1","do3")`; | `src/ingest/cade.py:29-31` | |
| | search-snippet text only (CADE is not in `FULL_TEXT_ORGANS`); | `src/ingest/dou.py:65-73` | |
| | `_PARTIES_RE` matches only "Requerentes", while editais say **"Partes:"**; | `cade.py:33-37` | |
| | `_ac_number` and `_parties` take the first match of multi-AC despachos and editais. | `cade.py:39-50, 60-84` | |
| **R7** | **Four divergent industry classifiers:** | | A1 (crypto → "Câmbio"), A2, MP 1.393, CMN 5.304 (auto financing → "Câmbio") |
| | `registry` vocabulary (used by `federal_acts`); | `registry.py:222-327` | |
| | `regulatory._DOMAINS`/`_DOMAIN_INDUSTRIES`, a regex over the **LLM-fused narrative** with no crypto, betting, AML or fund domain; | `src/synth/regulatory.py:119-136, 417-427` | |
| | `sector_events.INDUSTRY_TERMS`; | `src/synth/sector_events.py:87-108` | |
| | `_INSTRUMENTS` threading only IN/Res BCB, CMN, CVM, CNSP and Circular SUSEP. It lacks Resolução Susep, Portaria/Resolução Previc, CNPC, Portaria SPA/MF, MP and Lei. | `regulatory.py:83-94` | |
| **R8** | **Coverage holes in the industry topics:** | | A2, A3, MP 1.393 |
| | 8 of 17 covered industries have no `IndustryTopicSpec`: investment-banking, real-estate-funds (650 entities), agri-funds, advisory, financial-data-analytics, acquiring, wealth-management, private-markets; | `src/ingest/registry.py:222-327` | |
| | AML is not a tag; | same | |
| | "liquidação extrajudicial", "intervenção", RAET and "cassação" are not severe; | `src/ingest/federal_acts.py:114-121` | |
| | banking vocabulary misses the lead of MP 1.393. | same | |
| **R9** | **Sector events can't open from a sector regulator's own rule.** Only `critical` acts, or `high` top-level MP/Lei/Decreto, may open an event. A pending press "ban" report in the same industry does not combine with a `high` official act. Headlines like "publica diretrizes", "abre consulta", "mudar regras", "multa", "condena" and "liquidação" are "no change vocabulary". | `src/synth/sector_events.py:128-140, 517-527` | A1 (crypto pending), CMN 5.343, Previc 728 news |
| **R10** | **CCO has no enforcement or sanctions surface.** `build_cco` = integrity (data QA) + reputation (complaints) + distress. CVM PAS, BCB liquidations, COAF decisions, SPA editais, MP inquéritos and CEIS/CNEP never reach the compliance officer. | `src/synth/executive.py:1024-1070` | B1, A3, SPA editais, Betboom |
| **R11** | **Ask disables the KB on "extrajudicial".** `_DISTRESS_CUES` includes `extrajudicial`, which makes `answer()` skip `kb_retrieve`, meant for RJ distress news. Any question about a BCB/SUSEP/ANS **liquidação extrajudicial** therefore gets no KB. Also, a `[card_id: kb:0]` citation form fails `validate_citations`, so a correct answer is returned `grounded=false`. | `src/dashboard/agent_ask.py:227-229, 806-807, 406-437` | A3, A1 |
| **R12** | **KB content is thin or missing:** | | CVM fatos, pre-09-27 DOU acts (CMN 5.341, Previc 661/682, CADE despachos) |
| | the CVM Fato Relevante docs go through the "new-entrant entity" branch and are written as name + CNPJ only (46–74 bytes, all 31 docs); | `src/ingest/raw_writer.py:84-94` | |
| | all DOU docs written before 4fb4e54 are "`<Tipo> N° None` + title"; | `raw_writer.py:32-33` (old path; no backfill) | |
| | 13.5k CVM fund-class stubs dominate retrieval for any CVM question. | | |
| **R13** | **DOU entity binding = the matched search term.** `company` is the query term, so every hit is "about" whichever bank's name occurs anywhere in the act (Previc 661 → Itaú; Previc 625/627/655/682 → Bradesco; CMN 5.341 → CREDITAS). This feeds Ask misattribution (D1). | `src/ingest/dou.py:289-290` | D1, D2 |
| **R14** | **reg_threat is a constant.** A lifecycle card with no deadline scores 0.25 (`regulatory.py:366-370`), and "Setor financeiro" catch-all cards scope to **every** industry (`regulatory.py:441-456`). So `reg_threat_cards` = **25.0 for all 17 industries**, and insurance, closed-pension, crypto and betting show `n_reg 8 / n_changes 7`, all eight BCB/CMN cards. | `src/synth/regulatory.py:366-370, 441-456`; `executive.py:867-869` | all |

---

## 4. Ranked fix list (severity × effort)

| Rank | Fix | Severity | Effort | Closes |
|---|---|---|---|---|
| 1 | Add **"Atos do Poder Legislativo"** and **"Atos do Congresso Nacional"** to `RELEVANT_ORGANS`, `NORMATIVE_ISSUERS` and `FULL_TEXT_ORGANS`; fix the `registry.py:202` comment. Regression fixture: LC 237 found by "resseguro". Then add an explicit watch on MP 1.394/1.393 conversion ("Lei de conversão", "Ato Declaratório … Medida Provisória nº 1.394"). | Critical | S | R1 |
| 2 | Add **SUSEP, PREVIC (Diretoria de Normas), CVM (Colegiado/SNC/SSE/SRE), BCB Diretoria Colegiada, ANS** to the topic scope for **DO1 normative doc-types only** (Resolução / Instrução / Circular / Portaria-Normas). Add issuer phrases: "Resolução Susep", "Resolução CNSP", "Portaria Previc", "Resolução CNPC", "Ofício Circular CVM". Replay-test each phrase's kept count (it must be >0 in a 30-day window). | High | S | R2 |
| 3 | **Ask:** drop `extrajudicial` from `_DISTRESS_CUES` (keep `recuperação judicial`, `falência`). Accept the `[card_id: X]` citation form. | High | S | R11 |
| 4 | **DOU pagination or saturation guard:** page until `date < cutoff`, or at least log and alarm when a page is full (20) with its oldest hit newer than the cutoff. Add a per-run "saturated terms" metric to source_health. | High | S–M | R3 |
| 5 | **CADE lens:** fetch full text for "Defesa Econômica" acts; iterate every `Ato de Concentração nº … (Requerentes\|Partes):` block; `sections=("todos",)`; add a yield metric (distinct ACs/week) to source_health. | High | M | R6 |
| 6 | **CCO enforcement panel:** a `sanctions/enforcement` register fed by BCB Atos (liquidação/RAET/intervenção), SPA editais, COAF decisões, CEIS/CNEP, CVM PAS and MP/MPF inquéritos. Add `federal_acts` severity rules for liquidação, intervenção and cassação (operator-level critical). | High | M | R10, part of R8 |
| 7 | **CVM source** (ADR-0003 admission check first): the conteudo.cvm.gov.br legislação pages, the gov.br/cvm notícias page (PAS judgments, stop orders, Ofícios-Circulares), and `cvm_ofertas` status-change detection (suspensa/cancelada). | High | M | R5 |
| 8 | **One industry classifier:** make reg cards take `industries` from the underlying record's `federal_acts` result rather than a regex over the fused narrative. Add AML as a cross-industry compliance tag. Add `IndustryTopicSpec` for the 8 missing industries (FII: "fundos de investimento imobiliário", Lei 8.668; Fiagro: Lei 14.130; DTVM/corretoras; credenciadoras; consultoria CVM; FIP). Map MP 1.393-type credit programmes to banking/fintech. | Medium | M | R7, R8 |
| 9 | **Sector events:** let a `high` official act from the sector's own regulator open an event when a press "ban/suspension" report for the same industry is pending (BCB 589 + Terra). Widen the change vocabulary (novas regras, diretrizes, consulta pública, veda). | Medium | S | R9 |
| 10 | **KB hygiene:** a CVM fato template (company + category + subject), and a re-write of the title-only DOU and BCB docs from before 4fb4e54. | Medium | S | R12 |
| 11 | **DOU binding:** set `company` only when the term appears in the lead or parties; otherwise keep it as a mention. | Medium | S | R13 |
| 12 | **reg_threat:** derive it from act severity; stop scoping "Setor financeiro" BCB cards into SUSEP/PREVIC/SPA/CVM-only industries. | Medium | S | R14 |
| 13 | Extend the `reg_coverage` map to SUSEP, PREVIC, SPA, CADE, ANS and COAF, so the "gap" count is real. Decide the ANS scope (health insurers in `insurance`) and whether ANPD, MJSP/Senacon and the MP enforcement organs are admitted. | Low–Med | S | FC6 |

The **acceptance test** for all of these should be the method of this audit, automated: a fixture set of real acts per regulator, run DOU search → classify → feed → Ask, asserting "reaches CRO/CCO and Ask cites it". This is the per-regulator equivalent of the #173 acceptance.

---

## 5. False-coverage findings (config that looks like coverage but can't work)

1. **"Laws are covered."**
   - `registry.py:202`: `"Atos do Poder Executivo", # MPs, Decretos, Leis`. Leis are published under **Atos do Poder Legislativo**, which appears nowhere in `src/`.
   - The #175 admission note asserts the same.
   - No federal law has entered Onça via the DOU. The fate of MP 1.394 will arrive as a law.
2. **SUSEP / PREVIC / CVM / COAF in `RELEVANT_ORGANS`** (`dou.py:41-52`).
   - It looks like regulator coverage, but only 18 bank and insurer names are searched, 8 of which return 0 hits.
   - The insurance and pension topic phrases hit these regulators and are then removed by `NORMATIVE_ISSUERS`.
   - Live replay: "seguros privados" 0/20 kept, "resseguro" 1/20 (a Presidência despacho), "previdência complementar" 0/20.
3. **8 of 19 industry DOU phrases kept zero acts in 30 days.** Consórcio's two phrases, securitization's two, crypto's "criptoativos" and closed-pension's only phrase are dead config, like the betting terms before #174.
4. **`regulatory.py:88-90` comment:** "insurance normativos (already fetched via the DOU organ filter)" get lifecycle treatment. They are not fetched, and SUSEP now issues *Resoluções Susep*, which the `_INSTRUMENTS` regex does not thread.
5. **CRO per-industry panels for insurance / closed-pension / crypto / betting show "8 regulatory items, 7 changes".**
   - All eight are BCB/CMN "Setor financeiro" catch-all cards.
   - `reg_threat` is **25.0 in every industry** (a constant 0.25 lifecycle score) unless a sector event floors it.
6. **`feed.regulatory_coverage` summary: 15 segments, `gap: 0`.** The map only models BCB and CVM; SUSEP, SPA, CADE, PREVIC and ANS are "intentionally out" (`reg_coverage.py:12-13`).
7. **Fontes tab: "CADE atos de concentração — no ar / ok / 30 narrativas".** The lens yielded **one** distinct AC in 30 days, and CADE editais can never yield parties.
8. **"Entrantes regulados: BCB + SUSEP + SPA + PREVIC".** These are roster (new-licence) feeds, not normative or enforcement acts. They read as regulator coverage for three regulators whose rules never enter.
9. **CCO "compliance" officer:** its risk register is complaint rankings plus internal data-integrity findings. No regulator sanction or enforcement act reaches it, the CEIS/CNEP sanctions lens included.
10. **KB "has CVM filings":**
    - 31 Fato Relevante docs hold only a name and CNPJ;
    - 13.5k fund-class stubs crowd out every CVM query;
    - pre-09-27 DOU acts are titles ("Resolução N° None").
11. **`covered` industries that aren't:** securitization and closed-pension are *premium* tier but `covered: false` with 0 active entities. MP 1.393's only sector event landed on securitization.
12. **The sector-news queries for insurance and closed-pension work,** fetching the right SUSEP/PREVIC headlines on 09-24/25. But `sector_events` rejects them for lack of "ban" vocabulary and nothing else consumes sector-tagged news for the CRO. The #178 coverage alarm needs a volume *spike*, so steady regulator news never trips it.

---

## 6. What could not be verified

- Whether the in.gov.br search accepts a page/offset parameter; pagination was not probed. Result ordering looked date-descending in every sample.
- **Historical capture.** The replay used today's search results. An act published on, say, 09-17 might have been seen by a 3×/day run on 09-17 before page saturation. For SUSEP 96/97 and Previc 728 this makes no difference, because the topic-organ scope drops them regardless. They are also absent from all 249 digests since 08-27.
- **BCB 589's exact operative content.** The ementa was read; "veda transações … sem autorização" comes from the Terra headline, not from reading the full text.
- **Regulator sites beyond the DOU.** SUSEP, PREVIC, ANS and CADE SEI were not browsed; the DOU was taken as their official publication. The CVM site (legislação + notícias) was read.
- **ANPD, Congress bills (PLs) and state-level acts** were not examined.
- **Ask variance.** One live sample per question; answers are LLM output and may vary between runs.
- **Feed window.** Items dated 09-03 to 09-09 have mostly aged out of `feed.feed`. Their surfacing was judged from `narratives/<date>/`.

## Appendix: reproduction

- **Scratch scripts** (not committed): the DOU filter replay, KB retrieve and Ask invoker.
- **Ask invocation:**
  - event `{"headers":{"x-onca-origin": <runtime-read secret>}, "body": {"q", "officer": regulator|compliance|strategic}}`;
  - the secret was read into process memory only, never printed or written.
- **Evidence buckets:**
  - `s3://onca-raw-668449743071/{DOU,BCB,CVM-FatoRelevante}/…`;
  - `s3://onca-digests-668449743071/{lambda-digests,narratives,sector_events/latest.json,source_health}/…`;
  - live `feed.json` generated 2026-09-27T04:59Z.
