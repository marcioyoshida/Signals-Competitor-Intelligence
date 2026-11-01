"""#293: the Ibis Brazil module — signed receiver, tenant fan-out, watches, entitlement."""
import json
import time
from pathlib import Path

import pytest

from src.dashboard import ibis

SECRET = "whsec_test"
RECORDED = Path(__file__).parent / "contracts" / "ibis" / "1.26.0" / "recorded_webhook_event.json"


class FakeTable:
    """In-memory DynamoDB table: pk/sk keys, eq(pk) & begins_with(sk) queries."""

    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}

    def put_item(self, Item):
        self.items[(Item["pk"], Item["sk"])] = dict(Item)

    def get_item(self, Key):
        it = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(it)} if it else {}

    def delete_item(self, Key):
        self.items.pop((Key["pk"], Key["sk"]), None)

    def query(self, KeyConditionExpression, ScanIndexForward=True, Limit=None):
        eq, bw = KeyConditionExpression.get_expression()["values"]
        pk = eq.get_expression()["values"][1]
        prefix = bw.get_expression()["values"][1]
        rows = sorted((v for (p, s), v in self.items.items() if p == pk and s.startswith(prefix)),
                      key=lambda v: v["sk"], reverse=not ScanIndexForward)
        return {"Items": rows[:Limit] if Limit else rows}


def _delivery(body: bytes, *, secret=SECRET, at=None, path="/api/hooks/ibis"):
    at = int(at if at is not None else time.time())
    return {"rawPath": path, "requestContext": {"http": {"method": "POST"}},
            "headers": {"Ibis-Signature": ibis.sign(secret, body, at), "Ibis-Delivery-Id": "dlv-recorded-0001"},
            "body": body.decode("utf-8"), "isBase64Encoded": False}


def _me(method, path, tenant="bank-a", body=None, qs=None):
    return {"rawPath": path, "requestContext": {"http": {"method": method},
            "authorizer": {"jwt": {"claims": {"sub": "u1", "custom:tenant": tenant}}}},
            "body": json.dumps(body) if body is not None else None, "queryStringParameters": qs}


@pytest.fixture
def table(monkeypatch):
    t = FakeTable()
    monkeypatch.setattr(ibis, "_param", lambda name: SECRET if name == ibis.SECRET_PARAM else "x")
    return t


@pytest.fixture
def tenants(monkeypatch):
    cfgs = {"bank-a": {"tier": "saas", "modules": ["banking"]},
            "bank-b": {"tier": "sovereign", "modules": ["banking", "fintech"]},
            "entry-x": {"tier": "entry", "modules": ["agri-funds"]}}
    import src.dashboard.tenant_config as tc
    monkeypatch.setattr(tc, "get_tenant_config", lambda t: cfgs.get(t))
    return cfgs


# --- signature -------------------------------------------------------------------------------

def test_verify_accepts_ibis_scheme_and_refuses_tampering_and_staleness():
    body, now = b'{"a":1}', 1_800_000_000
    hdr = ibis.sign(SECRET, body, now)
    assert ibis.verify(SECRET, body, hdr, now=now + 299)
    assert not ibis.verify(SECRET, body, hdr, now=now + 301)          # > 5 min off
    assert not ibis.verify(SECRET, body, hdr, now=now - 301)
    assert not ibis.verify(SECRET, b'{"a":2}', hdr, now=now)          # body changed
    assert not ibis.verify("other", body, hdr, now=now)               # wrong secret
    assert not ibis.verify(SECRET, body, "", now=now)
    assert not ibis.verify(SECRET, body, "t=x,v1=00", now=now)
    assert not ibis.verify("", body, hdr, now=now)                    # unset secret fails closed


def test_signature_matches_ibis_own_reference():
    """Ibis src/monitor/webhooks.sign: HMAC-SHA256 over "<t>." + raw body, hex."""
    import hashlib
    import hmac
    body, at = b"payload", 1700000000
    mac = hmac.new(SECRET.encode(), f"{at}.".encode() + body, hashlib.sha256).hexdigest()
    assert ibis.sign(SECRET, body, at) == f"t={at},v1={mac}"


