# Sources & licensing

What Onça ingests, and under what terms. This is a summary of the discipline enforced
in code (`src/ingest/registry.py`, `docs/DATA_SOURCES.md`) — every claim below is
defensible line-by-line against the ingesters, not a policy statement layered on top.

## What we ingest

**Government open-data APIs, first.** The majority of signal comes from official
regulator/registry sources published as open data, free of charge, with no
authentication and no usage-restrictive licence:

- **Banco Central do Brasil (BCB)** — Pix DICT key counts, institutions in operation,
  average interest rates (juros médios), IF.data institution financials, prudential
  ratios (Basileia/inadimplência), Focus market expectations.
- **CVM** — securities offerings (ICVM 400/RCVM 160), fund registry and daily fund
  reports, listed-company financial statements (DFP/ITR/FRE).
- **PNCP** (public procurement), **CADE** (merger review), **CEIS/CNEP** (sanctions
  registries), **consumidor.gov.br** (consumer complaint index), **DataJud** (court
  filing index, party-name-scrubbed at the public API), **SUSEP** (insurance product
  registry, where token-accessible).
- **SEC EDGAR** — for US-listed Brazilian issuers (Stone, PagSeguro, Nu Holdings,
  Inter&Co, XP), accessed via the free EDGAR APIs with the required descriptive
  User-Agent.

**Logged-out, robots-respecting web, second.** News and press signals (Google News,
publisher RSS, DOU — Diário Oficial da União) are read the same way any browser or
search crawler would: no login wall is ever bypassed, `robots.txt` is respected, and
**we ingest headline, publisher, publication date, and a link — never the article
body.** Onça reasons about and cites *that a story ran*, not a copy of the story
itself. This is a deliberate ingestion-time boundary (`src/ingest/registry.py`
`SourceSpec`), not a display-time redaction.

## What we do not do

- No login-gated scraping — nothing behind a paywall or an authenticated session is
  ever ingested.
- No purchased/aggregated third-party datasets of uncertain provenance.
- No copying of full article text, images, or paywalled content.
- No source is ingested "because it might be useful" — each has a named
  `SourceSpec` and a documented CI rationale in `docs/DATA_SOURCES.md`.

## Freshness discipline

Every card and framework output carries the **as-of date of its underlying source**,
not the date the pipeline ran. A quarterly regulator filing is labeled quarterly; we
never imply daily freshness a monthly/quarterly feed doesn't have.

## Attribution & citation

Every synthesized claim in the product traces back to a source URL where the
upstream provides one (government portal page, official filing, or the specific news
URL for a headline). The grounded Q&A agent (`/api/ask`) will not answer beyond what
its retrieved, cited sources support — an ungrounded question gets an honest decline,
not a fabricated answer.
