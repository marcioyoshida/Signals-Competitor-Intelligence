"""#249 — invite codes link a federated login (Google / Apple, incl. a Hide-My-Email relay address)
to its organization's tenant; self-registration is never chosen for the user. Fixture data only."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import account, act_api, invites, lambda_pretoken, self_register, tenant_config  # noqa: E402
from src.synth import entity_registry as er  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 10, 19, 12, 0, tzinfo=dt.timezone.utc)
RELAY = "x7k2p9@privaterelay.appleid.com"
APPLE_SUB = "aaaa1111-2222-4333-8444-555566667777"


class CCF(Exception):
    pass


CCF.__name__ = "ConditionalCheckFailedException"


def _ev(cond, item):
    if cond is None:
        return True
    e = cond.get_expression()
    op, v = e["operator"], e["values"]
    if op == "AND":
        return _ev(v[0], item) and _ev(v[1], item)
    if op == "OR":
        return _ev(v[0], item) or _ev(v[1], item)
    if op == "attribute_exists":
        return v[0].name in item
    if op == "attribute_not_exists":
        return v[0].name not in item
    a = item.get(v[0].name)
    if op == "=":
        return a == v[1]
    if op == "<":
        return a is not None and a < v[1]
    if op == ">":
        return a is not None and a > v[1]
    if op == "begins_with":
        return isinstance(a, str) and a.startswith(v[1])
    raise AssertionError(op)


class Fed:
    """The federated-map table (pk ``email``) with the update shapes invites.py sends."""

    def __init__(self, items=()):
        self.items = {i["email"]: dict(i) for i in items}

    def get_item(self, Key, **kw):
        it = self.items.get(Key["email"])
        return {"Item": dict(it)} if it else {}

    def put_item(self, Item, **kw):
        self.items[Item["email"]] = dict(Item)

    def delete_item(self, Key, ReturnValues=None, **kw):
        old = self.items.pop(Key["email"], None)
        return {"Attributes": old} if old and ReturnValues else {}

    def scan(self, **kw):
        return {"Items": [dict(i) for i in self.items.values()]}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ConditionExpression=None,
                    ExpressionAttributeNames=None, ReturnValues=None, **kw):
        cur = self.items.get(Key["email"])
        if not _ev(ConditionExpression, cur or {}):
            raise CCF()
        it = cur if cur is not None else {"email": Key["email"]}
        names = ExpressionAttributeNames or {}
        expr = UpdateExpression
        if expr.startswith("ADD "):
            add, _, expr = expr[4:].partition(" SET ")
            name, val = add.split()
            it[name] = it.get(name, 0) + ExpressionAttributeValues[val]
            expr = "SET " + expr if expr else ""
        for part in expr.removeprefix("SET ").split(","):
            if part.strip():
                name, val = (p.strip() for p in part.split("="))
                it[names.get(name, name)] = ExpressionAttributeValues[val]
        self.items[Key["email"]] = it
        return {"Attributes": dict(it)} if ReturnValues else {}


class Tenants:
    def __init__(self, items):
        self.items = {i["tenant_id"]: i for i in items}

    def get_item(self, Key, **kw):
        it = self.items.get(Key["tenant_id"])
        return {"Item": it} if it else {}


TENANTS = Tenants([{"tenant_id": "org-bancox", "tier": "saas", "modules": ["banking"]}])


def _issue(fed, **kw):
    return invites.issue("org-bancox", issued_by="operator", table=fed, tenants=TENANTS, now=NOW, **kw)


def test_issue_stores_only_the_hash_and_returns_a_long_code():
    fed = Fed()
    inv = _issue(fed)
    raw = inv["code"].replace("-", "")
    assert len(raw) == 20 and set(raw) <= set(invites.ALPHABET)          # 100 bits
    (row,) = fed.items.values()
    assert row["email"] == "invite#" + invites.code_hash(inv["code"]) and raw not in json.dumps(row)
    assert row["tier"] == "saas" and row["expires_at"] == "2026-10-26T12:00:00+00:00"
    assert row["ttl"] > int((NOW + dt.timedelta(days=7)).timestamp())


@pytest.mark.parametrize("kw,err", [({"days": 0}, "days"), ({"days": 31}, "days"), ({"tier": "god"}, "tier")])
def test_issue_validates(kw, err):
    with pytest.raises(ValueError, match=err):
        _issue(Fed(), **kw)
    with pytest.raises(ValueError, match="unknown tenant"):
        invites.issue("nope", issued_by="op", table=Fed(), tenants=TENANTS, now=NOW)


def test_a_relay_email_user_joins_the_right_tenant_by_sub():
    fed = Fed()
    code = _issue(fed)["code"]
    typed = code.lower().replace("-", " ")                               # forgiving input
    r = invites.redeem(typed, sub=APPLE_SUB, email=RELAY, ip="203.0.113.9", table=fed, now=NOW)
    assert r == {"ok": True, "tenant_id": "org-bancox", "tier": "saas"}
    link = fed.items["sub#" + APPLE_SUB]
    assert link["tenant_id"] == "org-bancox" and link["contact_email"] == RELAY
    assert RELAY not in fed.items                                        # the relay is never a key
    assert invites.linked_tenant(APPLE_SUB, table=fed) == "org-bancox"


def test_used_expired_and_unknown_codes_all_read_invalid():
    fed = Fed()
    code = _issue(fed)["code"]
    assert invites.redeem(code, sub="s1", email=None, ip=None, table=fed, now=NOW)["ok"]
    assert invites.redeem(code, sub="s2", email=None, ip=None, table=fed, now=NOW) == {"ok": False, "reason": "invalid"}
    late = _issue(fed, days=1)["code"]
    assert invites.redeem(late, sub="s3", email=None, ip=None, table=fed,
                          now=NOW + dt.timedelta(days=2))["reason"] == "invalid"
    assert invites.redeem("ABCDE-FGHJK-MNPQR-STVWX", sub="s4", email=None, ip=None, table=fed, now=NOW)["reason"] == "invalid"
    assert invites.redeem("short", sub="s5", email=None, ip=None, table=fed, now=NOW)["reason"] == "invalid"
    assert "sub#s2" not in fed.items and "sub#s3" not in fed.items


def test_brute_force_is_rate_limited_per_sub_and_per_ip():
    fed = Fed()
    for _ in range(invites.LIMIT_PER_SUB):
        assert invites.redeem("ZZZZZ-ZZZZZ-ZZZZZ-ZZZZZ", sub="s1", email=None, ip="1.1.1.1", table=fed, now=NOW)["reason"] == "invalid"
    assert invites.redeem("ZZZZZ-ZZZZZ-ZZZZZ-ZZZZZ", sub="s1", email=None, ip="1.1.1.1", table=fed, now=NOW)["reason"] == "rate-limited"
    # the per-IP cap catches many fresh identities from one place
    n = 0
    while invites.redeem("ZZZZZ-ZZZZZ-ZZZZZ-ZZZZZ", sub=f"t{n}", email=None, ip="2.2.2.2", table=fed, now=NOW)["reason"] == "invalid":
        n += 1
    assert n == invites.LIMIT_PER_IP
    assert all(r.get("ttl") for k, r in fed.items.items() if k.startswith("rl#"))


def test_an_already_linked_sub_cannot_burn_a_second_code():
    fed = Fed()
    invites.redeem(_issue(fed)["code"], sub="s1", email=None, ip=None, table=fed, now=NOW)
    second = _issue(fed)["code"]
    assert invites.redeem(second, sub="s1", email=None, ip=None, table=fed, now=NOW)["reason"] == "already-linked"
    assert "used_at" not in fed.items["invite#" + invites.code_hash(second)]


def test_client_ip_ignores_spoofed_forwarded_entries():
    ev = {"headers": {"X-Forwarded-For": "6.6.6.6, 198.51.100.7, 130.176.1.1"},
          "requestContext": {"http": {"sourceIp": "130.176.1.1"}}}
    assert invites.client_ip(ev) == "198.51.100.7"
    assert invites.client_ip({"requestContext": {"http": {"sourceIp": "9.9.9.9"}}}) == "9.9.9.9"


# ---- the pre-token trigger: the sub link wins, for Google and Apple alike --------------------------
@pytest.mark.parametrize("provider", ["SignInWithApple", "Google"])
def test_pretoken_reads_the_sub_link_first(monkeypatch, provider):
    fed = Fed([{"email": "sub#" + APPLE_SUB, "tenant_id": "org-bancox", "tier": "saas"}])

    class DDB:
        def get_item(self, TableName, Key):
            it = fed.items.get(Key["email"]["S"])
            return {"Item": {k: {"S": str(v)} for k, v in it.items()}} if it else {}
    monkeypatch.setattr(lambda_pretoken, "FEDERATED_TABLE", "fed")
    monkeypatch.setattr("boto3.client", lambda *a, **k: DDB())
    ev = {"request": {"userAttributes": {"sub": APPLE_SUB, "email": RELAY, "cognito:user_status": "EXTERNAL_PROVIDER",
                                         "identities": json.dumps([{"providerName": provider}])}}}
    claims = lambda_pretoken.lambda_handler(ev, None)["response"]["claimsOverrideDetails"]["claimsToAddOrOverride"]
    assert claims == {"custom:tenant": "org-bancox", "custom:tier": "saas"}


# ---- the register endpoint ---------------------------------------------------------------------
def _reg_event(body, *, federated=True, tenant=None):
    claims = {"sub": APPLE_SUB, "email": RELAY}
    if federated:
        claims["identities"] = json.dumps([{"providerName": "SignInWithApple"}])
    if tenant:
        claims["custom:tenant"] = tenant
    return {"body": json.dumps(body), "headers": {"x-forwarded-for": "198.51.100.7, 130.176.1.1"},
            "requestContext": {"http": {"method": "POST", "sourceIp": "130.176.1.1"},
                               "authorizer": {"jwt": {"claims": claims}}}}


def test_register_redeems_an_invite(monkeypatch):
    fed = Fed()
    code = _issue(fed)["code"]
    monkeypatch.setattr(tenant_config, "_federated_table", lambda table=None: fed)
    r = self_register.lambda_handler(_reg_event({"invite": code}), None)
    assert r["statusCode"] == 200 and json.loads(r["body"]) == {"linked": True, "tier": "saas"}
    assert any(k.startswith("rl#ip#198.51.100.7#") for k in fed.items)
    bad = self_register.lambda_handler(_reg_event({"invite": code}), None)
    assert bad["statusCode"] == 409                                   # now linked: never a second link
    assert self_register.lambda_handler(_reg_event({"invite": code}, federated=False), None)["statusCode"] == 403


def test_register_errors_never_say_whether_a_code_exists(monkeypatch):
    fed = Fed()
    monkeypatch.setattr(tenant_config, "_federated_table", lambda table=None: fed)
    r = self_register.lambda_handler(_reg_event({"invite": "ZZZZZ-ZZZZZ-ZZZZZ-ZZZZZ"}), None)
    used = _issue(fed)["code"]
    invites.redeem(used, sub="someone", email=None, ip=None, table=fed)
    r2 = self_register.lambda_handler(_reg_event({"invite": used}), None)
    assert r["statusCode"] == r2["statusCode"] == 400 and r["body"] == r2["body"]


def test_self_registration_refuses_an_invite_linked_identity(monkeypatch):
    fed = Fed([{"email": "sub#" + APPLE_SUB, "tenant_id": "org-bancox", "tier": "saas"}])
    monkeypatch.setattr(tenant_config, "_federated_table", lambda table=None: fed)
    called = []
    monkeypatch.setattr(tenant_config, "self_register_entry_tenant", lambda *a, **k: called.append(1))
    r = self_register.lambda_handler(_reg_event({"industries": ["betting"]}), None)
    assert r["statusCode"] == 409 and not called


# ---- the operator act ---------------------------------------------------------------------------
def test_issue_invite_is_operator_only_and_never_replayed_or_journaled(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", "s")
    journal, puts = [], []
    monkeypatch.setattr(er, "_log", lambda *a, **k: journal.append(a))
    monkeypatch.setattr(act_api, "_get_act", lambda *a, **k: None)
    monkeypatch.setattr(act_api, "_put_act", lambda *a, **k: puts.append(a))
    fed = Fed()
    real = invites.issue
    monkeypatch.setattr(invites, "issue", lambda tid, tier=None, **k: real(
        tid, tier, issued_by=k["issued_by"], table=fed, tenants=TENANTS, now=NOW))
    body = {"intent": "issue_invite", "args": {"tenant_id": "org-bancox"}, "idempotency_key": "k1"}
    op = act_api.lambda_handler({"body": json.dumps(body), "headers": {"x-onca-origin": "s"},
                                 "requestContext": {"http": {"method": "POST"}}}, None)
    code = json.loads(op["body"])["invite"]["code"]
    assert op["statusCode"] == 200 and puts == [] and code not in json.dumps(journal, default=str)
    tenant = act_api.lambda_handler({"body": json.dumps(body), "headers": {},
                                     "requestContext": {"http": {"method": "POST"}, "authorizer": {"jwt": {"claims": {
                                         "sub": "u1", "custom:tenant": "org-bancox"}}}}}, None)
    assert tenant["statusCode"] == 403


# ---- account deletion knows about invite links --------------------------------------------------
def test_deletion_removes_the_sub_link_and_scrubs_the_invite():
    fed = Fed([{"email": "sub#" + APPLE_SUB, "tenant_id": "org-bancox", "contact_email": RELAY},
               {"email": "invite#h1", "tenant_id": "org-bancox", "used_by": APPLE_SUB},
               {"email": f"rl#sub#{APPLE_SUB}#2026101912", "n": 1},
               {"email": "colega@bancox.com.br", "tenant_id": "org-bancox"}])
    assert account.unmap_email(RELAY, APPLE_SUB, table=fed) == 1
    assert set(fed.items) == {"invite#h1", "colega@bancox.com.br"}
    assert fed.items["invite#h1"]["used_by"] == "deleted-user"


def test_invites_and_counters_are_not_counted_as_tenant_users():
    assert invites.is_person_row("colega@bancox.com.br") and invites.is_person_row("sub#x")
    assert not invites.is_person_row("invite#abc") and not invites.is_person_row("rl#ip#1.1.1.1#2026101912")


# ---- the gate: ways in to the organization come first; self-registration is never automatic -----
CTX = (ROOT / "src/dashboard/site/v2/context.js").read_text(encoding="utf-8")


def test_gate_orders_company_email_then_invite_then_new_individual_account():
    gate = CTX.split("if (isFederated()) {", 1)[1].split("\n      }\n", 1)[0]
    i1 = gate.index("Entre com o e-mail cadastrado pela sua empresa")
    i2 = gate.index("Tenho um código de convite")
    i3 = gate.index("Criar uma conta individual nova")
    assert i1 < i2 < i3
    assert "checked" not in gate.split("__regForm", 1)[1]                 # nothing pre-selected
    assert 'class="btn" style="margin-top:var(--s3)">Criar conta individual' in gate   # not the primary button
    assert "Compartilhar meu e-mail" in gate and "privaterelay" in gate


def test_after_linking_the_token_is_reissued_with_the_same_provider():
    body = CTX.split("async function reissueToken() {", 1)[1].split("\n  }\n", 1)[0]
    assert "ensureSession()" in body and "location.reload()" in body
    assert 'login(/apple/i.test(idp) ? "SignInWithApple" : "Google")' in body
    assert 'await login("Google");' not in CTX
    assert "await redeemInvite(code)" in CTX and "postRegister({ invite: code })" in CTX


def test_admin_can_issue_an_invite_shown_once():
    html = (ROOT / "src/dashboard/site/v2/admin/index.html").read_text(encoding="utf-8")
    form = html.split("function openInviteForm() {", 1)[1].split("\nfunction ", 1)[0]
    assert 'prAct("issue_invite", args)' in form and "não aparece de novo" in form
    assert 'id="invOpen"' in html and 'addEventListener("click", openInviteForm)' in html


def test_privacy_explains_relay_addresses_and_invites():
    html = (ROOT / "src/dashboard/site/docs/privacy.html").read_text(encoding="utf-8")
    assert "privaterelay.appleid.com" in html and "nunca tentamos descobrir" in html and "never trying to find" in html
