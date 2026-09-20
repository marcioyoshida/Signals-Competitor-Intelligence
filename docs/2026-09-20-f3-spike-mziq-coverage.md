# F3 spike — MZiQ IR-platform coverage and workbook stability

**Issue:** [#147](https://github.com/marcioyoshida/Signals-Competitor-Intelligence/issues/147)
**ADR:** 028 route 3 (`docs/2026-09-19-adr028-competitor-financial-statements-acquisition.md`)
**Run:** 2026-09-20, live network, ~600 HTTP requests total
**Verdict: the kill criterion FIRES. Do not build #148 as specified.**

---

## Kill criterion, stated before the run

> If fewer than **20** tracked entities have a machine-readable results workbook on MZiQ,
> the adapter is not worth building.

**Measured: 6 confirmed. Ceiling 20. Recommendation: kill.**

The count is the smaller half of the answer. The binding constraint is the *population*,
and it is below what Onça already ingests by two other routes — see "Why the count is not
the real finding".

---

## 1. Coverage

**Population.** Defined as every CVM-registered **active** issuer in a financial sector
(`Bancos`, `Emp. Adm. Part. - Bancos`, `Intermediação Financeira` + its holding variant,
`Bolsas de Valores/Mercadorias e Futuros`, `Arrendamento Mercantil`, `Seguradoras e
Corretoras` + its holding variant, `Emp. Adm. Part. - Crédito Imobiliário`,
`Securitização de Recebíveis`), deduped by CNPJ, **plus** the five tracked issuers that
are listed abroad and therefore absent from CVM (Nubank, StoneCo, PagSeguro, Inter&Co, XP).

| | count |
|---|---|
| CVM-active financial issuers | **49** |
| tracked foreign-listed issuers | 5 |
| **population** | **54** |
| IR site reached and platform identified | 34 |
| **on MZiQ** | **20** |
| reached, *not* MZiQ | 9 |
| bot-blocked (403: Itaú, Santander, XP) | 3 |
| no reachable IR host | 12 |

IR hosts were resolved from the **authoritative** `EMAIL` column of CVM's
`cad_cia_aberta.csv` (IR addresses are `ri@<domain>`), not by guessing brand slugs. An
earlier slug-guessing pass reached only 17 of 68 and missed BB, Itaú, Bradesco, Santander
and Inter — it was measuring the slug generator, not MZiQ.

Platform detection: the string `mziq` in the IR homepage (50–180 occurrences on a real
MZiQ site, 0 elsewhere), corroborated by a `mzfilemanager/v2/d/{company-uuid}` link.

**MZiQ issuers found (20):** bb, bradesco, itausa, abc_brasil, banrisul, banese, banpara,
bmg, parana_banco, brb*, b3, cielo, caixa_seguridade, porto_seguro, irb, qualicorp,
br_partners, inter, nubank, stone, pagseguro. (*partial — see raw counts below.)

## 2. Workbook availability

Of the 20 MZiQ issuers, document catalogues were successfully enumerated for **14**, and
a **machine-readable XLSX results workbook** was confirmed for **6**:

| issuer | workbook name | quarters found (2024-2026) |
|---|---|---|
| abc_brasil | `Séries Históricas {n}T{yy} PT.xlsx` | 10 |
| banrisul | `Séries Históricas {n}T{yy}.xlsx` | 10 |
| porto_seguro | `Planilha {n}T{yy}.xlsx` | 10 |
| br_partners | `Séries Históricas {n}T{yy}.xlsx` | 7 (one `.xlsm`) |
| cielo | `Série Histórica {n}T{yy}.xlsx` | 2 |
| inter | `Inter&Co - Séries Históricas 2T26.xlsx` | 1 |

Confirmed by `Content-Type:
application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` and a `.xlsx`
`Content-Disposition` filename — not by guessing from the link text. Everything else in
those catalogues (Earnings Release, DFP, ITR, Apresentação, Fact Sheet, Transcrição) is
`application/pdf`.

**6 is a lower bound, not a census.** Seven MZiQ sites could not be enumerated because
their category slugs were not recoverable (below). The honest projection from the measured
rate — 6 workbooks among the 14 issuers whose catalogue opened, 43% — is **~9 of 20**.
Even the absolute ceiling of 20-for-20 only *ties* the kill threshold.

## 3. Layout stability

ADR 028 predicted "sheet names are conventional, row labels are not". **Confirmed, with a
sharper result than predicted.**

**Sheet names are stable *within* an issuer.** Three consecutive quarters (4T25, 1T26, 2T26):

| issuer | sheets | across 3 quarters |
|---|---|---|
| abc_brasil | 27 | identical, 3/3 |
| banrisul | 29 | identical, 3/3 |
| porto_seguro | 10 | 1 rename (`IFRS-4-Glossário` → `Glossário`) |

**Sheet names share nothing *across* issuers.** The balance sheet is
`Balanço - Balance Sheet` (ABC), `Ativo` + `Passivo e PL` (Banrisul) and
`IFRS-4-Balanço` (Porto). There is no cross-issuer schema — every issuer needs its own map.

**Row labels drift, even within one issuer, within two quarters:**

| issuer / sheet | 4T25 → 2T26 | label overlap |
|---|---|---|
| banrisul / `DRE` | 37 → 37, identical order | 100% |
| abc_brasil / `Balanço` | 34 → 35 (+`Ativos Fiscais Correntes`, +`Aumento de Capital`, −`Impostos e Contribuições a Compensar`) | 96% |
| porto_seguro / `DREs Verticais` | 139 → 138 (−`Rewards`) | 99% |

A fixed row index breaks. Label matching mostly holds but needs per-issuer drift
tolerance — the same failure mode as #145's CVM equity bug, where a fixed `CD_CONTA`
read *Provisões* instead of PL and put BB at R$38.7bn instead of R$193.6bn.

## 4. Discovery cost — and an undocumented open API

ADR 028 recorded that `…/mzfilemanager/v2/d/{company-uuid}` returns **401**, and concluded
document UUIDs must be scraped from IR page HTML per quarter. **That conclusion is wrong,
and this is the most reusable finding of the spike.**

MZiQ sites are WordPress (`wp-content/themes/mziq_*`). Their results pages load documents
from a **central catalog API shared by every MZiQ client**, discovered in
`…/public/js/pages/mziq-file-manager.js` (`BASE_URL = 'https://apicatalog.mziq.com/filemanager'`):

```
POST https://apicatalog.mziq.com/filemanager/company/{company_uuid}/filter/categories/year/meta
     {"year": 2026, "categories": [...], "language": "pt_BR", "published": true}

POST https://apicatalog.mziq.com/filemanager/company/{company_uuid}/categoryInternalName/document/language/years
     {"categoryInternalNames": [...], "language_code": "pt_BR"}
```

**No token, no key, no session.** The 401 in ADR 028 was a method artefact — these routes
answer `POST` and reject `GET` with `{"error": "Unauthorized. No token provided."}`.

The response carries the whole catalogue: `file_title`, `file_name_original`,
`file_quarter`, `file_year`, `file_published_date`, `file_size` and a direct
`file_url`. ABC Brasil returns 20 years of history (2007-2026).

**Cost per issuer:** 1 GET for the IR homepage (company UUID) + 1 POST per year +
1 HEAD per workbook candidate to confirm the type. A full 3-year harvest for ABC Brasil
cost **13 requests**. Across all 20 sites the document phase cost **94 requests**. That is
cheap — an order of magnitude below the per-quarter HTML scraping ADR 028 assumed.

**The real blocker is not the documents, it is the `categoryInternalName` slugs.** They are
required (the API returns an empty set for `[]`, `null` or `["*"]`) and they are *bespoke
per site*: ABC uses `central-resultados-series-historicas`, BR Partners
`central_de_resultados_series_historicas`, Porto `central-resultados-planilha`, Banrisul
`cr-fact-sheet`. They are only discoverable as a `var categories = [...]` literal on the
issuer's own results page, and crawling ~30 pages per site recovered a usable set for just
**13 of 20**. Firing a generated 449-slug candidate list at every company recovered several
more but is not a general solution.

So the adapter's per-issuer cost is not the parsing — it is a **manual, per-issuer
configuration** (company UUID + category slugs + sheet map + label map) that must be
re-verified whenever an issuer redesigns its IR site.

## Why the count is not the real finding

Even at its ceiling, this route's whole addressable population is smaller than what Onça
already ingests:

| route | institutions | cadence | status |
|---|---|---|---|
| F2 — COSIF balancete (#146, #149) | **134** | monthly | live |
| F1 — CVM DFP/ITR (#145) | **438** issuers | quarterly | live |
| F3 — MZiQ workbooks | **6 confirmed**, ~9 projected, 20 ceiling | quarterly | this spike |

Every one of the 6 confirmed MZiQ issuers is **already covered** by both F1 and F2. F3
adds no institution Onça does not have; it adds *metrics* for a handful of them, behind a
bespoke per-issuer adapter and a vendor dependency.

## Recommendation

1. **Close #148 as not-worth-building in its current form.** A general MZiQ adapter fails
   the kill criterion on the measured count, and would be a per-issuer maintenance job
   for institutions already covered twice over.
2. **Keep the catalog-API finding.** It is the reusable asset from this spike: any future
   need for a *specific* issuer's IR documents (not just workbooks — press releases,
   transcripts, SEC 6-K/20-F are all in the same catalogue) is now 2 requests away
   instead of a scrape. Recorded here rather than built.
3. **If the workbook-only metrics are wanted later, scope it honestly** as a curated
   6-issuer adapter, and justify it on the metrics — NIM, efficiency ratio, ROE/ROA,
   Basileia, asset quality by stage, ARPAC — which genuinely are **not** in COSIF or the
   CVM package. That is a different, smaller story than #148, and it should be judged on
   whether those six issuers' extra metrics are worth a bespoke adapter each.

## Raw counts

Per-issuer document counts (2026 / 2025) and confirmed workbooks over 2024-2026:

```
issuer            docs26 docs25  XLSX(3y)  requests  categories recovered
abc_brasil          80    115      10         13     11
banrisul            23     37      10         13      3 (+generated)
porto_seguro        24     29      10         13      generated
br_partners         53     93       7         10     16
cielo               11     16       2          5      3
inter                1      0       1          4      1
bb                   9     14       0          3      1  (results cats not recovered)
b3                  55     78       0          3      1  (results cats not recovered)
caixa_seguridade    39     73       0          3      1  (results cats not recovered)
bradesco            30     33       0          3      1  (results cats not recovered)
parana_banco        16     29       0          3      generated
qualicorp           10     20       0          3      0
irb                  2      4       0          3      1
nubank               2      4       0          3      1
stone / pagseguro /  0      0       0          3      0  (catalogue not opened)
itausa / banpara /
bmg / banese
```

Artefacts from the run: `/tmp/p2.json` (population), `/tmp/final.json` (per-issuer
catalogue + workbook results), `/tmp/wb/*.xlsx` (9 workbooks, 3 issuers × 3 quarters).
