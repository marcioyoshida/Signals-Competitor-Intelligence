"""#107 (#14 Stage 3, web) — Tavily search-expansion client → harvest_ner-shaped rows."""
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import search_expansion as se


def _fake(results_by_query: dict[str, list[dict[str, Any]]]):
    """Build a fetcher that returns canned Tavily payloads per query, recording calls."""
    calls: list[dict[str, Any]] = []

    def fetch(url: str, payload: dict[str, Any], headers: dict[str, str]) -> Any:
        calls.append({"url": url, "payload": payload, "headers": headers})
        return {"query": payload["query"], "results": results_by_query.get(payload["query"], [])}

    return fetch, calls


def test_no_token_is_fail_closed(monkeypatch):
    monkeypatch.delenv("ONCA_TAVILY_TOKEN", raising=False)
    se._TOKEN_CACHE.clear()
    # no boto3 creds locally → token() returns None → [] (never raises)
    assert se.expand(["anything"], fetcher=lambda *a: {"results": [{"title": "x"}]}) == []
    assert se.search("q", tok=None, fetcher=lambda *a: {"results": [{"title": "x"}]}) == []


def test_search_builds_bearer_request_and_returns_results():
    fetch, calls = _fake({"nova fintech": [
        {"title": "Fintech Acme capta rodada", "url": "https://x/1", "content": "A Acme...",
         "score": 0.9, "id": "r1"},
    ]})
    res = se.search("nova fintech", tok="tvly-abc", fetcher=fetch,
                    include_domains=["valor.globo.com"])
    assert [r["id"] for r in res] == ["r1"]
    c = calls[0]
    assert c["url"] == se.ENDPOINT
    assert c["headers"]["Authorization"] == "Bearer tvly-abc"
    assert c["payload"]["query"] == "nova fintech"
    assert c["payload"]["topic"] == "news"
    assert c["payload"]["include_domains"] == ["valor.globo.com"]
    assert c["payload"]["language"] == "pt"


def test_max_results_clamped_to_contract_range():
    fetch, calls = _fake({})
    se.search("q", tok="t", max_results=99, fetcher=fetch)
    assert calls[0]["payload"]["max_results"] == 20
    se.search("q", tok="t", max_results=-5, fetcher=fetch)
    assert calls[1]["payload"]["max_results"] == 0


def test_expand_normalizes_and_dedups_by_url():
    fetch, _ = _fake({
        "q1": [
            {"title": "Banco Xpto autorizado", "url": "https://a/1", "content": "texto", "id": "1"},
            {"title": "", "url": "", "content": "", "id": "2"},  # dropped (no title/text)
        ],
        "q2": [
            {"title": "Banco Xpto de novo", "url": "https://a/1", "content": "outra", "id": "1b"},  # dup url
            {"title": "Seguradora Nova", "url": "https://a/3", "content": "t", "id": "3"},
        ],
    })
    items = se.expand(["q1", "q2"], tok="t", fetcher=fetch, include_domains=[])
    assert [it["url"] for it in items] == ["https://a/1", "https://a/3"]
    it = items[0]
    assert it["title"] == "Banco Xpto autorizado" and it["text"] == "texto"
    assert it["source"] == "tavily" and it["query"] == "q1"


def test_raw_content_is_folded_into_text():
    fetch, calls = _fake({"q": [
        {"title": "T", "url": "https://a/1", "content": "snippet.", "id": "1",
         "raw_content": "corpo completo do artigo com o banco digital Xyz."},
    ]})
    items = se.expand(["q"], tok="t", fetcher=fetch, include_domains=[],
                      include_raw_content="text")
    assert calls[0]["payload"]["include_raw_content"] == "text"
    assert "snippet." in items[0]["text"] and "banco digital Xyz" in items[0]["text"]


def test_expand_feeds_harvest_ner_end_to_end():
    """The whole point of #107: search rows flow into the existing NER harvester unchanged."""
    from src.synth import entity_discovery as ed

    fetch, _ = _fake({"q": [
        {"title": "Banco Zappix inicia operações",
         "url": "https://a/9", "content": "O Banco Zappix, novo banco digital, foi autorizado.",
         "id": "9"},
    ]})
    rows = se.expand(["q"], tok="t", fetcher=fetch, include_domains=[])
    # a single authoritative hit (min_mentions=1) yields a candidate the harvester recognizes
    cands = ed.harvest_ner(rows, min_mentions=1, table=_NoEntities())
    assert any("Zappix" in c["surface"] for c in cands)


class _NoEntities:
    """Registry table where nothing resolves — so the NER candidate is treated as unknown."""

    def get_item(self, Key):
        return {}

    def query(self, **kw):
        return {"Items": []}
