"""#347 (BYOK story 2): the sealed per-tenant model key and its /api/me/act intents."""
import base64
import json

import pytest

from src.dashboard import act_api, tenant_config
from src.synth import client_model as cm
from src.synth import entity_registry as er
from src.synth import model_keys as mk

KEY = "sk-ant-api03-SECRETSECRETSECRET-wxyz"
CTX = {"onca-tenant": "t-1", "onca-purpose": "model-key"}


class _Table:
    def __init__(self):
        self.rows = {}

    def get_item(self, Key):
        r = self.rows.get(Key["tenant_id"])
        return {"Item": dict(r)} if r else {}

    def put_item(self, Item):
        self.rows[Item["tenant_id"]] = dict(Item)

    def delete_item(self, Key, ReturnValues=None):
        old = self.rows.pop(Key["tenant_id"], None)
        return {"Attributes": old} if old else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ConditionExpression=None):
        row = self.rows.get(Key["tenant_id"])
        if row is None:
            raise RuntimeError("ConditionalCheckFailedException")
        for part in UpdateExpression.removeprefix("SET ").split(", "):
            k, v = part.split(" = ")
            row[k] = ExpressionAttributeValues[v]


class _Kms:
    """Encrypt = reverse + tag with the context; decrypt checks the context like KMS does."""

    def __init__(self):
        self.calls = []

    def encrypt(self, KeyId, Plaintext, EncryptionContext):
        self.calls.append(("encrypt", dict(EncryptionContext)))
        blob = json.dumps({"ctx": EncryptionContext, "p": Plaintext.decode()[::-1]}).encode()
        return {"CiphertextBlob": blob}

    def decrypt(self, CiphertextBlob, EncryptionContext):
        self.calls.append(("decrypt", dict(EncryptionContext)))
        d = json.loads(CiphertextBlob)
        if d["ctx"] != EncryptionContext:
            raise RuntimeError("InvalidCiphertextException")
        return {"Plaintext": d["p"][::-1].encode()}


@pytest.fixture
def store(monkeypatch):
    t, k = _Table(), _Kms()
    monkeypatch.setenv(mk.TABLE_ENV, "keys")
    monkeypatch.setenv(mk.KMS_ENV, "arn:aws:kms:us-east-1:1:key/x")
    monkeypatch.setattr(mk, "_table", lambda table=None: t)
    monkeypatch.setattr(mk, "_kms", lambda kms=None: k)
    return t, k


def _ok(target):
    return None


# --- the store ----------------------------------------------------------------------

def test_connect_verifies_first_then_seals_with_the_tenant_context(store):
    t, k = store
    seen = []
    conn = mk.connect("t-1", api_key=f"  {KEY} ", model="claude-sonnet-5-5", actor="u-1",
                      acknowledged=True, verify=lambda tg: seen.append(tg.api_key))
    assert seen == [KEY]                                   # verified on the real (trimmed) key
    assert k.calls == [("encrypt", CTX)]
    row = t.rows["t-1"]
    assert KEY not in json.dumps(row) and row["last4"] == "wxyz" and conn.last4 == "wxyz"
    assert KEY not in json.dumps(conn.public())
    assert mk.load_target("t-1").api_key == KEY


def test_a_rejected_key_is_never_stored(store):
    t, k = store

    def reject(tg):
        raise cm.ModelError(cm.KEY_REJECTED)
    with pytest.raises(cm.ModelError):
        mk.connect("t-1", api_key=KEY, model="claude-sonnet-5-5", actor="u", acknowledged=True,
                   verify=reject)
    assert t.rows == {} and k.calls == []


@pytest.mark.parametrize("kw,msg", [
    ({"acknowledged": False}, "aceite"), ({"api_key": ""}, "chave"),
    ({"model": "claude-opus-5-5"}, "modelos permitidos")])
def test_bad_input_is_refused_before_any_call(store, kw, msg):
    args = {"api_key": KEY, "model": "claude-sonnet-5-5", "actor": "u", "acknowledged": True, **kw}
    with pytest.raises(ValueError, match=msg):
        mk.connect("t-1", verify=lambda tg: pytest.fail("verified"), **args)


def test_one_tenants_ciphertext_cannot_be_read_as_anothers(store):
    t, _ = store
    mk.connect("t-1", api_key=KEY, model="claude-sonnet-5-5", actor="u", acknowledged=True, verify=_ok)
    t.rows["t-2"] = {**t.rows["t-1"], "tenant_id": "t-2"}      # copied row, wrong context
    with pytest.raises(RuntimeError):
        mk.load_target("t-2")


