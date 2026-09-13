import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import entity_registry as reg
from src.dashboard import registry_api as api


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[Item["pk"]] = Item

    def get_item(self, Key):
        it = self.items.get(Key["pk"])
        return {"Item": it} if it is not None else {}

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


def _setup(monkeypatch):
    t = FakeTable()
    monkeypatch.setattr(reg, "_table", lambda table=None: t if table is None else table)
    reg.seed(table=t)  # 20 curated entities + taxonomy
    return t


def _ev(method, path, body=None, secret="s", qs=None):
    e = {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "headers": {"x-onca-origin": secret},
    }
    if body is not None:
        e["body"] = json.dumps(body)
    if qs:
        e["queryStringParameters"] = qs
    return e


def _call(monkeypatch, method, path, body=None, secret="s", qs=None):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", "s")
    resp = api.lambda_handler(_ev(method, path, body, secret, qs), None)
    return resp["statusCode"], json.loads(resp["body"])


def test_origin_secret_enforced(monkeypatch):
    _setup(monkeypatch)
    code, body = _call(monkeypatch, "GET", "/api/registry/entities", secret="wrong")
    assert code == 403


def test_list_and_get_entities(monkeypatch):
    _setup(monkeypatch)
    code, body = _call(monkeypatch, "GET", "/api/registry/entities")
    assert code == 200 and body["count"] == 35
    code, body = _call(monkeypatch, "GET", "/api/registry/entities/c6")
    assert code == 200 and body["entity"]["display_name"] == "C6 Bank"
    code, body = _call(monkeypatch, "GET", "/api/registry/entities/ghost")
    assert code == 404


def test_create_entity(monkeypatch):
    _setup(monkeypatch)
    code, body = _call(monkeypatch, "POST", "/api/registry/entities", {
        "entity_id": "acme", "display_name": "ACME Pay", "aliases": ["ACME PAY", "ACME"],
        "industries": ["fintech"], "news_term": "ACME Pay",
    })
    assert code == 201 and body["entity"]["entity_id"] == "acme"
    assert reg.resolve_by_alias("ACME PAY") == "acme"   # ALIAS# index written
    # duplicate -> 409
    code, _ = _call(monkeypatch, "POST", "/api/registry/entities", {
        "entity_id": "acme", "display_name": "x", "aliases": ["x"]})
    assert code == 409
    # missing fields -> 400
    code, _ = _call(monkeypatch, "POST", "/api/registry/entities", {"entity_id": "z"})
    assert code == 400


def test_patch_entity_curation(monkeypatch):
    _setup(monkeypatch)
    code, body = _call(monkeypatch, "PATCH", "/api/registry/entities/c6", {
        "news_term": "Banco C6", "ambiguous_tokens": ["c6"], "industries": ["banking", "fintech"],
    })
    assert code == 200
    ent = body["entity"]
    assert ent["news_term"] == "Banco C6"
    assert ent["ambiguous_tokens"] == ["C6"] and ent["ambiguous"] is True
    assert ent["industries"] == ["banking", "fintech"]
    # protected key ignored
    code, body = _call(monkeypatch, "PATCH", "/api/registry/entities/c6", {"entity_id": "hacked"})
    assert body["entity"]["entity_id"] == "c6"


def test_deactivate_and_include_inactive(monkeypatch):
    _setup(monkeypatch)
    code, _ = _call(monkeypatch, "DELETE", "/api/registry/entities/neon")
    assert code == 200
    _, body = _call(monkeypatch, "GET", "/api/registry/entities")
    assert "neon" not in [e["entity_id"] for e in body["entities"]]
    _, body = _call(monkeypatch, "GET", "/api/registry/entities",
                    qs={"include_inactive": "1"})
    assert "neon" in [e["entity_id"] for e in body["entities"]]


def test_add_aliases(monkeypatch):
    _setup(monkeypatch)
    code, body = _call(monkeypatch, "POST", "/api/registry/entities/c6/aliases",
                       {"aliases": ["C6 HOLDING FINANCEIRA"]})
    assert code == 200 and "C6 HOLDING FINANCEIRA" in body["added"]
    assert reg.resolve_by_alias("C6 Holding Financeira") == "c6"


def test_industries_and_reviews_endpoints(monkeypatch):
    _setup(monkeypatch)
    code, body = _call(monkeypatch, "GET", "/api/registry/industries")
    assert code == 200 and "banking" in body["industries"]
    code, body = _call(monkeypatch, "GET", "/api/registry/reviews")
    assert code == 200 and body["count"] == 0


def test_unknown_route_and_method(monkeypatch):
    _setup(monkeypatch)
    code, _ = _call(monkeypatch, "GET", "/api/registry/widgets")
    assert code == 404
    code, _ = _call(monkeypatch, "DELETE", "/api/registry/entities")
    assert code == 405


# ---- auth cutover 2026-09-11: JWT + elevated claim replaces the shared basic-auth edge ----

def _jwt_ev(method, path, *, groups=None, tier=None, email=None, body=None):
    """An event as the Cognito JWT authorizer delivers it — claims already VERIFIED
    upstream (this handler must never decode a raw bearer token itself)."""
    claims = {"sub": "u-1"}
    if groups is not None:
        claims["cognito:groups"] = groups
    if tier is not None:
        claims["custom:tier"] = tier
    if email is not None:
        claims["email"] = email
    e = {
        "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": claims}}},
        "rawPath": path,
        "headers": {},
    }
    if body is not None:
        e["body"] = json.dumps(body)
    return e


def test_elevated_jwt_is_authorized_without_any_shared_secret(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    r = api.lambda_handler(_jwt_ev("GET", "/api/registry/entities", groups=["operator"]), None)
    assert r["statusCode"] == 200


def test_authenticated_but_unelevated_identity_is_refused(monkeypatch):
    # A tenant's own token must not enumerate the curated registry — that IS the asset.
    _setup(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    r = api.lambda_handler(_jwt_ev("GET", "/api/registry/entities", tier="saas"), None)
    assert r["statusCode"] == 403
    assert "elevated" in json.loads(r["body"])["error"]


def test_no_identity_and_no_secret_is_fail_closed(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    r = api.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                            "rawPath": "/api/registry/entities", "headers": {}}, None)
    assert r["statusCode"] == 403


def test_a_configured_break_glass_secret_cannot_downgrade_a_real_identity(monkeypatch):
    # Precedence matters: if the secret were checked first, an unelevated tenant token
    # arriving through a path that also carried the secret would be waved through as
    # "operator" and journaled as such.
    _setup(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", "s")
    ev = _jwt_ev("GET", "/api/registry/entities", tier="saas")
    ev["headers"]["x-onca-origin"] = "s"
    assert api.lambda_handler(ev, None)["statusCode"] == 403


def test_writes_are_journaled_under_the_authenticated_operator(monkeypatch):
    # The point of the cutover: "who changed this" is answerable. ADR 018's journal
    # records a precedence class (`source`), which never identified a person.
    _setup(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    monkeypatch.delenv("ONCA_CURATION_ACTOR", raising=False)
    ev = _jwt_ev("POST", "/api/registry/entities", groups=["admin"],
                 email="ana@onca.example",
                 body={"entity_id": "novo-banco", "display_name": "Novo Banco",
                       "aliases": ["NOVO BANCO"]})
    assert api.lambda_handler(ev, None)["statusCode"] == 201
    import os
    assert os.environ["ONCA_CURATION_ACTOR"] == "ana@onca.example"
