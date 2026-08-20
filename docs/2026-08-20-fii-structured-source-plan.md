# Plan: CVM FII/FIAGRO structured informe-mensal source (deferred)

Status: **planned, not built.** Deferred at the end of the 2026-08-20 ingestion
expansion to avoid shipping mis-attributed data. The fund modules already have
**live news coverage** (see commit `70317a6`), so this is an enhancement, not on
the critical path.

## Context — what this completes

The 2026-08-20 session added four ingestion workstreams (all live):

| Workstream | Commit |
|---|---|
| Betting/iGaming module + SPA-via-DOU lens | `fc76bdd` |
| Governança axis on the CVM fato lens | `28c1783` |
| Fund modules (`real-estate-funds`, `agri-funds`) + entities + news lens | `70317a6` |
| Macro panel (Copom/Selic + Focus) | `373825f` |

The one remaining piece from "Fundos Imobiliários / FIAGRO … structured from CVM"
is the **structured informe-mensal source** — the FII/FIAGRO analogue of
`src/ingest/cvm_inf_diario.py` (which does the same for FIF: PL moves by CNPJ).

## What the real CVM data showed (grounding — don't re-derive)

Dataset (verified live 2026-08-20):
`https://dados.cvm.gov.br/dados/FII/DOC/INF_MENSAL/DADOS/inf_mensal_fii_{YEAR}.zip`
Members: `inf_mensal_fii_geral_{YEAR}.csv`, `..._ativo_passivo_{YEAR}.csv`,
`..._complemento_{YEAR}.csv`.

- `geral` columns include: `CNPJ_Fundo_Classe`, `Nome_Fundo_Classe`,
  `Data_Referencia` (monthly), `Segmento_Atuacao`, `Tipo_Gestao`,
  `Quantidade_Cotas_Emitidas`, `Codigo_ISIN`, `Nome_Administrador`.
- `ativo_passivo` columns include: `CNPJ_Fundo_Classe`, `Data_Referencia`,
  `Total_Necessidades_Liquidez`, `Total_Investido`, `Total_Passivo`.

### Three gotchas that make a naive build wrong
1. **CNPJ-keyed, no B3 ticker.** Our fund entities are ticker-centric
   (`MXRF11`, …). The join needs an exact CNPJ↔ticker bridge.
2. **Name matching produces FALSE matches** — do not use it. Observed:
   "RENDA URBANA" matched *BTG Pactual* Renda Urbana (not HGRU11/CSHG);
   "VALORA" matched *Valora CRI CDI* (not VGIA11/Agro). Mis-attribution.
3. **No clean PL column.** `ativo_passivo` has `Total_Investido`/`Total_Passivo`,
   not a labelled patrimônio líquido. Prefer a clean signal (see below).
4. **FIAGROs are NOT in the FII dataset** — separate CVM package (locate it;
   likely `dados.cvm.gov.br/dataset/fiagro-*` / an INF_MENSAL under a FIAGRO doc).

### Verified CNPJs (exact, unambiguous — from the live geral CSV)
Enrich these 7 registry entities' `cnpj_roots` so the join is exact:

| entity | CNPJ | Nome_Fundo_Classe |
|---|---|---|
| `mxrf11` | 97.521.225/0001-25 | FII MAXI RENDA RL |
| `knri11` | 12.005.956/0001-65 | KINEA RENDA IMOBILIARIA FII |
| `xpml11` | 28.757.546/0001-00 | XP MALLS FII |
| `visc11` | 17.554.274/0001-25 | VINCI SHOPPING CENTERS FII |
| `kncr11` | 16.706.958/0001-32 | KINEA RENDIMENTOS IMOBILIARIOS FII |
| `xplg11` | 26.502.794/0001-85 | XP LOG FII RL |
| `recr11` | 28.152.272/0001-26 | FII REC RECEBIVEIS IMOBILIARIOS |

Still need precise CNPJs (look up by **ISIN**, not name): `hglg11`, `btlg11`,
`hsml11`, `hgru11`. `vgia11` earlier matched the wrong Valora fund — re-verify.
FIAGROs (`knca11`, `rzag11`, `xpca11`, `vgia11`, `rura11`) come from the FIAGRO
dataset once located.

## Build plan (next session)

1. **`src/ingest/cvm_fii.py`** — mirror `cvm_inf_diario.py`:
   - Fetch `inf_mensal_fii_{year}.zip`; iterate `geral` (+ join `ativo_passivo`
     by `CNPJ_Fundo_Classe` + `Data_Referencia` if a balance signal is used).
   - Emit per-fund rows keyed by CNPJ for the latest 1–2 competency months.
   - Pick a **clean** signal: `Quantidade_Cotas_Emitidas` month-over-month
     change (= follow-on offering / new issuance — a real competitive event),
     resolved strictly via CNPJ. Avoid the ambiguous PL columns for v1.
2. **Entity enrichment** — add the 7 verified `cnpj_roots` above (live registry
   `put_entity`/patch); look up the remaining FIIs by ISIN.
3. **FIAGRO** — locate the separate dataset; add a sibling fetch (or a `tipo`
   param on `cvm_fii`) for `agri-funds`.
4. **Wire** into `lambda_port` structured branch (budgeted), diff via
   `detect_moves`/`detect_new` on a `cvm_fii` state source, add a `fii_moves`
   digest slice; resolution to entities via `entity_registry.resolve_by_cnpj`.
5. **Tests** — ZIP-fixture parse, CNPJ join, move detection, and that a fund
   with no matching CNPJ does NOT attribute (guards against the false-match bug).

## Verification
- A tracked fund with a verified CNPJ shows a structured move when
  `Quantidade_Cotas_Emitidas` changes; an untracked/ambiguous fund never
  attributes to a wrong entity.
- `python -m pytest -q` green; one live pipeline run shows `real-estate-funds`
  carrying a structured (non-news) signal.
