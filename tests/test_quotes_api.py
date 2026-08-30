"""Quote proxy (issue #43) — industry→tickers mapping + origin-secret guard, no network."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import quotes_api as q


def _stub_quote(sym):
    return {"symbol": sym, "price": 10.0, "change": 1.5, "currency": "BRL"}


def test_industry_maps_to_representatives(monkeypatch):
    monkeypatch.setattr(q, "_quote", _stub_quote)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    body = json.loads(q.lambda_handler({"queryStringParameters": {"industry": "agri-funds"}}, None)["body"])
    assert body["industry"] == "agri-funds"
    assert [x["symbol"] for x in body["quotes"]] == q.INDUSTRY_TICKERS["agri-funds"]


def test_unknown_industry_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(q, "_quote", _stub_quote)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    body = json.loads(q.lambda_handler({"queryStringParameters": {"industry": "consorcio"}}, None)["body"])
    assert [x["symbol"] for x in body["quotes"]] == q.DEFAULT_TICKERS


def test_origin_secret_guard(monkeypatch):
    monkeypatch.setattr(q, "_quote", _stub_quote)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", "s3cret")
    assert q.lambda_handler({}, None)["statusCode"] == 403
    ok = q.lambda_handler({"headers": {"X-Onca-Origin": "s3cret"}, "queryStringParameters": {}}, None)
    assert ok["statusCode"] == 200


def test_bad_ticker_is_skipped(monkeypatch):
    monkeypatch.setattr(q, "_quote", lambda s: None if s == "BBDC4" else _stub_quote(s))
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    body = json.loads(q.lambda_handler({"queryStringParameters": {"industry": "banking"}}, None)["body"])
    assert "BBDC4" not in [x["symbol"] for x in body["quotes"]] and body["quotes"]
