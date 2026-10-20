"""#251 — self-service account deletion: only the caller, every per-user store, idempotent, the
organization survives, a sole self-registered tenant closes, and the next sign-in starts fresh.
Fixture identities only."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import account, act_api, lambda_pretoken, tenant_config  # noqa: E402
from src.dashboard.oauth.store import MemoryStore  # noqa: E402
from src.synth import entity_registry as er  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "src/dashboard/site"
NOW = dt.datetime(2026, 10, 18, 12, 0, tzinfo=dt.timezone.utc)
ME, OTHER = "11111111-aaaa-4aaa-8aaa-000000000001", "22222222-bbbb-4bbb-8bbb-000000000002"


# ---- fakes: a DynamoDB table that evaluates the boto3 condition objects the code really sends ---
def _ev(cond, item):
    if cond is None:
        return True
    e = cond.get_expression()
    op, v = e["operator"], e["values"]
    if op == "AND":
        return _ev(v[0], item) and _ev(v[1], item)
    if op == "OR":
        return _ev(v[0], item) or _ev(v[1], item)
    if op == "=":
        return item.get(v[0].name) == v[1]
    if op == "begins_with":
        return isinstance(item.get(v[0].name), str) and item[v[0].name].startswith(v[1])
    raise AssertionError(f"unsupported condition {op}")


class Table:
    def __init__(self, *keys, items=()):
        self.keys, self.items = keys, {}
        for it in items:
            self.items[tuple(it[k] for k in keys)] = dict(it)

    def _k(self, key):
        return tuple(key[k] for k in self.keys)

    def scan(self, FilterExpression=None, **kw):
        return {"Items": [dict(i) for i in self.items.values() if _ev(FilterExpression, i)]}

    def query(self, KeyConditionExpression=None, FilterExpression=None, **kw):
        return {"Items": [dict(i) for i in self.items.values()
                          if _ev(KeyConditionExpression, i) and _ev(FilterExpression, i)]}

    def get_item(self, Key, **kw):
        it = self.items.get(self._k(Key))
        return {"Item": dict(it)} if it else {}

    def delete_item(self, Key, ReturnValues=None, **kw):
        old = self.items.pop(self._k(Key), None)
        return {"Attributes": old} if (old and ReturnValues) else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ConditionExpression="", **kw):
        it = self.items.get(self._k(Key))
        if it is None or ("attribute_not_exists(closed_at)" in ConditionExpression and "closed_at" in it):
            raise type("ConditionalCheckFailedException", (Exception,), {})()
        for part in UpdateExpression.removeprefix("SET ").split(","):
            name, val = (p.strip() for p in part.split("="))
            it[name] = ExpressionAttributeValues[val]


class UserNotFoundException(Exception):
    pass


class Cognito:
    def __init__(self, users):
        self.users = dict(users)          # username -> {"sub":…, "custom:tenant":…}
        self.calls = []

    def _need(self, name):
        if name not in self.users:
            raise UserNotFoundException("User does not exist.")

    def admin_user_global_sign_out(self, UserPoolId, Username):
        self.calls.append(("signout", Username)); self._need(Username)

    def admin_delete_user(self, UserPoolId, Username):
        self.calls.append(("delete", Username)); self._need(Username); self.users.pop(Username)

    def list_users(self, **kw):
        return {"Users": [{"Username": u, "Attributes": [{"Name": k, "Value": v} for k, v in a.items()]}
                          for u, a in self.users.items()]}


def _world(tenant_item=None, other_in_tenant=False):
    tenant_item = tenant_item or {"tenant_id": "entry-abc123", "tier": "entry", "billing_managed": True,
                                  "trial_until": (NOW + dt.timedelta(days=9)).isoformat(),
                                  "trial_modules": ["betting"], "modules": []}
    tid = tenant_item["tenant_id"]
    entities = Table("pk", items=[
        {"pk": "DECISION#d1", "actor": ME, "rationale": "minha nota"},
        {"pk": "DECISION#d2", "actor": OTHER},
        {"pk": "ENGAGEMENT#e1", "actor": ME}, {"pk": "ENGAGEMENT#e2", "actor": ME},
        {"pk": "ENGAGEMENT#e3", "actor": OTHER},
        {"pk": f"ACT#{ME}:k1", "type": "act"}, {"pk": f"ACT#{OTHER}:k1", "type": "act"},
        {"pk": "bancoabc", "actor": ME, "type": "entity"},          # a registry row a user curated: stays
    ])
    journal = Table("entity_id", "ts", items=[
        {"entity_id": "DECISION#d1", "ts": "1", "source": "curated"},
        {"entity_id": "DECISION#d2", "ts": "1", "source": "curated"},
        {"entity_id": "__act__", "ts": "1", "source": ME, "detail": {"args": {"rationale": "x"}}},
        {"entity_id": "__act__", "ts": "2", "source": OTHER},
        {"entity_id": "bancoabc", "ts": "1", "source": ME},
    ])
    push = Table("pk", items=[
        {"pk": "SUB#web1", "user": ME}, {"pk": "SUB#expo1", "user": ME, "endpoint": "expo:ExponentPushToken[x]"},
        {"pk": "SUB#web2", "user": OTHER}, {"pk": f"DIGEST#{ME}", "email": "me@example.com"},
        {"pk": f"DIGEST#{OTHER}"}, {"pk": "DIGESTTOK#h1", "sub": ME}, {"pk": "DIGESTTOK#h2", "sub": OTHER},
        {"pk": "STAT#2026-10-18", "sent": 3},
    ])
    feedback = Table("pk", items=[
        {"pk": "FB#r1", "type": "report", "sub": ME, "text": "t"},
        {"pk": "FB#r2", "type": "report", "sub": OTHER, "text": "t"},
        {"pk": f"FBQ#{ME}#2026-10-18", "n": 1}, {"pk": f"FBQ#{OTHER}#2026-10-18", "n": 1},
    ])
    quota = Table("pk", items=[{"pk": f"{ME}#2026-10-18", "n": 4}, {"pk": f"{OTHER}#2026-10-18", "n": 2}])
    tenants = Table("tenant_id", items=[tenant_item])
    fed = Table("email", items=[{"email": "me@example.com", "tenant_id": tid, "tier": "entry"}]
                + ([{"email": "colega@example.com", "tenant_id": tid, "tier": "entry"}] if other_in_tenant else []))
    oauth = MemoryStore()
    oauth.put("usr#" + ME, "fam#f1", {"client_id": "claude", "created": 1})
    oauth.put("usr#" + ME, "consent#claude", {"client_id": "claude"})
    oauth.put("usr#" + OTHER, "fam#f2", {"client_id": "claude", "created": 1})
    cognito = Cognito({"google_123": {"sub": ME}, "colega": {"sub": OTHER, "custom:tenant": "org-1"}})
    keys = Table("key_hash", items=[{"key_hash": "h1", "tenant_id": tid, "created_by": "me@example.com"},
                                    {"key_hash": "h2", "tenant_id": tid, "created_by": ME},
                                    {"key_hash": "h3", "tenant_id": tid, "created_by": "colega@example.com"}])
    tables = {"entities": entities, "journal": journal, "push": push, "feedback": feedback,
              "ask_quota": quota, "tenants": tenants, "federated": fed, "api_keys": keys}
    return tables, oauth, cognito


def _delete(tables, oauth, cognito, tenant="entry-abc123", email="Me@Example.com"):
    return account.delete_account(sub=ME, username="google_123", email=email, tenant=tenant,
                                  cognito=cognito, pool="us-east-1_x", tables=tables,
                                  oauth_store=oauth, now=NOW)


# ---- the store sweep ----------------------------------------------------------------------------
def test_deletes_every_row_of_the_caller_and_nothing_of_anyone_else():
    tables, oauth, cognito = _world()
    counts = _delete(tables, oauth, cognito)
    assert counts == {"decisions": 1, "engagement": 2, "act_replays": 1, "journal": 2, "push": 4,
                      "reports": 1, "ask_quota": 1, "apps_revoked": 1, "api_keys_anonymized": 2, "federated_mapping": 1,
                      "tenant_closed": True, "tenant": "sole-user", "user_deleted": True}
    assert sorted(k[0] for k in tables["entities"].items) == [
        f"ACT#{OTHER}:k1", "DECISION#d2", "ENGAGEMENT#e3", "bancoabc"]
    assert sorted(tables["journal"].items) == [("DECISION#d2", "1"), ("__act__", "2"), ("bancoabc", "1")]
    assert sorted(k[0] for k in tables["push"].items) == [
        f"DIGEST#{OTHER}", "DIGESTTOK#h2", "STAT#2026-10-18", "SUB#web2"]
    assert sorted(k[0] for k in tables["feedback"].items) == ["FB#r2", f"FBQ#{OTHER}#2026-10-18"]
    keys = {k[0]: v["created_by"] for k, v in tables["api_keys"].items.items()}  # #252: keys stay, creator forgotten
    assert keys == {"h1": "deleted-user:" + account.user_hash(ME), "h2": "deleted-user:" + account.user_hash(ME),
                    "h3": "colega@example.com"}
    assert list(tables["ask_quota"].items) == [(f"{OTHER}#2026-10-18",)]
    assert oauth.get("usr#" + ME, "fam#f1")["revoked"] and not oauth.get("usr#" + ME, "consent#claude")
    assert not oauth.get("usr#" + OTHER, "fam#f2").get("revoked")
    # sessions are revoked BEFORE any data goes, and the Cognito user goes LAST
    assert cognito.calls == [("signout", "google_123"), ("delete", "google_123")]
    assert "google_123" not in cognito.users and "colega" in cognito.users


def test_deletion_is_idempotent():
    tables, oauth, cognito = _world()
    _delete(tables, oauth, cognito)
    again = _delete(tables, oauth, cognito)
    assert again["user_deleted"] is False and again["tenant"] == "already-closed"
    assert all(again[k] == 0 for k in ("decisions", "engagement", "journal", "push", "reports",
                                       "ask_quota", "apps_revoked", "federated_mapping"))
    assert cognito.calls[-1] == ("signout", "google_123")       # gone already: no second delete


def test_an_organization_tenant_survives():
    org = {"tenant_id": "org-1", "tier": "saas", "modules": ["banking"]}   # operator-provisioned
    tables, oauth, cognito = _world(tenant_item=org)
    counts = _delete(tables, oauth, cognito, tenant="org-1")
    assert counts["tenant"] == "organization" and counts["tenant_closed"] is False
    assert tables["tenants"].items[("org-1",)] == org
    assert "colega" in cognito.users                            # the other user is untouched


def test_a_shared_self_registered_tenant_is_not_closed():
    tables, oauth, cognito = _world(other_in_tenant=True)
    counts = _delete(tables, oauth, cognito)
    assert counts["tenant"] == "shared" and not counts["tenant_closed"]
    assert "closed_at" not in tables["tenants"].items[("entry-abc123",)]
    assert ("colega@example.com",) in tables["federated"].items   # only MY mapping went


def test_the_sole_users_trial_tenant_is_closed():
    tables, oauth, cognito = _world()
    _delete(tables, oauth, cognito)
    item = tables["tenants"].items[("entry-abc123",)]
    assert item["closed_reason"] == "account_deleted" and item["trial_modules"] == []
    eff = tenant_config.effective_entitlement(item, NOW + dt.timedelta(seconds=1))
    assert eff["modules"] == [] and eff["billing"]["state"] == "trial_expired"


def test_a_live_paid_subscription_blocks_and_nothing_is_touched():
    paid = {"tenant_id": "entry-abc123", "tier": "entry", "billing_managed": True, "modules": [],
            "subs": {"betting": {"status": "active", "tier": "entry"}}}
    tables, oauth, cognito = _world(tenant_item=paid)
    before = {k: dict(t.items) for k, t in tables.items()}
    with pytest.raises(account.Blocked) as exc:
        _delete(tables, oauth, cognito)
    assert exc.value.status == 409 and exc.value.reason == "active-subscription"
    assert "contato@onssa.org" in exc.value.message
    assert cognito.calls == [] and {k: dict(t.items) for k, t in tables.items()} == before


def test_a_failed_sign_out_aborts_before_any_data_goes():
    tables, oauth, cognito = _world()

    def boom(**kw):
        raise RuntimeError("throttled")
    cognito.admin_user_global_sign_out = boom
    with pytest.raises(account.Blocked) as exc:
        _delete(tables, oauth, cognito)
    assert exc.value.status == 502 and ("DECISION#d1",) in tables["entities"].items


def test_the_next_federated_sign_in_starts_fresh(monkeypatch):
    tables, oauth, cognito = _world()
    _delete(tables, oauth, cognito)
    fed = tables["federated"]

    class DDB:
        def get_item(self, TableName, Key):
            it = fed.items.get((Key["email"]["S"],))
            return {"Item": {k: {"S": v} for k, v in it.items()}} if it else {}
    monkeypatch.setattr(lambda_pretoken, "FEDERATED_TABLE", "fed")
    monkeypatch.setattr("boto3.client", lambda *a, **k: DDB())
    ev = {"request": {"userAttributes": {"email": "me@example.com", "identities": "[{}]",
                                         "cognito:user_status": "EXTERNAL_PROVIDER"}}}
    out = lambda_pretoken.lambda_handler(ev, None)
    assert "claimsOverrideDetails" not in (out.get("response") or {})   # no tenant: the fresh-start gate


# ---- the act API contract ----------------------------------------------------------------------
def _ev_jwt(args, sub=ME, **extra):
    claims = {"sub": sub, "email": "me@example.com", "custom:tenant": "entry-abc123",
              "cognito:username": "google_123", **extra}
    return {"body": json.dumps({"intent": "delete_account", "args": args, "idempotency_key": "k9"}),
            "headers": {}, "requestContext": {"http": {"method": "POST"},
                                              "authorizer": {"jwt": {"claims": claims}}}}


@pytest.fixture
def act(monkeypatch):
    seen = {"calls": [], "journal": [], "put_act": []}
    monkeypatch.setattr(er, "_log", lambda *a, **k: seen["journal"].append(a))
    monkeypatch.setattr(act_api, "_get_act", lambda *a, **k: None)
    monkeypatch.setattr(act_api, "_put_act", lambda *a, **k: seen["put_act"].append(a))

    def fake(**kw):
        seen["calls"].append(kw)
        return {"user_deleted": True, "decisions": 0}
    monkeypatch.setattr(account, "delete_account", fake)
    return seen


def test_someone_elses_account_can_never_be_targeted(act):
    r = act_api.lambda_handler(_ev_jwt({"confirm": "EXCLUIR", "sub": OTHER, "_sub": OTHER,
                                        "username": "colega", "_username": "colega",
                                        "email": "colega@example.com", "tenant": "org-1"}), None)
    assert r["statusCode"] == 200
    (kw,) = act["calls"]
    assert kw == {"sub": ME, "username": "google_123", "email": "me@example.com", "tenant": "entry-abc123"}


def test_the_operator_path_cannot_delete_accounts(act, monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", "s")
    ev = {"body": json.dumps({"intent": "delete_account", "args": {"confirm": "EXCLUIR"}}),
          "headers": {"x-onca-origin": "s"}, "requestContext": {"http": {"method": "POST"}}}
    assert act_api.lambda_handler(ev, None)["statusCode"] == 403 and not act["calls"]


def test_the_typed_confirmation_is_required(act):
    r = act_api.lambda_handler(_ev_jwt({"confirm": "sim"}), None)
    assert r["statusCode"] == 400 and json.loads(r["body"])["reason"] == "confirm" and not act["calls"]


def test_nothing_is_stored_under_the_raw_sub(act):
    r = act_api.lambda_handler(_ev_jwt({"confirm": "excluir"}), None)
    body = json.loads(r["body"])
    assert body["outcome"] == "applied" and ME not in r["body"]
    assert act["put_act"] == []                                 # no ACT#<sub>: replay row re-created
    (row,) = act["journal"]
    assert ME not in json.dumps(row, default=str) and row[2].startswith("deleted-user:")


def test_a_replay_after_deletion_is_a_noop(act, monkeypatch):
    monkeypatch.setattr(account, "delete_account", lambda **kw: {"user_deleted": False})
    r = act_api.lambda_handler(_ev_jwt({"confirm": "EXCLUIR"}), None)
    assert r["statusCode"] == 200 and json.loads(r["body"])["outcome"] == "noop"


def test_an_unexpected_failure_is_a_clean_retryable_502(act, monkeypatch):
    def boom(**kw):
        raise RuntimeError("InvalidParameterException")
    monkeypatch.setattr(account, "delete_account", boom)
    r = act_api.lambda_handler(_ev_jwt({"confirm": "EXCLUIR"}), None)
    assert r["statusCode"] == 502 and "tente de novo" in json.loads(r["body"])["error"]


def test_the_pool_listing_takes_no_attribute_projection():
    _, _, cognito = _world()
    seen = []
    orig = cognito.list_users
    cognito.list_users = lambda **kw: (seen.append(kw), orig(**kw))[1]
    account._other_users("t", ME, "me@example.com", cognito=cognito, pool="p", federated=None)
    assert seen and "AttributesToGet" not in seen[0]


def test_a_block_is_reported_to_the_user(act, monkeypatch):
    def blocked(**kw):
        raise account.Blocked(409, "active-subscription", "Há uma assinatura ativa")
    monkeypatch.setattr(account, "delete_account", blocked)
    r = act_api.lambda_handler(_ev_jwt({"confirm": "EXCLUIR"}), None)
    assert r["statusCode"] == 409 and json.loads(r["body"])["error"].startswith("Há uma assinatura")


# ---- the UI, the public page, the policy ---------------------------------------------------------
EXEC = (SITE / "v3/index.html").read_text(encoding="utf-8")


def test_account_sheet_offers_deletion_with_a_confirmation_step():
    sheet = EXEC.split("function openAccount() {", 1)[1].split("\n}\n", 1)[0]
    assert 'id="acctDel"' in sheet and "openDeleteAccount" in sheet
    step = EXEC.split("function openDeleteAccount() {", 1)[1].split("\n}\n", 1)[0]
    assert 'intent: "delete_account"' in step and "Fica com a sua organização" in step
    assert "não pode ser desfeita" in step and "Ctx.logout()" in step and "/docs/excluir-conta.html" in step
    assert 'go.disabled = word.value.trim().toUpperCase() !== DELETE_WORD' in step
    assert "args: { confirm:" in step and "sub" not in step.split("args: {", 1)[1].split("}", 1)[0]
    desk = EXEC.split("function renderAuthUI() {", 1)[1].split("\n}\n", 1)[0]
    assert "openAccount" in desk                                   # reachable on desktop too
    assert ".del input {" in EXEC and "min-height:46px" in EXEC.split(".del input {", 1)[1][:200]


def test_public_deletion_page_pt_and_en():
    html = (SITE / "docs/excluir-conta.html").read_text(encoding="utf-8")
    assert "Excluir a sua conta" in html and "Delete your account" in html
    assert "contato@onssa.org" in html and "EXCLUIR" in html and "O que fica" in html and "What stays" in html
    assert "a:where(:not(.btn):not(.pbtn):not(.mlink))::after" in html     # #281 touch targets
    from infra import edge_policy
    assert edge_policy.PUBLIC_ALIASES["/excluir-conta"] == "/docs/excluir-conta.html"
    assert "/docs/excluir-conta.html" in edge_policy.public_paths_js(SITE)


def test_policy_and_terms_point_to_self_service_deletion():
    for rel in ("docs/privacy.html", "docs/terms.html"):
        html = (SITE / rel).read_text(encoding="utf-8")
        assert "/docs/excluir-conta.html" in html and "Excluir minha conta" in html, rel
