"""#350 (BYOK story 5): the client's own Amazon Bedrock through a cross-account role + external id."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.dashboard import act_api, tenant_config
from src.synth import client_model as cm
from src.synth import entity_registry as er
from src.synth import model_keys as mk

BROKER = "arn:aws:iam::668449743071:role/onca-client-model-broker"
ROLE = "arn:aws:iam::123456789012:role/onca-bedrock-access"
XID = "onca-" + "a" * 32
BR = "anthropic.claude-sonnet-5-5"
T = cm.Target(provider=cm.BEDROCK_ROLE, model=BR, tenant="t-1", role_arn=ROLE, external_id=XID,
              region="sa-east-1")


class _ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Sts:
    def __init__(self, fail_hop=None, code="AccessDenied"):
        self.calls, self.fail_hop, self.code = [], fail_hop, code

    def factory(self, creds):
        hop = 1 if creds is None else 2
        sts = self

        class _C:
            def assume_role(self, **kw):
                sts.calls.append((hop, kw, creds))
                if sts.fail_hop == hop:
                    raise _ClientError(sts.code)
                exp = datetime.now(timezone.utc) + timedelta(minutes=15)
                return {"Credentials": {"AccessKeyId": f"AK{hop}", "SecretAccessKey": f"SK{hop}",
                                        "SessionToken": f"ST{hop}", "Expiration": exp}}
        return _C()


@pytest.fixture(autouse=True)
def _broker(monkeypatch):
    monkeypatch.setenv(cm.BROKER_ROLE_ENV, BROKER)
    cm._CRED_CACHE.clear()
    yield
    cm._CRED_CACHE.clear()


# --- the call path -------------------------------------------------------------------------

def test_the_allow_list_has_the_bedrock_model_under_its_own_provider():
    assert cm.MODELS[BR]["provider"] == cm.BEDROCK_ROLE
    assert cm.resolve_target("t-1", loader=lambda t: T) == T
    wrong = cm.Target(provider=cm.BEDROCK_ROLE, model="claude-sonnet-5-5", tenant="t-1",
                      role_arn=ROLE, external_id=XID, region="sa-east-1")
    with pytest.raises(cm.ModelError) as e:
        cm.resolve_target("t-1", loader=lambda t: wrong)
    assert e.value.code == cm.NO_ACCESS


def test_two_hops_broker_first_then_the_client_role_with_the_external_id():
    sts = _Sts()
    creds = cm._assumed_credentials(T, sts_factory=sts.factory)
    (h1, k1, c1), (h2, k2, c2) = sts.calls
    assert (h1, k1["RoleArn"], c1) == (1, BROKER, None) and "ExternalId" not in k1
    assert (h2, k2["RoleArn"], k2["ExternalId"]) == (2, ROLE, XID)
    assert c2["AccessKeyId"] == "AK1"                       # chained from the broker's session
    assert k1["RoleSessionName"] == k2["RoleSessionName"] == "onca-t-1"
    assert creds == {"AccessKeyId": "AK2", "SecretAccessKey": "SK2", "SessionToken": "ST2"}
    cm._assumed_credentials(T, sts_factory=sts.factory)
    assert len(sts.calls) == 2                              # reused while fresh


def test_the_cache_is_per_role_and_external_id():
    sts = _Sts()
    cm._assumed_credentials(T, sts_factory=sts.factory)
    other = cm.Target(provider=cm.BEDROCK_ROLE, model=BR, tenant="t-1", role_arn=ROLE,
                      external_id="onca-" + "b" * 32, region="sa-east-1")
    cm._assumed_credentials(other, sts_factory=sts.factory)
    assert len(sts.calls) == 4


def test_a_refused_client_role_is_role_denied_and_a_broker_failure_is_ours():
    with pytest.raises(cm.ModelError) as e:
        cm._assumed_credentials(T, sts_factory=_Sts(fail_hop=2).factory)
    assert e.value.code == cm.ROLE_DENIED and cm.ROLE_DENIED in cm.MESSAGES
    with pytest.raises(cm.ModelError) as e:
        cm._assumed_credentials(T, sts_factory=_Sts(fail_hop=1).factory)
    assert e.value.code == cm.UNAVAILABLE
    with pytest.raises(cm.ModelError) as e:
        cm._assumed_credentials(T, sts_factory=_Sts(fail_hop=2, code="RegionDisabledException").factory)
    assert e.value.code == cm.UNAVAILABLE


def test_no_broker_configured_means_unavailable_not_hosted(monkeypatch):
    monkeypatch.delenv(cm.BROKER_ROLE_ENV)
    with pytest.raises(cm.ModelError) as e:
        cm._assumed_credentials(T, sts_factory=_Sts().factory)
    assert e.value.code == cm.UNAVAILABLE


def test_the_client_is_the_sdk_bedrock_mantle_client_in_the_clients_region():
    c = cm._client_for(T, sts_factory=_Sts().factory)
    assert type(c).__name__ == "AnthropicBedrockMantle"
    assert "bedrock-mantle.sa-east-1.api.aws" in str(c.base_url)
    assert c.aws_session_token == "ST2" and c.max_retries == 0


@pytest.mark.parametrize("bad", [dict(role_arn="arn:aws:iam::1:role/x"), dict(external_id=""),
                                 dict(region="sa-east-9")])
def test_an_incomplete_connection_never_calls(bad):
    tg = cm.Target(**{**dict(provider=cm.BEDROCK_ROLE, model=BR, tenant="t-1", role_arn=ROLE,
                             external_id=XID, region="sa-east-1"), **bad})
    sts = _Sts()
    with pytest.raises(cm.ModelError) as e:
        cm._client_for(tg, sts_factory=sts.factory)
    assert e.value.code == cm.NO_ACCESS and sts.calls == []


def _resp(text="ok", stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, model=BR,
                           content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=10, output_tokens=3,
                                                 cache_read_input_tokens=0, cache_creation_input_tokens=0))


def test_bedrock_calls_the_plain_messages_api_without_the_fallback_beta():
    seen = {}
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: seen.update(kw) or _resp()))
    text, usage = cm.anthropic_converse(T, "q", system="s", client=client)
    assert text == "ok" and seen["model"] == BR and seen["output_config"] == {"effort": cm.EFFORT}
    assert "betas" not in seen and "fallbacks" not in seen and seen["system"] == "s"
    assert usage["input_tokens"] == 10


def test_the_anthropic_key_path_keeps_the_server_side_fallback():
    seen = {}
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: seen.update(kw) or _resp())))
    key = cm.Target(provider=cm.ANTHROPIC_KEY, model="claude-sonnet-5-5", tenant="t-1", api_key="k")
    cm.anthropic_converse(key, "q", client=client)
    assert seen["betas"] == [cm.FALLBACK_BETA] and seen["fallbacks"] == "default"


def test_a_bedrock_refusal_is_model_refused():
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: _resp(stop="refusal")))
    with pytest.raises(cm.ModelError) as e:
        cm.anthropic_converse(T, "q", client=client)
    assert e.value.code == cm.REFUSED


# --- the store -----------------------------------------------------------------------------

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


class _NoKms:
    def __getattr__(self, name):
        raise AssertionError("a role connection never touches KMS")


@pytest.fixture
def store(monkeypatch):
    t = _Table()
    monkeypatch.setenv(mk.TABLE_ENV, "keys")
    monkeypatch.setenv(mk.KMS_ENV, "arn:aws:kms:us-east-1:1:key/x")
    monkeypatch.setattr(mk, "_table", lambda table=None: t)
    monkeypatch.setattr(mk, "_kms", lambda kms=None: _NoKms())
    return t


def _connect(**kw):
    args = dict(role_arn=ROLE, region="sa-east-1", external_id=XID, model=BR, actor="u-1",
                acknowledged=True, verify=lambda tg: None)
    args.update(kw)
    return mk.connect_role("t-1", **args)


def test_connect_role_verifies_first_and_stores_no_secret(store):
    seen = []
    conn = _connect(verify=lambda tg: seen.append((tg.role_arn, tg.external_id, tg.region)))
    assert seen == [(ROLE, XID, "sa-east-1")]
    row = store.rows["t-1"]
    assert "sealed" not in row and row["external_id"] == XID and row["provider"] == cm.BEDROCK_ROLE
    pub = conn.public()
    assert pub["role_arn"] == ROLE and pub["region"] == "sa-east-1" and pub["last4"] == ""
    assert "external_id" not in pub
    assert mk.load_target("t-1") == T                        # no KMS on the way back either


def test_a_refused_role_stores_nothing(store):
    def refuse(tg):
        raise cm.ModelError(cm.ROLE_DENIED)
    with pytest.raises(cm.ModelError):
        _connect(verify=refuse)
    assert store.rows == {}


@pytest.mark.parametrize("kw", [dict(role_arn="arn:aws:iam::123:role/x"),
                                dict(role_arn="arn:aws:iam::668449743071:role/anything"),
                                dict(region="br-south-1"), dict(external_id=""),
                                dict(acknowledged=False), dict(model="claude-sonnet-5-5")])
def test_connect_role_rejects_bad_input_before_any_call(store, kw):
    called = []
    with pytest.raises(ValueError):
        _connect(verify=lambda tg: called.append(1), **kw)
    assert called == [] and store.rows == {}


# --- the external id -----------------------------------------------------------------------

class _CfgTable:
    def __init__(self, rows):
        self.rows = rows

    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeValues,
                    ReturnValues):
        row = self.rows.get(Key["tenant_id"])
        if row is None:
            raise RuntimeError("ConditionalCheckFailedException")
        assert "if_not_exists(model_external_id" in UpdateExpression
        row.setdefault("model_external_id", ExpressionAttributeValues[":x"])
        return {"Attributes": {"model_external_id": row["model_external_id"]}}


def test_the_external_id_is_created_once_and_never_changes():
    t = _CfgTable({"t-1": {}})
    first = tenant_config.ensure_model_external_id("t-1", table=t)
    assert first.startswith("onca-") and len(first) == 37
    assert tenant_config.ensure_model_external_id("t-1", table=t) == first
    assert tenant_config.ensure_model_external_id("t-2", table=_CfgTable({"t-2": {}})) != first
    with pytest.raises(KeyError):
        tenant_config.ensure_model_external_id("t-9", table=t)


def test_an_operator_re_put_keeps_the_external_id():
    assert "model_external_id" in tenant_config._PREF_FIELDS


def test_the_published_template_matches_the_code():
    from pathlib import Path
    tpl = (Path(__file__).resolve().parents[1] / "src/dashboard/site/docs/onca-bedrock-role.yaml").read_text()
    assert BROKER in tpl and "sts:ExternalId" in tpl and "bedrock-mantle:CreateInference" in tpl
    assert f"- {BR}" in tpl and 'AllowedPattern: "onca-[0-9a-f]{32}"' in tpl
    assert "CallWithBearerToken" not in tpl


# --- /api/me/act ---------------------------------------------------------------------------

def _me(intent, args=None, *, email="cfo@banco.com.br"):
    claims = {"sub": "u-1", "custom:tenant": "t-1", "email": email, "email_verified": "true"}
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
    monkeypatch.setattr(tenant_config, "ensure_model_external_id", lambda t, **k: XID if t == "t-1" else "")
    monkeypatch.setattr(cm, "verify", lambda tg, **k: None)
    return journal


def _call(ev):
    r = act_api.lambda_handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


def test_model_admins_see_what_their_role_must_trust_members_do_not(act):
    status, body = _call(_me("model_status"))
    assert status == 200 and body["bedrock"]["principal"] == BROKER
    assert body["bedrock"]["external_id"] == XID and "sa-east-1" in body["bedrock"]["regions"]
    assert body["acknowledgements"][cm.BEDROCK_ROLE] == mk.ACKNOWLEDGEMENT_BEDROCK
    assert {m["provider"] for m in body["models"]} == {cm.ANTHROPIC_KEY, cm.BEDROCK_ROLE}
    status, body = _call(_me("model_status", email="analista@banco.com.br"))
    assert status == 200 and "bedrock" not in body


def test_connect_uses_the_tenants_own_external_id_never_the_requests(act, store):
    status, body = _call(_me("connect_model", {"model": BR, "role_arn": ROLE, "region": "sa-east-1",
                                               "external_id": "onca-" + "f" * 32,
                                               "acknowledged": True}))
    assert status == 200 and body["connection"]["role_arn"] == ROLE
    assert store.rows["t-1"]["external_id"] == XID


def test_a_denied_role_is_reported_with_its_code(act, monkeypatch):
    def refuse(tg, **k):
        raise cm.ModelError(cm.ROLE_DENIED)
    monkeypatch.setattr(cm, "verify", refuse)
    status, body = _call(_me("connect_model", {"model": BR, "role_arn": ROLE, "region": "sa-east-1",
                                               "acknowledged": True}))
    assert status == 400 and body["reason"] == cm.ROLE_DENIED
