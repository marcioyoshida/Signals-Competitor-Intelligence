"""Quote proxy (issue #43) — industry→tickers mapping + origin-secret guard, no network."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import quotes_api as q

SECRET = "s3cret"

# WAF Phase 0: the origin gate is FAIL-CLOSED, so an unset ONCA_ORIGIN_SECRET now
# denies instead of disabling the check. Every call therefore carries the header
# CloudFront injects.
def _ev(qs=None, headers=None):
    return {"queryStringParameters": qs if qs is not None else {},
            "headers": {"x-onca-origin": SECRET} if headers is None else headers}


def _stub_quote(sym):
    return {"symbol": sym, "price": 10.0, "change": 1.5, "currency": "BRL"}


def test_industry_maps_to_representatives(monkeypatch):
    monkeypatch.setattr(q, "_quote", _stub_quote)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    body = json.loads(q.lambda_handler(_ev({"industry": "agri-funds"}), None)["body"])
    assert body["industry"] == "agri-funds"
    assert [x["symbol"] for x in body["quotes"]] == q.INDUSTRY_TICKERS["agri-funds"]


def test_named_industry_without_reps_returns_empty_with_note(monkeypatch):
    # A named sector with no curated B3 reps must NOT fall back to broad-market defaults
    # (that would misrepresent unrelated names as sector quotes) — empty + explicit note.
    monkeypatch.setattr(q, "_quote", _stub_quote)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    body = json.loads(q.lambda_handler(_ev({"industry": "consorcio"}), None)["body"])
    assert body["quotes"] == []
    assert body["note"]


def test_no_industry_uses_broad_market(monkeypatch):
    # The no-sector ("all") view has nothing narrower implied, so the broad set is honest.
    monkeypatch.setattr(q, "_quote", _stub_quote)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    body = json.loads(q.lambda_handler(_ev(), None)["body"])
    assert [x["symbol"] for x in body["quotes"]] == q.DEFAULT_TICKERS


def test_origin_secret_guard(monkeypatch):
    monkeypatch.setattr(q, "_quote", _stub_quote)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    assert q.lambda_handler({}, None)["statusCode"] == 403
    ok = q.lambda_handler({"headers": {"X-Onca-Origin": SECRET}, "queryStringParameters": {}}, None)
    assert ok["statusCode"] == 200


def test_unset_origin_secret_denies_rather_than_disabling_the_gate(monkeypatch):
    """Fail-closed regression: dropping the env var must NOT open the Yahoo proxy."""
    monkeypatch.setattr(q, "_quote", _stub_quote)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    assert q.lambda_handler(_ev(), None)["statusCode"] == 403


def test_bad_ticker_is_skipped(monkeypatch):
    monkeypatch.setattr(q, "_quote", lambda s: None if s == "BBDC4" else _stub_quote(s))
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    body = json.loads(q.lambda_handler(_ev({"industry": "banking"}), None)["body"])
    assert "BBDC4" not in [x["symbol"] for x in body["quotes"]] and body["quotes"]
