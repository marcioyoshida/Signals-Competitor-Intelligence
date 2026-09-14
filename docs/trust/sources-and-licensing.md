# Sources & licensing

What Onça ingests, and under what terms. Every claim below is checked against the
ingestion code itself (`src/ingest/registry.py`, `docs/DATA_SOURCES.md`), not a policy
statement layered on top — see "Verify this yourself" for the exact files.

## What we ingest

| Category | Sources (named) | Access mechanism |
| --- | --- | --- |
| BR government/regulatory open data | Banco Central (Pix DICT keys, institution authorizations, juros médios, IF.data, normativos, Focus) via Olinda OData; CVM (fund registry, ofertas, Informe Diário, IPE Fatos Relevantes, DFP/ITR financials); Diário Oficial da União (SUSEP/CADE/BACEN/CVM/CMN/CNSP/PREVIC acts); CADE antitrust decisions; CEIS/CNEP sanctions; PNCP public contracts (default-off); SUSEP supervised-entities; CNJ DataJud judicial-recovery/bankruptcy filings | Direct REST/OData/CSV/ZIP endpoints the agency itself publishes |
| Foreign regulatory open data | SEC EDGAR filings (20-F/6-K) for US-listed Brazilian fintechs (Stone, PagSeguro, Nu Holdings, Inter&Co, XP) | Free EDGAR API, descriptive User-Agent required |
| News / media | Google News RSS search + ~15 named Brazilian outlet RSS feeds (Valor Econômico, Money Times, Brazil Journal, NeoFeed, CQCS, Livecoins, …) | Publisher-run RSS syndication |
| Other | B3 ISE index constituent list (public equity-index membership, ESG-standing proxy); Tavily web-search API (licensed, entity-name discovery only) | Public index disclosure; paid/licensed API |

Two sources exist in code but are **not currently live**: consumidor.gov.br complaints
(default-off; its `dados.gov.br` token is presently rejected, so it returns nothing) and
Reclame Aqui reputation (default-off — the only live path sits behind Cloudflare
bot-protection, and the code explicitly does not implement evasion).

## Terms per category

- **Government/regulatory (BR + US).** Pulled from the regulator's own API/bulk-file
  endpoint — no login, no scraping. One named exception: the Receita Federal CNPJ bulk
  registry's official `gov.br` download now redirects through an SSO login a server-side
  job cannot complete, so we pull the same public dataset from a third-party community
  mirror instead. That feed only *proposes* entity-name candidates for human review — it
  never writes directly to the registry.
- **News.** Consumed as RSS syndication the publisher distributes; we store headline,
  publisher, link, and date only. No code anywhere in the pipeline fetches or extracts an
  article body — every narrative claim traces back to the original publisher's own link.
- **Public web, generally.** The stated policy (`CLAUDE.md`, `docs/CONTEXT.md`) is:
  government first; public web only logged-out; login-gated sources are "buy, don't
  build"; auth-bypass/CAPTCHA-solving is never done. In practice this is a small,
  manually-vetted allowlist of named RSS/API endpoints, not a general crawler — there is
  no code-level `robots.txt` parser, because there is no general crawling for one to gate.

## What we explicitly do not do

- Never store or reproduce full article text — metadata (headline/publisher/link/date)
  only.
- Never scrape a login-gated page or bypass bot-protection/CAPTCHA to reach a source —
  Reclame Aqui is the concrete example: wired, held off, evasion explicitly refused.
- Never scrape LinkedIn or reproduce hiring/social data other than through a licensed
  aggregator (not yet integrated).
- Never resell raw source data as a standalone feed — output is synthesized, cited
  narratives, not a redistribution of BCB/CVM/DOU bulk files.
- Never use paid credit-bureau data (e.g., Serasa) as a core signal.

## Verify this yourself

- `docs/DATA_SOURCES.md` — per-source schema, access pattern, live-verified volumes.
- `src/ingest/registry.py` — declarative source/lens registry (ADR 019).
- `src/ingest/trade_press.py` — news parsing (confirms metadata-only capture).
- `src/ingest/reclame_aqui.py`, `src/ingest/consumidor_gov.py`, `src/ingest/gov_dados.py`,
  `src/ingest/receita_bulk.py` — the sources with access caveats above.
- `CLAUDE.md`, `docs/CONTEXT.md` (search "Legal tiering") — the stated ingestion policy.
