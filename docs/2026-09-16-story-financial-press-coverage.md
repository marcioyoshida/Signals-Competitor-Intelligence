# Story — Brazilian financial-press coverage: close the Exame/InfoMoney gap

Filed 2026-09-16, out of the GDELT S0 spike
(`Signals-GDELT-Spine/docs/2026-09-16-s0-findings.md`), which proved GDELT carries none of
the Brazilian specialist financial press and recommended ingesting it directly instead.

**That recommendation was mostly wrong, and this story is what survives it.** Three of the
four outlets it named — Valor, Brazil Journal, NeoFeed — have been ingested as direct RSS
feeds since 2026-08-16. Only Exame is missing, plus InfoMoney. Verified in
`src/ingest/trade_press.py:40` (`OUTLET_FEEDS`), not inferred.

## Verified state — 2026-09-16

All 17 configured outlet feeds were probed live today. **Every one returns HTTP 200 with
items from today or within 6 days.** Nothing is silently dead:

| Publisher | Items | Newest |
| --- | ---: | --- |
| Valor Econômico (`financas`, `empresas`) | 100 each | today |
| Brazil Journal, NeoFeed | 10 each | today / 2 d |
| Money Times ×2, Startups, Startupi, iGaming Brazil | 10 each | today |
| CQCS 50, Sonho Seguro 20, Cointelegraph BR 30, CriptoFácil 15 | — | today |
| Revista Apólice, Revista Cobertura | 10 each | today |
| Funds Explorer | 10 | 6 d |

Valor at 100 items per feed across two sections is the deepest source in the set. The
"Brazilian financial press is a coverage gap" framing does not survive contact with the
code — the spine is already there and healthy.

## The actual gap

`Exame` and `InfoMoney` are named in `trade_press.py`'s docstring as outlets covered *via
Google News RSS aggregation*, and `exame.com` appears in `search_expansion.py:45`. Neither
has a direct feed. That leaves them on a materially worse path than the other 17:

1. **Worse citations.** Google News yields redirect URLs, not publisher links. The
   codebase already flags this as the reason direct feeds exist at all
   (`trade_press.py:36` — "direct publisher links, better citations than Google News
   redirects"). Citation quality is the product's positioning, not a detail.
2. **Query-gated recall.** The Google News path is driven by watchlist `terms` with
   `max_per_term=10`. Anything an outlet publishes about a tracked entity that doesn't rank
   in that outlet's top-10 for the term is invisible. Direct feeds are pulled wholesale and
   filtered locally, so recall is bounded by the feed, not by Google's ranking.

Both candidate feeds are live and parse (probed 2026-09-16):

| Candidate | Status | Items | Note |
| --- | --- | ---: | --- |
| `https://www.infomoney.com.br/feed/` | 200 | 10 | RFC-822 dates, clean, finance-only |
| `https://exame.com/invest/feed/` | 200 | 25 | the finance section — **prefer this** |
| `https://exame.com/feed/` | 200 | 25 | general interest; top item today was an Oscar story |

## The blocker — Exame breaks the date parser

This is the part worth knowing before anyone estimates the work. Exame emits **ISO-8601**
in `pubDate`:

```xml
<pubDate>2026-09-16T20:36:44</pubDate>
```

Every other feed emits RFC-822 (`Wed, 16 Sep 2026 ...`). The pipeline is:

- `_parse_feed` → `_iso(pubdate)` (`trade_press.py:298`) → `parsedate_to_datetime(...)`,
  which **raises on ISO-8601** and is caught into `return ""`.
- `fetch_news` → `_parse_date("")` (`:305`) → `None` → `if not date or date < cutoff:
  continue`.

Net effect: **adding Exame's feed without touching `_iso` drops 100% of its items,
silently, with no error.** The feed would look wired and contribute nothing — the same
failure shape as a dead feed, which is exactly what the health probe above exists to catch.

Fix is small and general: give `_iso` an ISO-8601 fallback before returning `""`. That
helps any future feed with the same habit.

## Work

| # | Item | Est. | Note |
| - | ---- | ---- | ---- |
| 1 | `_iso` ISO-8601 fallback + unit test | 0.5 d | Do this first, independently. Fixture both date shapes; assert neither returns `""` |
| 2 | Add `("Exame", ".../invest/feed/")` and `("InfoMoney", ".../feed/")` to `OUTLET_FEEDS` | 0.5 d | Follow the existing comment convention — publisher, why the outlet, date verified |
| 3 | Live verification pass | 0.5 d | Run `trade_press.inspect()` against the live watchlist; confirm non-zero items from both, with publisher links (not `news.google.com`) |
| 4 | Feed-health check | 0.5–1 d | The probe written for this story, as a repeatable check. See scope note below |

Total ≈ 2 d. No new infra, no new service, no new dependency — this is a list edit plus a
parser fallback.

## Scope note on item 4

A dead outlet feed degrades coverage silently: `_fetch_url` fails or returns nothing, and
the run continues. Today's probe found everything healthy, so this is preventative rather
than a live fix, and it could reasonably be deferred.

Two homes for it, and the choice is not obvious:
- **Inside Onça** as a pipeline freshness signal, next to the ADR 024 G1 freshness SLO work
  (#109) — same alarm channel, same operator.
- **In fleet-monitor**, which already probes cross-fork health and would surface it on the
  existing dashboard.

Recommend the first: a stale RSS feed is an ingestion-quality problem, not a service-uptime
problem, and the people who act on it are already looking at #109's alarms.

## What this story is not

An honest bound on the value: this adds two outlets to a set of seventeen that are already
working. It improves citation quality and recall for Exame and InfoMoney specifically. It
does **not** meaningfully change Brazilian press coverage overall, and it should not be
sequenced ahead of anything on the ADR 024 launch path.

One observed limitation, noted and deliberately not addressed here: outlet items are
matched on **headline text only** (`title_f` in `fetch_news`). An article about a tracked
entity that doesn't name it in the headline is dropped. Widening that to summary/`description`
would likely lift recall more than adding outlets does — but it changes the precision
profile of every existing feed at once, so it belongs in its own story with its own
false-positive measurement, not bolted onto this one.
