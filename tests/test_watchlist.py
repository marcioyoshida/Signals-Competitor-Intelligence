"""#302 / ADR 001 step 6: per-tenant watchlist, score weights and alert prefs."""
import datetime as dt
import json

import pytest

from src.dashboard import act_api, feed_api, push, tenant_config as tc, watchlist as wl

REG = {
    "itau": {"industries": ["banking"]},
    "nubank": {"industries": ["fintech"]},
    "itau-consorcio": {"industries": ["consorcio"], "parent": "itau"},
    "xp": {"industries": ["investment-banking"]},
}


# --- validation -------------------------------------------------------------------------------
def test_clean_watchlist_dedupes_and_caps():
    assert wl.clean_watchlist(["Itau", "itau", "nubank"]) == ["itau", "nubank"]
    with pytest.raises(ValueError):
        wl.clean_watchlist(["ok", "no spaces"])
    with pytest.raises(ValueError):
        wl.clean_watchlist([f"e{i}" for i in range(wl.WATCHLIST_MAX + 1)])
    with pytest.raises(ValueError):
        wl.clean_watchlist("itau")


def test_clean_weights_and_alert_prefs():
    assert wl.clean_score_weights({"watchlist": 2, "regulatory": 1.5}) == {"watchlist": 2.0, "regulatory": 1.5}
    for bad in ({"watchlist": 9}, {"regulatory": -1}, {"Bad Key": 1}, {"product": "x"}, [1]):
        with pytest.raises(ValueError):
            wl.clean_score_weights(bad)
    assert wl.clean_alert_prefs({"only_watchlist": 1}) == {"only_watchlist": True}
    with pytest.raises(ValueError):
        wl.clean_alert_prefs({"sms": True})


def test_entitlement_boundary_follows_the_parent_chain():
    get = REG.get
    assert wl.entitled("itau", ["banking"], get)
    assert wl.entitled("itau-consorcio", ["banking"], get)      # ADR 017: a licensed parent's arm
    assert not wl.entitled("nubank", ["banking"], get)
    assert not wl.entitled("ghost", ["banking"], get)
    assert not wl.entitled("itau", [], get)


# --- read path --------------------------------------------------------------------------------
def _scoped():
    return {
        "entities": [{"entity": "bradesco", "label": "Bradesco"}, {"entity": "itau", "label": "Itaú"},
                     {"entity": "santander", "label": "Santander"}],
        "entity_attrs": {"bradesco": {}, "itau": {}, "santander": {}},
        "feed": [
            {"id": "a", "entity": "bradesco", "threat_score": 80, "lenses": ["product"], "is_alert": True,
             "date": "2026-11-02", "narrative": "A"},
            {"id": "b", "entity": "itau", "threat_score": 60, "lenses": ["regulatory"], "is_alert": True,
             "date": "2026-11-02", "narrative": "B"},
            {"id": "c", "entities": ["santander"], "threat_score": 50, "lenses": [], "is_alert": False,
             "date": "2026-11-02"},
        ],
    }


def test_watched_rank_first_and_threat_score_is_untouched():
    out = wl.apply_to_feed(_scoped(), {"watchlist": ["itau", "nubank"], "score_weights": {"watchlist": 2}})
    assert out["watchlist"] == ["itau"]                          # out of scope: dropped, never widened
    assert [r["entity"] for r in out["entities"]] == ["itau", "bradesco", "santander"]
    assert [c["id"] for c in out["feed"]] == ["b", "a", "c"]     # 60x2 > 80
    b = out["feed"][0]
    assert b["threat_score"] == 60 and b["rank_score"] == 120 and b["watched"]


def test_lens_weights_reorder_without_a_watchlist():
    out = wl.apply_to_feed(_scoped(), {"score_weights": {"product": 0.5}})
    assert [c["id"] for c in out["feed"]] == ["b", "c", "a"]     # 60, 50, 80x0.5


def test_no_prefs_leaves_the_feed_as_built():
    s = _scoped()
    out = wl.apply_to_feed(s, None)
    assert [c["id"] for c in out["feed"]] == ["a", "b", "c"] and "rank_score" not in out["feed"][0]
    assert out["watchlist"] == [] and out["alert_prefs"] == wl.ALERT_DEFAULTS


class _S3:
    def __init__(self, feed):
        self.feed = feed

    def head_object(self, Bucket, Key):
        return {"ETag": '"v1"'}

    def get_object(self, Bucket, Key):
        return {"Body": type("B", (), {"read": lambda s: json.dumps(self.feed).encode()})()}


