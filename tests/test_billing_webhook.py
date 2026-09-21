import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from src.dashboard import billing_webhook as bw  # noqa: E402

SECRET = "whsec_test_secret"


def _sign(payload: str, secret: str = SECRET, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def _session(email="buyer@acme.com", price="price_entry", paid="paid"):
    return {
        "id": "evt_1",
        "type": "checkout.session.completed",
        "data": {"object": {
            "payment_status": paid,
            "customer_details": {"email": email},
            "metadata": {"price_id": price},
        }},
    }


class FakeTable:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        it = self.items.get(Key["tenant_id"])
        return {"Item": it} if it else {}

    def put_item(self, Item):
        self.items[Item["tenant_id"]] = Item


# --- signature verification -------------------------------------------------

def test_valid_signature_passes():
    body = '{"a":1}'
    bw.verify_signature(body, _sign(body), SECRET)


def test_tampered_body_is_rejected():
    body = '{"amount":100}'
    header = _sign(body)
    with pytest.raises(bw.SignatureError):
        bw.verify_signature('{"amount":1}', header, SECRET)


def test_wrong_secret_is_rejected():
    body = '{"a":1}'
    with pytest.raises(bw.SignatureError):
        bw.verify_signature(body, _sign(body), "whsec_other")


def test_missing_secret_fails_CLOSED():
    # The critical one: no configured secret must reject, never allow.
    body = '{"a":1}'
    with pytest.raises(bw.SignatureError):
        bw.verify_signature(body, _sign(body), None)
    with pytest.raises(bw.SignatureError):
        bw.verify_signature(body, _sign(body), "")


def test_replayed_old_event_is_rejected():
    body = '{"a":1}'
    old = int(time.time()) - 4000
    with pytest.raises(bw.SignatureError):
        bw.verify_signature(body, _sign(body, ts=old), SECRET)


def test_multiple_v1_signatures_are_all_tried():
    # Stripe sends several v1 values during secret rotation.
    body = '{"a":1}'
    ts = int(time.time())
    good = hmac.new(SECRET.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    bw.verify_signature(body, f"t={ts},v1=deadbeef,v1={good}", SECRET)


def test_malformed_header_is_rejected():
    for header in ("", "garbage", "v1=abc", "t=123"):
        with pytest.raises(bw.SignatureError):
            bw.verify_signature("{}", header, SECRET)


# --- plan resolution --------------------------------------------------------

def test_plan_comes_from_the_price_not_the_payload(monkeypatch):
    monkeypatch.setenv("ONCA_BILLING_PRICES", json.dumps(
        {"price_entry": {"tier": "entry", "modules": ["consorcio"]}}))
    assert bw.plan_for_price("price_entry")["modules"] == ["consorcio"]
    assert bw.plan_for_price("price_unknown") is None
    assert bw.plan_for_price(None) is None


def test_malformed_price_catalog_recognises_nothing(monkeypatch):
    monkeypatch.setenv("ONCA_BILLING_PRICES", "{not json")
    assert bw.plan_for_price("price_entry") is None


def test_extract_price_prefers_expanded_line_items():
    s = {"line_items": {"data": [{"price": {"id": "price_expanded"}}]},
         "metadata": {"price_id": "price_meta"}}
    assert bw.extract_price_id(s) == "price_expanded"
    assert bw.extract_price_id({"metadata": {"price_id": "price_meta"}}) == "price_meta"
    assert bw.extract_price_id({}) is None


def test_extract_email_reads_either_field():
    assert bw.extract_email({"customer_details": {"email": "A@B.com"}}) == "a@b.com"
    assert bw.extract_email({"customer_email": "c@d.com"}) == "c@d.com"
    assert bw.extract_email({}) is None


# --- provisioning -----------------------------------------------------------

def test_provisions_an_entry_tenant(monkeypatch):
    monkeypatch.setenv("ONCA_BILLING_PRICES", json.dumps(
        {"price_entry": {"tier": "entry", "modules": ["consorcio"]}}))
    t, f = FakeTable(), FakeTable()
    rep = bw.provision_from_session(_session(), table=t, federated_table=f)
    assert rep["status"] == "provisioned"
    assert rep["modules"] == ["consorcio"]
    assert rep["tenant_id"].startswith("entry-")


def test_a_stripe_retry_does_not_create_a_second_tenant(monkeypatch):
    monkeypatch.setenv("ONCA_BILLING_PRICES", json.dumps(
        {"price_entry": {"tier": "entry", "modules": ["consorcio"]}}))
    t, f = FakeTable(), FakeTable()
    first = bw.provision_from_session(_session(), table=t, federated_table=f)
    second = bw.provision_from_session(_session(), table=t, federated_table=f)
    assert first["status"] == "provisioned"
    assert second["status"] == "duplicate"
    tenants = [k for k in t.items if not k.startswith(bw.IDEMPOTENCY_PK)]
    assert len(tenants) == 1


def test_an_unpaid_session_entitles_nobody(monkeypatch):
    monkeypatch.setenv("ONCA_BILLING_PRICES", json.dumps(
        {"price_entry": {"tier": "entry", "modules": ["consorcio"]}}))
    t = FakeTable()
    rep = bw.provision_from_session(_session(paid="unpaid"), table=t,
                                    federated_table=FakeTable())
    assert rep["status"] == "ignored"
    assert t.items == {}


def test_an_unknown_price_entitles_nobody(monkeypatch):
    monkeypatch.setenv("ONCA_BILLING_PRICES", "{}")
    t = FakeTable()
    rep = bw.provision_from_session(_session(), table=t, federated_table=FakeTable())
    assert rep["status"] == "error"
    assert t.items == {}


def test_a_price_cannot_smuggle_a_non_entry_module(monkeypatch):
    # Independent second gate: even if the catalog is wrong, put_tenant_config's
    # allow-list rejects a module the entry tier may not license.
    monkeypatch.setenv("ONCA_BILLING_PRICES", json.dumps(
        {"price_entry": {"tier": "entry", "modules": ["banking"]}}))
    with pytest.raises(ValueError):
        bw.provision_from_session(_session(), table=FakeTable(),
                                  federated_table=FakeTable())


def test_other_event_types_are_ignored():
    ev = _session()
    ev["type"] = "invoice.paid"
    assert bw.provision_from_session(ev, table=FakeTable())["status"] == "ignored"


# --- handler ----------------------------------------------------------------

def test_handler_rejects_an_unsigned_request(monkeypatch):
    monkeypatch.setenv("ONCA_STRIPE_WEBHOOK_SECRET", SECRET)
    out = bw.lambda_handler({"body": "{}", "headers": {}})
    assert out["statusCode"] == 400
    assert "signature" in json.loads(out["body"])["error"]


def test_handler_rejects_when_no_secret_is_configured(monkeypatch):
    monkeypatch.delenv("ONCA_STRIPE_WEBHOOK_SECRET", raising=False)
    body = json.dumps(_session())
    out = bw.lambda_handler({"body": body, "headers": {"Stripe-Signature": _sign(body)}})
    assert out["statusCode"] == 400


def test_handler_accepts_a_signed_request(monkeypatch):
    monkeypatch.setenv("ONCA_STRIPE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("ONCA_BILLING_PRICES", json.dumps(
        {"price_entry": {"tier": "entry", "modules": ["consorcio"]}}))
    from src.dashboard import tenant_config

    t = FakeTable()
    monkeypatch.setattr(bw, "_table", lambda table=None: table if table is not None else t)
    monkeypatch.setattr(tenant_config, "_table", lambda table=None: table or t)
    monkeypatch.setattr(tenant_config, "_federated_table", lambda table=None: table or FakeTable())
    body = json.dumps(_session())
    out = bw.lambda_handler({"body": body,
                             "headers": {"Stripe-Signature": _sign(body)}})
    assert out["statusCode"] == 200
    assert json.loads(out["body"])["status"] == "provisioned"


def test_handler_decodes_a_base64_body(monkeypatch):
    import base64

    monkeypatch.setenv("ONCA_STRIPE_WEBHOOK_SECRET", SECRET)
    body = json.dumps(_session())
    out = bw.lambda_handler({
        "body": base64.b64encode(body.encode()).decode(), "isBase64Encoded": True,
        "headers": {"Stripe-Signature": _sign(body)}})
    # signature verified against the DECODED body, so this gets past the gate
    assert out["statusCode"] != 400