# --- receiver --------------------------------------------------------------------------------

def test_receiver_refuses_a_bad_signature_before_parsing(table):
    ev = _delivery(b"not json at all")
    ev["headers"]["Ibis-Signature"] = "t=1,v1=00"
    r = ibis.handler(ev, table=table)
    assert r["statusCode"] == 401 and not table.items


def test_delivery_fans_out_verbatim_to_each_watching_tenant_and_is_idempotent(table):
    raw = RECORDED.read_bytes()
    lk = json.loads(raw)["watch"]["lookup_key"]
    for t in ("bank-a", "bank-b"):
        table.put_item(Item={"pk": f"watch#{lk}", "sk": f"tenant#{t}", "query": {"lei": "x"}})
    r = ibis.handler(_delivery(raw), table=table)
    assert r["statusCode"] == 200 and json.loads(r["body"])["tenants"] == 2
    again = ibis.handler(_delivery(raw), table=table)          # Ibis retries the same delivery
    assert again["statusCode"] == 200
    alerts = [v for (p, s), v in table.items.items() if s.startswith("evt#")]
    assert len(alerts) == 2                                   # one per tenant, no duplicate
    src = json.loads(raw)
    for a in alerts:
        assert a["text_pt_br"] == src["text_pt_br"]           # verbatim (rule 1)
        assert a["disclaimer_pt_br"] == src["disclaimer_pt_br"]
        assert a["event"]["evidence"] == src["event"]["evidence"]
        assert a["entry"]["url"] == src["entry"]["url"]


def test_unmapped_delivery_is_kept_for_audit_and_still_acknowledged(table):
    raw = RECORDED.read_bytes()
    r = ibis.handler(_delivery(raw), table=table)
    assert r["statusCode"] == 200 and json.loads(r["body"])["tenants"] == 0
    (marker,) = [v for (p, s), v in table.items.items() if p.startswith("dlv#")]
    assert marker["raw"] == raw.decode("utf-8")


def test_test_delivery_is_acknowledged_without_alerts(table):
    body = json.dumps({"type": "ibis.test", "delivery_id": "dlv-t1", "text": "t"}).encode()
    r = ibis.handler(_delivery(body), table=table)
    assert r["statusCode"] == 200 and json.loads(r["body"]).get("test") is True
    assert not [k for k in table.items if k[1].startswith("evt#")]


def test_receiver_handles_base64_bodies(table):
    import base64
    raw = RECORDED.read_bytes()
    ev = _delivery(raw)
    ev["body"], ev["isBase64Encoded"] = base64.b64encode(raw).decode(), True
    assert ibis.handler(ev, table=table)["statusCode"] == 200


# --- tenant routes ---------------------------------------------------------------------------

class FakeIbis:
    def __init__(self, watch_status=200):
        self.calls, self.watch_status = [], watch_status

    def __call__(self, method, path, *, body=None, params=None, timeout=10.0):
        self.calls.append((method, path, body, params))
        if path == "/v1/watchlist" and method == "POST":
            q = body["queries"][0]
            key = next(f"{k.upper()}#{v}" for k, v in q.items() if k not in ("jurisdiction", "register"))
            return self.watch_status, {"results": [{"status": 200, "added": True,
                                                    "watch": {"lookup_key": key, "jurisdiction": q.get("jurisdiction")}}]}
        if path == "/v1/screen":
            return 200, {"results": [{"status": 200, "audit_id": "a-0", "answer": {
                "text": "Ibis's own English sentence.", "disclaimer": "D", "generated_at": "2026-11-01T00:00:00Z",
                "regulator_links": [{"url": "https://www.bcb.gov.br/x"}]}}]}
        if path == "/v1/check":
            return 200, {"text": "Checked now.", "disclaimer": "D", "generated_at": "g", "regulator_links": []}
        return 200, {}


