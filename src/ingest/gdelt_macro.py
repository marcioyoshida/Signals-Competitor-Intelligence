"""O2 (#137): GDELT GKG macro-theme ingestion — a topic signal, not an entity signal.

Onça's entity-specific news is 19 direct BR outlet RSS feeds (``trade_press.py``) plus
a rejected GDELT entity-matching attempt (S0 in ``Signals-GDELT-Spine`` — GKG's
translate-then-NER pipeline destroys Brazilian company names, 7.4% hit rate). This
module deliberately does NOT match on entity names. It matches on ``V2Themes``, GKG's
separate dictionary-based topic taxonomy — a theme trigger like "juros"/"interest rate"
translates far more reliably than a proper noun does, so the S0 failure mode doesn't
apply here.

Measured 2026-09-17/18 against ``gdelt-bq.gdeltv2.gkg_partitioned`` (see the O2 issue
and ``Signals-GDELT-Spine`` addendum for the full writeup):

- The broad theme set (``ECON_STOCKMARKET`` included) pulls single-stock churn
  ("Varonis Systems and Fastly Stocks Trade Up, What You Need To Know") that isn't the
  "Fed/rates/forex/funds" macro backdrop this was built for. Dropped ``ECON_STOCKMARKET``
  and ``ECON_WORLDCURRENCIES_DOLLAR`` from the qualifying set for that reason — they're
  too broad on their own.
- ``min_offset < 200 and n_hits >= 2`` on the tightened set (still requiring the theme
  to appear early and more than once, per O2's precision gate) yields ~60 docs/day
  across a curated allowlist of sources GDELT actually crawls — Bloomberg/WSJ/FT are
  effectively absent from GKG (paywalled, blocked from crawling), same "premium press is
  invisible to GDELT" pattern already documented for Brazilian outlets.
- Cost: ~350MB/day scanned with column pruning (SourceCommonName/Extras/V2Themes/
  DocumentIdentifier only) regardless of the source/theme WHERE filter — BigQuery prunes
  by column, not by row, so don't expect the source allowlist to reduce billed bytes.
  At on-demand pricing that's a rounding error (~$0.002/day).

Scope: this module is ingestion + curation only. It does not write to the narrative
corpus or wire into ``candidates.py``/feed-build — see O3 (#138) for where an
entity-less signal fits (or doesn't) into a narrative taxonomy built around entity
attribution. It follows the same "free-standing until wired" precedent as O1's
``gdelt_bridge.py`` (no ``registry.py``/``SourceSpec`` entry needed yet).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import re
from typing import Any

# Tightened to genuinely macro/rate/forex/funds themes (see module docstring for why
# ECON_STOCKMARKET and ECON_WORLDCURRENCIES_DOLLAR were dropped from this set).
MACRO_THEMES: tuple[str, ...] = (
    "ECON_INTEREST_RATES",
    "EPU_POLICY_FEDERAL_RESERVE",
    "ECON_CENTRALBANK",
    "ECON_CURRENCY_EXCHANGE_RATE",
    "WB_341_INVESTMENT_FUNDS",
    "WB_444_MONETARY_POLICY",
)

# Curated allowlist — sources GDELT actually crawls that carry real Fed/rates/forex
# coverage, measured 2026-09-17/18. Bloomberg/WSJ/FT/Economist/Barron's/AP were tried
# and don't appear in GKG at all (paywall-blocked from crawling); left out rather than
# carried as dead weight. Re-measure before adding more — this list is not exhaustive,
# it's what was verified to actually produce on-topic hits.
SOURCE_ALLOWLIST: tuple[str, ...] = (
    "reuters.com",
    "cnbc.com",
    "businessinsider.com",
    "forbes.com",
    "morningstar.com",
    "businesstimes.com.sg",
    "financialpost.com",
    "yahoo.com",
    "fool.com",
    "marketwatch.com",
    "investing.com",
    "barrons.com",
    "thestreet.com",
    "kiplinger.com",
    "nasdaq.com",
    "benzinga.com",
    "seekingalpha.com",
)

DEFAULT_MIN_OFFSET = 200
DEFAULT_MIN_HITS = 2

_PAGE_TITLE_RE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.DOTALL)


def _build_query(target_date: dt.date, *, min_offset: int, min_hits: int) -> str:
    themes_list = ", ".join(f"'{t}'" for t in MACRO_THEMES)
    sources_list = ", ".join(f"'{s}'" for s in SOURCE_ALLOWLIST)
    # NB: BigQuery prunes by column, not by row — the SourceCommonName filter narrows
    # the *result set*, not the billed bytes. Don't rely on a tighter allowlist to cut
    # cost; it won't. See module docstring.
    return f"""
    WITH docs AS (
      SELECT DocumentIdentifier, SourceCommonName, Extras,
        (SELECT MIN(CAST(SPLIT(t,',')[OFFSET(1)] AS INT64))
           FROM UNNEST(SPLIT(V2Themes,';')) t
           WHERE SPLIT(t,',')[OFFSET(0)] IN ({themes_list})
        ) AS min_offset,
        (SELECT COUNT(*)
           FROM UNNEST(SPLIT(V2Themes,';')) t
           WHERE SPLIT(t,',')[OFFSET(0)] IN ({themes_list})
        ) AS n_hits
      FROM `gdelt-bq.gdeltv2.gkg_partitioned`
      WHERE _PARTITIONDATE = '{target_date.isoformat()}'
        AND SourceCommonName IN ({sources_list})
    )
    SELECT DocumentIdentifier, SourceCommonName, Extras
    FROM docs
    WHERE min_offset IS NOT NULL AND min_offset < {min_offset} AND n_hits >= {min_hits}
    """


def _extract_title(extras: str) -> str:
    m = _PAGE_TITLE_RE.search(extras or "")
    return html.unescape(m.group(1)).strip() if m else ""


def _looks_english(title: str) -> bool:
    """Cheap heuristic, not an NLP language detector: mostly-ASCII text.

    GDELT's crawl occasionally surfaces a non-English page under a mostly-English
    domain (e.g. a Reuters regional edition). No translation layer exists here — an
    unreadable headline is worse than a dropped one, same call ``trade_press.py``
    makes by only ever showing a source's own headline text, never a machine gloss.
    """
    if not title:
        return False
    ascii_chars = sum(1 for c in title if ord(c) < 128)
    return ascii_chars / len(title) > 0.9


def parse_rows(rows: list[dict[str, Any]], *, target_date: dt.date) -> list[dict[str, Any]]:
    """Turn raw BigQuery rows into Onça news-item dicts, deduped and English-filtered.

    Item shape mirrors ``trade_press.py``'s outlet items, with two deliberate
    differences: ``kind="macro"`` (not ``"competitor"``) and ``company``/``name`` are
    always ``None`` — this is not attributed to any tracked entity. Downstream code
    already tolerates that (``candidates.py``'s entity-less-lens fusion path).
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        title = _extract_title(row.get("Extras") or "")
        if not title or not _looks_english(title):
            continue
        publisher = row.get("SourceCommonName") or ""
        url = row.get("DocumentIdentifier") or ""
        if not url:
            continue
        sig = re.sub(r"[^a-z0-9]+", "", f"{title}|{publisher}".lower())
        item_id = "gdelt:" + hashlib.sha1(sig.encode()).hexdigest()[:16]
        if item_id in seen:
            continue
        seen.add(item_id)
        out.append(
            {
                "id": item_id,
                "source": "News",
                "kind": "macro",
                "publisher": publisher,
                "title": title,
                "subject": title,
                "company": None,
                "name": None,
                "date": target_date.isoformat(),
                "url": url,
            }
        )
    out.sort(key=lambda r: r["publisher"])
    return out


def fetch_macro_news(
    bq_client: Any,
    *,
    target_date: dt.date,
    min_offset: int = DEFAULT_MIN_OFFSET,
    min_hits: int = DEFAULT_MIN_HITS,
) -> list[dict[str, Any]]:
    """Run the theme-filtered query for one day and return curated news items.

    ``bq_client`` is a ``google.cloud.bigquery.Client`` (or any object exposing the
    same ``.query(sql).result()`` shape) — injected so this is unit-testable without
    real GCP credentials, same DI pattern the rest of this codebase uses
    (``table=None`` in ``put_tenant_config``, ``fetcher=`` in ``trade_press.py``).
    """
    query = _build_query(target_date, min_offset=min_offset, min_hits=min_hits)
    rows = [dict(row.items()) for row in bq_client.query(query).result()]
    return parse_rows(rows, target_date=target_date)