FEED = {
    "entity_attrs": {"itau": {"industries": ["banking"]}, "bradesco": {"industries": ["banking"]}},
    "entities": [{"entity": "bradesco", "label": "Bradesco"}, {"entity": "itau", "label": "Itaú"}],
    "feed": [{"id": "a", "entity": "bradesco", "threat_score": 80, "industries": ["banking"]},
             {"id": "b", "entity": "itau", "threat_score": 60, "industries": ["banking"]}],
    "groups": {},
}
CFGS = {"acme": {"tier": "saas", "modules": ["banking"], "watchlist": ["itau"], "score_weights": {"watchlist": 2}},
        "other": {"tier": "saas", "modules": ["banking"]}}


def _get(monkeypatch, tenant, inm=None):
    import boto3
    monkeypatch.setattr(tc, "get_tenant_config", lambda t: CFGS.get(t))
    monkeypatch.setenv("ONCA_SITE_BUCKET", "b")
    monkeypatch.setattr(boto3, "client", lambda name: _S3(FEED))
    ev = {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": "u", "custom:tenant": tenant}}}}}
    if inm:
        ev["headers"] = {"if-none-match": inm}
    return feed_api.lambda_handler(ev, None)


def test_feed_order_changes_for_the_watching_tenant_only(monkeypatch):
    acme = json.loads(_get(monkeypatch, "acme")["body"])
    other = json.loads(_get(monkeypatch, "other")["body"])
    assert [c["id"] for c in acme["feed"]] == ["b", "a"] and acme["watchlist"] == ["itau"]
    assert [c["id"] for c in other["feed"]] == ["a", "b"] and other["watchlist"] == []
    assert "itau" not in json.dumps(other.get("watchlist"))


def test_a_changed_watchlist_changes_the_etag(monkeypatch):
    first = _get(monkeypatch, "acme")["headers"]["etag"]
    assert _get(monkeypatch, "acme", inm=first)["statusCode"] == 304
    CFGS["acme"] = {**CFGS["acme"], "watchlist": ["bradesco"]}
    try:
        assert _get(monkeypatch, "acme", inm=first)["statusCode"] == 200
    finally:
        CFGS["acme"] = {**CFGS["acme"], "watchlist": ["itau"]}


# --- write path -------------------------------------------------------------------------------
@pytest.fixture
def store(monkeypatch):
    saved = {}
    monkeypatch.setattr(tc, "get_tenant_config", lambda t: {"acme": {"tier": "saas", "modules": ["banking"]},
                                                            "lapsed": {"tier": "saas", "modules": []}}.get(t))
    monkeypatch.setattr(tc, "set_tenant_prefs", lambda t, **k: saved.setdefault(t, {}).update(
        {x: v for x, v in k.items() if v is not None and x != "table"}) or {
            "watchlist": saved[t].get("watchlist", []), "score_weights": saved[t].get("score_weights", {}),
            "alert_prefs": saved[t].get("alert_prefs", {})})
    monkeypatch.setattr(act_api.entity_registry, "get_entity", lambda e, **k: REG.get(e))
    return saved


def test_set_watchlist_uses_the_verified_tenant_not_an_argument(store):
    out = act_api._act_set_watchlist({"tenant": "acme", "tenant_id": "victim", "entity_ids": ["itau"]}, "u")
    assert out[0] == "applied" and store == {"acme": {"watchlist": ["itau"]}}


def test_set_watchlist_rejects_ids_outside_the_licence(store):
    outcome, status, detail = act_api._act_set_watchlist({"tenant": "acme", "entity_ids": ["itau", "nubank"]}, "u")
    assert (outcome, status, detail["entity_ids"]) == ("blocked", 400, ["nubank"]) and store == {}


def test_set_watchlist_fails_closed(store):
    assert act_api._act_set_watchlist({"tenant": None, "entity_ids": ["itau"]}, "u")[1] == 403
    assert act_api._act_set_watchlist({"tenant": "lapsed", "entity_ids": ["itau"]}, "u")[1] == 403
    assert act_api._act_set_watchlist({"tenant": "acme", "score_weights": {"x y": 1}}, "u")[1] == 400


def test_omitted_fields_are_left_alone(store):
    act_api._act_set_watchlist({"tenant": "acme", "entity_ids": ["itau"]}, "u")
    act_api._act_set_watchlist({"tenant": "acme", "alert_prefs": {"only_watchlist": True}}, "u")
    assert store["acme"] == {"watchlist": ["itau"], "alert_prefs": {"only_watchlist": True}}


def test_set_watchlist_is_tenant_self_service():
    assert "set_watchlist" in act_api._SELF_SERVICE and act_api.catalog()["set_watchlist"] == "apply"


class _Tbl:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        it = self.items.get(Key["tenant_id"])
        return {"Item": dict(it)} if it else {}

    def put_item(self, Item):
        self.items[Item["tenant_id"]] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ConditionExpression=None,
                    ReturnValues=None):
        it = self.items.get(Key["tenant_id"])
        if it is None:
            raise type("ConditionalCheckFailedException", (Exception,), {})("cond")
        for part in UpdateExpression.removeprefix("SET ").split(", "):
            k, v = part.split(" = ")
            it[k] = ExpressionAttributeValues[v]
        return {"Attributes": dict(it)}