def test_disconnect_stops_the_next_call(store):
    mk.connect("t-1", api_key=KEY, model="claude-sonnet-5-5", actor="u", acknowledged=True, verify=_ok)
    assert mk.disconnect("t-1") is True and mk.load_target("t-1") is None
    assert mk.disconnect("t-1") is False


def test_stamps_update_an_existing_row_only(store):
    t, _ = store
    mk.record_check("t-1", error=cm.KEY_REJECTED)               # no row: nothing resurrected
    assert t.rows == {}
    mk.connect("t-1", api_key=KEY, model="claude-sonnet-5-5", actor="u", acknowledged=True, verify=_ok)
    mk.record_check("t-1", error=cm.NO_CREDIT)
    assert mk.connection("t-1").public()["last_error_message"] == cm.MESSAGES[cm.NO_CREDIT]
    mk.record_check("t-1")
    assert mk.connection("t-1").last_error == ""


# --- the loader the answer paths use ------------------------------------------------

def test_loader_is_off_where_the_store_is_not_configured(monkeypatch):
    monkeypatch.delenv(mk.TABLE_ENV, raising=False)
    assert cm.tenant_loader() is None


def test_loader_failure_is_a_visible_error_never_hosted(store, monkeypatch):
    def boom(tenant, **kw):
        raise RuntimeError("AccessDenied")
    monkeypatch.setattr(mk, "load_target", boom)
    with pytest.raises(cm.ModelError) as e:
        cm.resolve_target("t-1", loader=cm.tenant_loader())
    assert e.value.code == cm.UNAVAILABLE


def test_loader_resolves_a_connected_tenant(store):
    mk.connect("t-1", api_key=KEY, model="claude-sonnet-5-5", actor="u", acknowledged=True, verify=_ok)
    tg = cm.resolve_target("t-1", loader=cm.tenant_loader())
    assert tg.api_key == KEY and tg.model == "claude-sonnet-5-5"
    assert cm.resolve_target("t-2", loader=cm.tenant_loader()) is None


def test_runtime_failures_stamp_key_errors_only(store):
    t, _ = store
    mk.connect("t-1", api_key=KEY, model="claude-sonnet-5-5", actor="u", acknowledged=True, verify=_ok)
    tg = mk.load_target("t-1")
    cm.record_failure(tg, cm.UNAVAILABLE)
    cm.record_failure(tg, cm.REFUSED)
    assert t.rows["t-1"]["last_error"] == ""
    cm.record_failure(tg, cm.KEY_REJECTED)
    assert t.rows["t-1"]["last_error"] == cm.KEY_REJECTED


def test_verify_is_one_minimal_call():
    from types import SimpleNamespace
    seen = {}
    # The plain Messages call (no fallback beta): it exists on both the Anthropic and the Bedrock
    # Mantle clients.
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: seen.update(kw)))
    cm.verify(cm.Target(provider=cm.ANTHROPIC_KEY, model="claude-sonnet-5-5", api_key=KEY), client=client)
    assert seen["max_tokens"] == 16 and seen["model"] == "claude-sonnet-5-5"


# --- /api/me/act -------------------------------------------------------------------

def _me(intent, args=None, *, email="cfo@banco.com.br", groups=None, verified="true"):
    claims = {"sub": "u-1", "custom:tenant": "t-1", "email": email, "email_verified": verified}
    if groups:
        claims["cognito:groups"] = groups
    return {"body": json.dumps({"intent": intent, "args": args or {}}), "headers": {},
            "requestContext": {"http": {"method": "POST"}, "authorizer": {"jwt": {"claims": claims}}}}


@pytest.fixture
def act(store, monkeypatch):
    journal = []
    monkeypatch.setattr(er, "_log", lambda subject, kind, actor, detail: journal.append(detail))
    monkeypatch.setattr(act_api, "_get_act", lambda *a, **k: None)
    monkeypatch.setattr(act_api, "_put_act", lambda *a, **k: None)
    cfg = {"t-1": {"tier": "saas", "modules": ["banking"], "model_admins": ["cfo@banco.com.br"]}}
    monkeypatch.setattr(tenant_config, "get_tenant_config", lambda t, **k: cfg.get(t))
    monkeypatch.setattr(cm, "verify", lambda tg, **k: None)
    return journal


def _call(ev):
    r = act_api.lambda_handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


def test_a_model_admin_connects_and_the_key_never_reaches_the_journal(act, store):
    status, body = _call(_me("connect_model", {"api_key": KEY, "model": "claude-sonnet-5-5",
                                               "acknowledged": True}))
    assert status == 200 and body["outcome"] == "applied" and body["connection"]["last4"] == "wxyz"
    assert KEY not in json.dumps(body) and KEY not in json.dumps(act, default=str)
    assert act[-1]["args"]["api_key"] == "…wxyz"
    assert "_email" not in act[-1]["args"] and store[0].rows["t-1"]["connected_by"] == "u-1"


