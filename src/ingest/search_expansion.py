"""#107 (#14 Stage 3, web) — web-search expansion source (Tavily) → NER harvest candidates.

The entity-discovery pipeline's consumer side already exists: `entity_discovery.harvest_ner`
extracts company-name candidates from a corpus and PROPOSES them (never auto-creates, ADR 011
§4). What was missing was a SEARCH SOURCE that reaches companies the daily ingest never
mentions. This module is that source: it queries Tavily's Search API for BR financial-services
discovery terms and returns result items shaped exactly like the narrative corpus rows
`harvest_ner` already consumes (`id`/`title`/`text`), so the harvester is reused unchanged.

Tavily Search REST contract (verified against docs.tavily.com, 2026-09-07):
  POST https://api.tavily.com/search
  auth   header ``Authorization: Bearer <ONCA_TAVILY_TOKEN>``
  body   { query, topic(general|news|finance), max_results(0-20), search_depth,
           time_range(day|week|month|year), include_domains[], language(ISO 639-1), ... }
  resp   { query, results: [ {title, url, content, score, id} ], response_time, request_id }

Token: read from ``ONCA_TAVILY_TOKEN`` env, else the ``signalscompetitor/onca/api-key`` secret
(JSON key ``ONCA_TAVILY_TOKEN``). With no token this client returns ``[]`` (fail-closed), so
the ingest wiring built on it is simply inert until the token is set — no fabricated results.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Iterable

ENDPOINT = "https://api.tavily.com/search"
_SECRET_ID = "signalscompetitor/onca/api-key"
_TOKEN_CACHE: dict[str, str | None] = {}

# Default discovery queries: BR financial-services firms likely absent from the daily corpus.
# Portuguese, phrased to surface named companies (not macro news). Overridable via env.
DEFAULT_QUERIES: tuple[str, ...] = (
    "nova fintech brasileira autorizada Banco Central",
    "nova instituição de pagamento autorizada Bacen",
    "nova seguradora autorizada SUSEP",
    "nova gestora de recursos registrada CVM",
    "banco digital brasileiro lançamento",
    "corretora de valores nova B3",
)

# Restricting to reputable BR financial-press domains cuts result noise sharply. Overridable
# via ONCA_SEARCH_DOMAINS (comma-separated); empty string disables the filter.
DEFAULT_DOMAINS: tuple[str, ...] = (
    "valor.globo.com", "infomoney.com.br", "exame.com", "neofeed.com.br",
    "braziljournal.com", "moneytimes.com.br",
)


def token() -> str | None:
    """ONCA_TAVILY_TOKEN from env, else the api-key secret (JSON). Cached; None if absent."""
    if "v" in _TOKEN_CACHE:
        return _TOKEN_CACHE["v"]
    tok = os.environ.get("ONCA_TAVILY_TOKEN")
    if not tok:
        try:
            import boto3
            raw = boto3.client("secretsmanager").get_secret_value(SecretId=_SECRET_ID)["SecretString"]
            tok = (json.loads(raw) or {}).get("ONCA_TAVILY_TOKEN")
        except Exception as exc:  # pragma: no cover - secret unavailable locally
            print(f"Warning: ONCA_TAVILY_TOKEN unavailable: {exc}")
            tok = None
    _TOKEN_CACHE["v"] = tok
    return tok


Fetcher = Callable[[str, dict[str, Any], dict[str, str]], Any]


def _default_fetch(url: str, payload: dict[str, Any], headers: dict[str, str]) -> Any:
    import requests
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def search(
    query: str,
    *,
    topic: str = "news",
    max_results: int = 10,
    search_depth: str = "basic",
    time_range: str | None = "month",
    include_domains: Iterable[str] | None = None,
    include_raw_content: str | bool | None = None,
    language: str | None = "pt",
    tok: str | None = None,
    fetcher: Fetcher | None = None,
) -> list[dict[str, Any]]:
    """Run one Tavily search. Returns the raw ``results`` list (may be empty). Best-effort:
    any error (no token, network, bad status) yields ``[]`` — never raises to the caller."""
    tok = tok or token()
    if not tok:
        return []
    payload: dict[str, Any] = {
        "query": query, "topic": topic, "max_results": max(0, min(20, int(max_results))),
        "search_depth": search_depth,
    }
    if include_raw_content:
        payload["include_raw_content"] = include_raw_content
    if time_range:
        payload["time_range"] = time_range
    if language:
        payload["language"] = language
    doms = [d for d in (include_domains or []) if d]
    if doms:
        payload["include_domains"] = doms
    try:
        data = (fetcher or _default_fetch)(
            ENDPOINT, payload, {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    except Exception as exc:  # pragma: no cover - network/status best-effort
        print(f"Warning: Tavily search failed for {query!r}: {exc}")
        return []
    results = (data or {}).get("results") if isinstance(data, dict) else None
    return list(results or [])


# Cap raw article text folded into the corpus row — enough prose for the anchored NER cues
# to fire without ballooning memory across dozens of results.
_RAW_CHARS = 4000


def _to_item(r: dict[str, Any], query: str) -> dict[str, Any] | None:
    """Map a Tavily result to a corpus-shaped row for ``harvest_ner`` (id/title/text).

    When the result carries ``raw_content`` (requested via ``include_raw_content``), it is
    appended to the snippet ``content`` — headline snippets rarely contain the "Banco X" /
    "a fintech X" cues the NER heuristic anchors on, but full-article prose does."""
    if not isinstance(r, dict):
        return None
    url = str(r.get("url") or "").strip()
    title = str(r.get("title") or "").strip()
    text = str(r.get("content") or "").strip()
    raw = str(r.get("raw_content") or "").strip()
    if raw:
        text = (text + " " + raw[:_RAW_CHARS]).strip()
    if not (title or text):
        return None
    return {
        "id": str(r.get("id") or url or title),
        "title": title,
        "text": text,
        "url": url or None,
        "score": r.get("score"),
        "source": "tavily",
        "query": query,
    }


def expand(
    queries: Iterable[str] | None = None,
    *,
    per_query: int = 10,
    include_domains: Iterable[str] | None = None,
    tok: str | None = None,
    fetcher: Fetcher | None = None,
    **search_kwargs: Any,
) -> list[dict[str, Any]]:
    """Run all discovery queries and return de-duplicated corpus rows for ``harvest_ner``.

    De-dupes across queries by URL (falling back to id). Fail-closed: no token ⇒ ``[]``.
    """
    tok = tok or token()
    if not tok:
        return []
    qs = list(queries) if queries is not None else list(DEFAULT_QUERIES)
    doms = include_domains if include_domains is not None else DEFAULT_DOMAINS
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for q in qs:
        for r in search(q, max_results=per_query, include_domains=doms,
                        tok=tok, fetcher=fetcher, **search_kwargs):
            it = _to_item(r, q)
            if not it:
                continue
            dedup = it["url"] or it["id"]
            if dedup in seen:
                continue
            seen.add(dedup)
            items.append(it)
    return items