def test_prefs_update_only_their_fields_and_survive_an_operator_reput():
    t = _Tbl()
    tc.put_tenant_config("acme", "saas", ["banking"], table=t)
    tc.set_tenant_prefs("acme", watchlist=["itau"], score_weights={"watchlist": 2.0}, table=t)
    assert t.items["acme"]["modules"] == ["banking"] and t.items["acme"]["watchlist"] == ["itau"]
    tc.put_tenant_config("acme", "saas", ["banking", "fintech"], table=t)   # operator re-provision
    cfg = tc.get_tenant_config("acme", table=t)
    assert cfg["watchlist"] == ["itau"] and cfg["score_weights"] == {"watchlist": 2.0}
    with pytest.raises(KeyError):
        tc.set_tenant_prefs("ghost", watchlist=[], table=t)


# --- alerts -----------------------------------------------------------------------------------
NOW = dt.datetime(2026, 11, 2, 12, 0, tzinfo=push.BRT)
PFEED = {"entities": [{"entity": "itau"}, {"entity": "bradesco"}], "entity_attrs": {},
         "feed": [{"id": "w1", "entity": "itau", "is_alert": True, "date": "2026-11-02"},
                  {"id": "n1", "entity": "bradesco", "is_alert": True, "date": "2026-11-02"}],
         "product_radar": {"alerts": [{"id": "r1", "entity": "bradesco", "date": "2026-11-02"},
                                      {"id": "r2", "entity": "itau", "date": "2026-11-02"}]},
         "sector_events": [{"id": "s1", "severity": "critical", "date": "2026-11-02"}], "distress": []}


def _notify(cfg):
    s = {"pk": "SUB#a", "endpoint": "https://push.example/a", "tenant": "acme", "groups": [], "officer": "cso",
         "prefs": {"types": list(push.TYPES), "daily_cap": 5, "quiet": [21, 7]}, "seen": [], "pending": 0,
         "fails": 0, "primed": True}
    push.notify(PFEED, [s], now=NOW, send=lambda ep, count=0: 201, save=lambda x: None,
                drop=lambda x: None, bump=lambda k: None, scope=lambda f, m: f, modules=lambda t, g: ["banking"],
                tenant_prefs=lambda t: cfg)
    return sorted(s["seen"])


def test_push_adds_watch_events_and_can_narrow_to_the_watchlist():
    assert _notify(None) == ["radar:r1", "radar:r2", "regulatory:s1"]
    assert _notify({"watchlist": ["itau"]}) == ["radar:r1", "radar:r2", "regulatory:s1", "watch:w1"]
    assert _notify({"watchlist": ["itau"], "alert_prefs": {"only_watchlist": True}}) == ["radar:r2", "watch:w1"]
    assert _notify({"watchlist": ["itau"], "alert_prefs": {"push": False}}) == ["radar:r1", "radar:r2", "regulatory:s1"]
    assert _notify({"watchlist": ["nubank"]}) == ["radar:r1", "radar:r2", "regulatory:s1"]  # out of scope


def test_digest_section_lists_the_weeks_alerts_on_watched_entities():
    part = wl.digest_part({**PFEED, "entities": [{"entity": "itau", "label": "Itaú"}],
                           "feed": [{**PFEED["feed"][0], "narrative": "Itaú lança produto", "threat_score": 70}]},
                          ["itau"], today="2026-11-02")
    assert part["lines"] == ["Itaú: Itaú lança produto"] and "Itaú" in part["headline"]
    assert wl.digest_part(PFEED, [], today="2026-11-02") is None