def test_a_member_who_is_not_a_model_admin_can_read_status_but_not_connect(act):
    status, body = _call(_me("connect_model", {"api_key": KEY, "acknowledged": True},
                             email="analista@banco.com.br"))
    assert status == 403 and body["reason"] == "not-model-admin"
    assert KEY not in json.dumps(act, default=str)
    status, body = _call(_me("model_status", email="analista@banco.com.br"))
    assert status == 200 and body["can_manage"] is False and body["connection"] is None
    assert body["models"][0]["model"] == "claude-sonnet-5-5" and "Anthropic" in body["acknowledgement"]


def test_a_client_cannot_name_another_tenant(act):
    status, body = _call(_me("disconnect_model", {"tenant_id": "t-2", "tenant": "t-2"}))
    assert status == 200 and body["outcome"] == "noop"         # acted on t-1 (its own), not t-2


def test_a_rejected_key_is_reported_with_its_code(act, monkeypatch):
    def reject(tg, **k):
        raise cm.ModelError(cm.NO_CREDIT)
    monkeypatch.setattr(cm, "verify", reject)
    status, body = _call(_me("connect_model", {"api_key": KEY, "acknowledged": True}))
    assert status == 400 and body["reason"] == cm.NO_CREDIT and body["error"] == cm.MESSAGES[cm.NO_CREDIT]


def test_test_and_disconnect(act, store, monkeypatch):
    _call(_me("connect_model", {"api_key": KEY, "acknowledged": True}))
    status, body = _call(_me("test_model"))
    assert status == 200 and body["ok"] is True

    def reject(tg, **k):
        raise cm.ModelError(cm.KEY_REJECTED)
    monkeypatch.setattr(cm, "verify", reject)
    status, body = _call(_me("test_model"))
    assert body["ok"] is False and body["connection"]["last_error"] == cm.KEY_REJECTED
    status, body = _call(_me("disconnect_model"))
    assert body["outcome"] == "applied" and store[0].rows == {}


def test_disabled_where_the_store_is_not_configured(act, monkeypatch):
    monkeypatch.delenv(mk.TABLE_ENV)
    status, body = _call(_me("model_status"))
    assert status == 503


def test_model_admins_are_set_by_an_operator_only(act, monkeypatch):
    status, _ = _call(_me("set_model_admins", {"tenant_id": "t-1", "emails": "x@y.com"}))
    assert status == 403
    saved = {}
    monkeypatch.setattr(tenant_config, "set_model_admins",
                        lambda t, emails, **k: saved.setdefault(t, emails))
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", "s")
    ev = {"body": json.dumps({"intent": "set_model_admins",
                              "args": {"tenant_id": "t-1", "emails": "CFO@banco.com.br"}}),
          "headers": {"x-onca-origin": "s"}, "requestContext": {"http": {"method": "POST"}}}
    status, _ = _call(ev)
    assert status == 200 and saved == {"t-1": ["CFO@banco.com.br"]}


def test_is_model_admin_is_case_insensitive_and_empty_by_default():
    assert tenant_config.is_model_admin({"model_admins": ["CFO@Banco.com.br"]}, "cfo@banco.com.br")
    assert not tenant_config.is_model_admin({}, "cfo@banco.com.br")
    assert not tenant_config.is_model_admin({"model_admins": ["a@b.c"]}, None)


def test_connect_model_is_never_an_mcp_tool():
    from src.dashboard import mcp_server
    names = {t["name"] for t in mcp_server.ops_tools(True)}
    assert "connect_model" not in names and "test_model" in names


def test_cdk_grants_are_purpose_conditioned():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "infra" / "app.py").read_text(encoding="utf-8")
    block = src[src.index('"OncaModelKeyKms"'):src.index("# Agentic API key admin (self-service)")]
    assert '"kms:EncryptionContext:onca-purpose": "model-key"' in block
    assert "(act_fn, [\"kms:Encrypt\", \"kms:Decrypt\"])" in block
    assert "(agent_fn, [\"kms:Decrypt\"]), (agent_api_fn, [\"kms:Decrypt\"])" in block
    assert "enable_key_rotation=True" in block and "deletion_protection=True" in block


def test_an_unverified_email_is_not_a_model_admin(act):
    status, body = _call(_me("connect_model", {"api_key": KEY, "acknowledged": True}, verified="false"))
    assert status == 403 and body["reason"] == "not-model-admin"
