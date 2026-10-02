"""`GET /api/feed` tier projection (ADR 016/024): an Entry tenant — paid Entry or the
14-day trial — gets the Entry slice at Entry depth for ITS module only; a SaaS tenant, or
an Entry tenant whose tier an active SaaS subscription lifted to saas, keeps full depth."""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import feed_api
from src.dashboard import tenant_config as tc
from src.dashboard.feed_builder import ENTRY_TENANT_WITHHELD_LISTS, ENTRY_TENANT_WITHHELD_STORES

RUN = "2026-09-30"


def _feed():
    cit = [{"url": "https://cvm.gov.br/x"}]
    return {
        "run_date": RUN,
        "entity_attrs": {
            "fii_a": {"industries": ["real-estate-funds"]},
            "bet_a": {"industries": ["betting"]},
            "nubank": {"industries": ["fintech"]},
        },
        "groups": {},
        "entities": [{"entity": "fii_a"}, {"entity": "bet_a"}, {"entity": "nubank"}],
        "feed": [
            {"id": "f1", "entity": "fii_a", "industries": ["real-estate-funds"], "date": RUN,
             "citations": cit},
            # a derived-axis inference: SaaS depth, never Entry
            {"id": "f2", "entity": "fii_a", "industries": ["real-estate-funds"], "date": RUN,
             "is_inference": True, "axis": "predictive", "citations": cit},
            {"id": "b1", "entity": "bet_a", "industries": ["betting"], "date": RUN, "citations": cit},
            {"id": "n1", "entity": "nubank", "industries": ["fintech"], "date": RUN, "citations": cit},
        ],
        "distress": [{"entity": "fii_a", "kind": "rj"}],
        "capital_moves": [{"entity": "fii_a"}],
        "reputation": [{"entity": "fii_a"}],
        "financials": [{"entity_id": "fii_a"}],
        "swot": {"fii_a": {"s": ["x"]}},
        "porter": {"fii_a": {}},
        "silence": [{"entity": "bet_a", "industries": ["betting"]}],
        "executive": {"officers": ["cso", "cro"], "cso": {"by_industry": {"__all__": {"n": 9}}}},
    }


class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeS3:
    def __init__(self, feed: dict) -> None:
        self._feed = feed

    def get_object(self, Bucket, Key):
        return {"Body": _FakeBody(json.dumps(self._feed).encode())}


def _call(monkeypatch, cfg):
    import boto3

    monkeypatch.setattr(tc, "get_tenant_config", lambda t: cfg)
    monkeypatch.setenv("ONCA_SITE_BUCKET", "b")
    monkeypatch.setattr(boto3, "client", lambda name: _FakeS3(_feed()))
    ev = {"requestContext": {"authorizer": {"jwt": {"claims": {
        "sub": "u", "custom:tenant": "entry-abc123def456"}}}}}
    r = feed_api.lambda_handler(ev, None)
    assert r["statusCode"] == 200
    return json.loads(r["body"])


def _assert_entry_slice(body, module):
    assert body["tier"] == "entry"
    assert body["scoped_modules"] == [module]
    # only its module, only shallow cards
    assert {c["id"] for c in body["feed"]} == {"f1"}
    assert {e["entity"] for e in body["entities"]} == {"fii_a"}
    assert set(body["entity_attrs"]) == {"fii_a"}
    # no officer dashboards, no SaaS depth
    assert body["executive"] == {"officers": [], "cso": {}}
    for k in ENTRY_TENANT_WITHHELD_LISTS:
        assert body[k] == [], k
    for k in ENTRY_TENANT_WITHHELD_STORES:
        assert body[k] == {}, k
    assert body["kpis"]["narratives_total"] == 1


def test_entry_tier_gets_entry_slice_only(monkeypatch):
    body = _call(monkeypatch, {"tier": "entry", "modules": ["real-estate-funds"]})
    _assert_entry_slice(body, "real-estate-funds")


def test_trial_tenant_gets_entry_slice_only(monkeypatch):
    until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=10)).isoformat()
    eff = tc.effective_entitlement({"tier": "entry", "modules": [], "billing_managed": True,
                                    "trial_until": until, "trial_modules": ["real-estate-funds"]},
                                   now=dt.datetime.now(dt.timezone.utc))
    assert eff["billing"]["state"] == "trial" and eff["tier"] == "entry"
    body = _call(monkeypatch, {"tier": eff["tier"], "modules": eff["modules"],
                               "billing": eff["billing"]})
    _assert_entry_slice(body, "real-estate-funds")
    assert body["billing"]["state"] == "trial"


def test_entry_tier_drops_a_non_entry_module(monkeypatch):
    """Fail closed: a module outside ENTRY_INDUSTRIES never reaches an Entry tenant."""
    body = _call(monkeypatch, {"tier": "entry", "modules": ["fintech"]})
    assert body["feed"] == [] and body["scoped_modules"] == []
    assert body["executive"] == {"officers": [], "cso": {}}


def test_saas_tenant_keeps_full_depth(monkeypatch):
    body = _call(monkeypatch, {"tier": "saas", "modules": ["real-estate-funds"]})
    assert body["tier"] == "saas"
    assert {c["id"] for c in body["feed"]} == {"f1", "f2"}
    assert body["distress"] and body["swot"] == {"fii_a": {"s": ["x"]}}
    assert body["scoped_modules"] == ["real-estate-funds"]


def test_active_saas_sub_lifts_entry_to_full_depth(monkeypatch):
    item = {"tier": "entry", "modules": [], "billing_managed": True,
            "trial_until": "2026-01-01T00:00:00+00:00", "trial_modules": ["real-estate-funds"],
            "subs": {"real-estate-funds": {"tier": "saas", "band": "saas_entry",
                                           "status": "active"}}}
    eff = tc.effective_entitlement(item, now=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc))
    assert eff["tier"] == "saas"
    body = _call(monkeypatch, {"tier": eff["tier"], "modules": eff["modules"],
                               "billing": eff["billing"]})
    assert body["tier"] == "saas"
    assert {c["id"] for c in body["feed"]} == {"f1", "f2"}
    assert body["distress"] and body["financials"]