def test_entry_tier_and_anonymous_callers_are_refused(table, tenants):
    assert ibis.handler(_me("GET", "/api/me/ibis", tenant="entry-x"), table=table)["statusCode"] == 403
    anon = {"rawPath": "/api/me/ibis", "requestContext": {"http": {"method": "GET"}}}
    assert ibis.handler(anon, table=table)["statusCode"] == 403
    assert ibis.handler(_me("GET", "/api/me/ibis", tenant="unknown"), table=table)["statusCode"] == 403


def test_add_watch_maps_tenant_and_returns_ibis_text_verbatim(table, tenants):
    fake = FakeIbis()
    r = ibis.handler(_me("POST", "/api/me/ibis/watch", body={"kind": "cnpj", "value": "12.345.678/0001-95"}),
                     table=table, call=fake)
    out = json.loads(r["body"])
    assert r["statusCode"] == 200 and out["lookup_key"] == "CNPJ#12345678000195"
    assert out["answer"]["text"] == "Ibis's own English sentence."      # never translated here
    assert ("watch#CNPJ#12345678000195", "tenant#bank-a") in table.items
    listed = json.loads(ibis.handler(_me("GET", "/api/me/ibis"), table=table)["body"])
    assert [w["lookup_key"] for w in listed["watches"]] == ["CNPJ#12345678000195"]


def test_name_watches_are_refused_and_identifiers_validated(table, tenants):
    fake = FakeIbis()
    for body in ({"kind": "name", "value": "Banco X"}, {"kind": "cnpj", "value": "123"},
                 {"kind": "lei", "value": "short"}, {"kind": "registration_number", "value": "A1"},
                 {"kind": "domain", "value": "localhost"}):
        r = ibis.handler(_me("POST", "/api/me/ibis/watch", body=body), table=table, call=fake)
        assert r["statusCode"] == 400, body
    assert fake.calls == []


def test_tenants_are_isolated_and_last_one_out_drops_ibis_watch(table, tenants):
    fake = FakeIbis()
    body = {"kind": "domain", "value": "https://www.Exemplo-Cripto.com/app"}
    for t in ("bank-a", "bank-b"):
        assert ibis.handler(_me("POST", "/api/me/ibis/watch", tenant=t, body=body), table=table, call=fake)["statusCode"] == 200
    lk = "DOMAIN#exemplo-cripto.com"
    # bank-b's alert is invisible to bank-a
    table.put_item(Item={"pk": "tenant#bank-b", "sk": "evt#2026-11-01#d1", "text_pt_br": "só do b"})
    a = json.loads(ibis.handler(_me("GET", "/api/me/ibis", tenant="bank-a"), table=table)["body"])
    assert a["alerts"] == []
    ibis.handler(_me("DELETE", "/api/me/ibis/watch", tenant="bank-a", body={"lookup_key": lk}), table=table, call=fake)
    assert not [c for c in fake.calls if c[0] == "DELETE"]                # bank-b still watches
    ibis.handler(_me("DELETE", "/api/me/ibis/watch", tenant="bank-b", body={"lookup_key": lk}), table=table, call=fake)
    (dele,) = [c for c in fake.calls if c[0] == "DELETE"]
    assert dele[3] == {"domain": "exemplo-cripto.com"}
    # a tenant cannot delete a watch it doesn't hold
    r = ibis.handler(_me("DELETE", "/api/me/ibis/watch", tenant="bank-a", body={"lookup_key": lk}), table=table, call=fake)
    assert r["statusCode"] == 404


def test_check_returns_ibis_answer_text_as_written(table, tenants):
    r = ibis.handler(_me("GET", "/api/me/ibis/check", qs={"kind": "lei", "value": "529900EXEMPLOFAKE042"}),
                     table=table, call=FakeIbis())
    assert json.loads(r["body"])["answer"]["text"] == "Checked now."


def test_ibis_failure_on_watch_is_reported_and_nothing_is_mapped(table, tenants):
    r = ibis.handler(_me("POST", "/api/me/ibis/watch", body={"kind": "cnpj", "value": "12345678000195"}),
                     table=table, call=FakeIbis(watch_status=503))
    assert r["statusCode"] == 502 and not table.items
