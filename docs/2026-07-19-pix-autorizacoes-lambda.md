# Pix + Autorizações on Lambda — 2026-07-19

## What changed

Extended the Phase 1.5 ingest Lambda beyond normativos / CVM funds /
IF.data to include:

1. **BCB institutions in operation** (`bcb_autorizacoes`) — new-entrant
   signal via `detect_new`, first-run seed suppressed.
2. **BCB Pix DICT keys** (`bcb_pix`) — per-ISPB key-stock momentum via
   `detect_moves` + `DynamoDbValueState`.

Also restored DynamoDB state support in source `src/diff/engine.py`
(`DynamoDbState`, `DynamoDbValueState`) so Lambda and tests share one
engine.

## Live schema alignment (required)

First deploy against catalog-era URLs returned HTTP 400/500. Fixed after
probing Olinda:

| Intended source | Broken assumption | Live path |
|---|---|---|
| Pix per-institution | `TransacoesPix?$filter=AnoMes` EntitySet | FunctionImport `ChavesPix(Data=@d)` |
| Autorizações | BcBase `EntidadesSupervisionadas` as EntitySet | `Instituicoes_em_funcionamento` EntitySets |

See `docs/DATA_SOURCES.md` for full live field tables.

## CDK / env

`infra/app.py` now passes:

- `ONCA_COMPETITOR_ISPB` (from `config/watchlist.yaml`)
- `ONCA_PIX_MOVE_THRESHOLD_PCT` (default 15)

## Smoke test (my2027)

| Run | Result |
|---|---|
| 1st invoke | 200; autorizações baseline **1751**; Pix ISPBs **872**; 0 moves |
| 2nd invoke | 200; both stay 0 new / 0 moves; DynamoDB holds full state |

Digest keys: `regulatory`, `competitor`, `market`, `new_entrants`,
`pix_moves`. New entrants also write to the raw corpus for the KB when
non-empty; Pix moves are numeric-only (not RAG docs).
