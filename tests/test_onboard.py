"""#379: the operator onboarding Lambda (``onca-onboard``) — contract, guards, and what it may undo."""
from __future__ import annotations

import copy
import json
import re
from typing import Any

import pytest
from botocore.exceptions import ClientError

from scripts import provision_tenant as cli
from src.dashboard import invites, onboard, upgrade
from src.dashboard import tenant_config as tc

POOL = "pool-1"


# ---- fakes ------------------------------------------------------------------------------------
class _Table:
    """A DynamoDB table keyed by one attribute. Evaluates the few condition / update expressions
    onboard.py uses, pages its scan two rows at a time, and counts every write."""

    def __init__(self, key: str) -> None:
        self.key, self.items, self.writes = key, {}, 0

    def snapshot(self) -> str:
        return json.dumps(self.items, sort_keys=True)

    def get_item(self, Key):
        it = self.items.get(Key[self.key])
        return {"Item": copy.deepcopy(it)} if it else {}

    def _check(self, cur, cond, names, values, op):
        for term in (cond or "").split(" AND "):
            term = term.strip()
            if not term:
                continue
            m = re.fullmatch(r"(attribute_exists|attribute_not_exists)\((\S+)\)", term)
            if m:
                has = cur is not None and (names or {}).get(m.group(2), m.group(2)) in cur
                ok = has if m.group(1) == "attribute_exists" else not has
            else:
                name, val = (p.strip() for p in term.split("="))
                ok = cur is not None and cur.get((names or {}).get(name, name)) == values[val]
            if not ok:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, op)

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None, **_):
        cur = self.items.get(Item[self.key])
        if ConditionExpression and cur is not None:   # upgrade._claim's condition, as test_upgrade
            v = ExpressionAttributeValues or {}
            if not (cur.get("action") == v[":pending"] and cur.get("claimed_at", 0) < v[":stale"]):
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.writes += 1
        self.items[Item[self.key]] = copy.deepcopy(Item)

    def update_item(self, Key, UpdateExpression, ConditionExpression=None,
                    ExpressionAttributeNames=None, ExpressionAttributeValues=None, **_):
        k = Key[self.key]
        self._check(self.items.get(k), ConditionExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues, "UpdateItem")
        assert UpdateExpression.startswith("SET ")
        self.writes += 1
        item = self.items.setdefault(k, {self.key: k})
        for part in UpdateExpression[4:].split(","):
            name, val = (p.strip() for p in part.split("="))
            item[(ExpressionAttributeNames or {}).get(name, name)] = copy.deepcopy(
                ExpressionAttributeValues[val])

    def delete_item(self, Key, ConditionExpression=None, ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, **_):
        k = Key[self.key]
        self._check(self.items.get(k), ConditionExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues, "DeleteItem")
        self.writes += 1
        self.items.pop(k, None)

    def scan(self, ExclusiveStartKey=None, **_):
        keys = sorted(self.items)
        start = keys.index(ExclusiveStartKey[self.key]) + 1 if ExclusiveStartKey else 0
        page = keys[start:start + 2]
        out = {"Items": [copy.deepcopy(self.items[k]) for k in page]}
        if start + 2 < len(keys):
            out["LastEvaluatedKey"] = {self.key: page[-1]}
        return out


class _Cognito:
    """A pool whose username attribute is the e-mail: the real Username is an opaque id, and the
    admin calls accept either (as the live pool does)."""

    class exceptions:
        class UserNotFoundException(Exception):
            pass

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}   # e-mail -> user
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: str | None = None

    def seed(self, email, tenant=None, tier="saas", status="CONFIRMED"):
        attrs = {"email": email, "custom:tier": tier}
        if tenant:
            attrs["custom:tenant"] = tenant
        self.users[email] = {"Username": f"u-{len(self.users) + 1:04d}", "attrs": attrs,
                             "UserStatus": status, "Enabled": True}
        return self.users[email]

    def _find(self, name):
        for email, u in self.users.items():
            if name in (email, u["Username"]):
                return email, u
        raise self.exceptions.UserNotFoundException(name)

    def _call(self, name, kw):
        if self.fail == name:
            raise RuntimeError(f"{name} failed")
        self.calls.append((name, kw))

    def names(self):
        return [n for n, _ in self.calls]

    def admin_get_user(self, UserPoolId, Username):
        self._call("admin_get_user", {"Username": Username})
        _email, u = self._find(Username)
        return {"Username": u["Username"], "UserStatus": u["UserStatus"], "Enabled": u["Enabled"],
                "UserAttributes": [{"Name": k, "Value": v} for k, v in u["attrs"].items()]}

    def admin_create_user(self, **kw):
        self._call("admin_create_user", kw)
        if kw.get("MessageAction") == "RESEND":
            self._find(kw["Username"])
            return
        attrs = {a["Name"]: a["Value"] for a in kw["UserAttributes"]}
        self.seed(kw["Username"], status="FORCE_CHANGE_PASSWORD")["attrs"] = attrs

    def admin_update_user_attributes(self, UserPoolId, Username, UserAttributes):
        self._call("admin_update_user_attributes", {"Username": Username})
        self._find(Username)[1]["attrs"].update({a["Name"]: a["Value"] for a in UserAttributes})

    def admin_disable_user(self, UserPoolId, Username):
        self._call("admin_disable_user", {"Username": Username})
        self._find(Username)[1]["Enabled"] = False

    def admin_enable_user(self, UserPoolId, Username):
        self._call("admin_enable_user", {"Username": Username})
        self._find(Username)[1]["Enabled"] = True

    def admin_delete_user(self, UserPoolId, Username):
        self._call("admin_delete_user", {"Username": Username})
        del self.users[self._find(Username)[0]]


class _World:
    def __init__(self) -> None:
        self.tenants, self.fed, self.cognito = _Table("tenant_id"), _Table("email"), _Cognito()

    def call(self, _action, **kw):
        ev = {"action": _action, "request_id": "req-1", "actor": "fleet-monitor", "cohort": "pilot",
              "tenant_id": "pilot-acme"}
        ev.update(kw)
        out = onboard.handle({k: v for k, v in ev.items() if v is not ...}, table=self.tenants,
                             federated_table=self.fed, cognito=self.cognito, pool_id=POOL)
        json.dumps(out)   # what Lambda does with the return value
        return out

    def put(self, **kw):
        kw.setdefault("tier", "saas")
        kw.setdefault("modules", ["banking", "insurance"])
        return self.call("put", **kw)

    def snapshot(self):
        return self.tenants.snapshot(), self.fed.snapshot(), json.dumps(
            {e: {**u} for e, u in self.cognito.users.items()}, sort_keys=True)

    def writes(self):
        return self.tenants.writes + self.fed.writes + sum(
            1 for n in self.cognito.names() if n != "admin_get_user")


@pytest.fixture
def w():
    return _World()


def _purchase(w, tenant="pilot-acme", module="acquiring", tier="saas_mid", evt="evt_1", **kw):
    return upgrade.handle({"product": "onca", "tenant_ref": tenant, "tier": tier, "module": module,
                           "event": "checkout.session.completed", "status": "active",
                           "stripe_event_id": evt, **kw}, w.tenants)


# ---- put --------------------------------------------------------------------------------------
def test_put_creates_a_stamped_operator_tenant_and_answers_in_the_contract_shape(w, monkeypatch):
    monkeypatch.setattr(onboard, "_now", lambda: "2026-11-10T12:00:00+00:00")
    r = w.put(expires_on="2027-01-09", modules=["Insurance", " banking "])
    assert r == {"ok": True, "request_id": "req-1", "tenant_id": "pilot-acme", "tier": "saas",
                 "modules": ["banking", "insurance"], "user": "none", "login": None,
                 "onboard": {"cohort": "pilot", "expires_on": "2027-01-09"}, "changed": True}
    assert w.tenants.items["pilot-acme"] == {
        "tenant_id": "pilot-acme", "tier": "saas", "plane": "saas",
        "modules": ["banking", "insurance"],
        "onboard": {"cohort": "pilot", "expires_on": "2027-01-09", "by": "fleet-monitor",
                    "at": "2026-11-10T12:00:00+00:00", "request_id": "req-1"}}
    assert w.tenants.writes == 1   # the stamp is in the SAME put as the row, never a second write
    # a comp is not a trial: an operator tenant, unchanged by billing
    cfg = tc.get_tenant_config("pilot-acme", table=w.tenants)
    assert cfg["billing"] is None and cfg["modules"] == ["banking", "insurance"]


def test_put_entry_tier_with_a_non_entry_module_returns_the_existing_guards_message(w):
    with pytest.raises(ValueError) as guard:
        tc.put_tenant_config("pilot-acme", "entry", ["crypto", "banking"], table=_Table("tenant_id"))
    r = w.put(tier="entry", modules=["crypto", "banking"], email="ana@acme.example", login="password")
    assert r == {"ok": False, "request_id": "req-1", "error": f"REJECTED: {guard.value}"}
    assert "may only license entry-tier industries" in r["error"]
    assert not w.tenants.items and not w.fed.items and not w.cognito.users and w.writes() == 0


def test_put_passes_every_other_put_tenant_config_guard_through(w):
    assert "tier must be one of" in w.put(tier="platinum")["error"]
    assert "not launch-ready" in w.put(modules=["securitization"])["error"]
    assert w.writes() == 0


def test_put_rejects_an_unknown_module_on_a_tier_without_an_allow_list(w):
    r = w.put(modules=["banking", "bankng"])
    assert r["ok"] is False and "unknown module(s) ['bankng']" in r["error"]
    assert w.writes() == 0


@pytest.mark.parametrize("row", [
    # self-registered: the paid-Entry trial
    {"tenant_id": "pilot-acme", "tier": "entry", "modules": [], "plane": "portal",
     "billing_managed": True, "trial_until": "2099-01-01T00:00:00+00:00", "trial_modules": ["crypto"]},
    # a purchase, even a lapsed one
    {"tenant_id": "pilot-acme", "tier": "saas", "modules": ["insurance"], "plane": "saas",
     "subs": {"acquiring": {"status": "lapsed", "tier": "saas", "band": "saas_mid"}}},
    # a stamped tenant that has since bought
    {"tenant_id": "pilot-acme", "tier": "saas", "modules": ["insurance"], "plane": "saas",
     "onboard": {"cohort": "pilot"}, "billing_managed": True,
     "subs": {"acquiring": {"status": "active", "tier": "saas", "band": "saas_mid"}}},
])
def test_put_refuses_a_billing_managed_tenant_or_one_with_subs_and_leaves_it_byte_identical(w, row):
    w.tenants.items["pilot-acme"] = copy.deepcopy(row)
    before = w.snapshot()
    r = w.put(email="ana@acme.example", login="password")
    assert r["ok"] is False and r["error"].startswith("REJECTED: ") and "billing-managed" in r["error"]
    assert "use link" in r["error"]
    assert w.snapshot() == before and w.writes() == 0
    assert w.tenants.items["pilot-acme"] == row


def test_put_never_adopts_a_tenant_it_did_not_create(w):
    """A hand-provisioned row (the CLI, the deploy-time demo seed) stamped by a put would become
    removable. It is refused, and stays as it was."""
    tc.put_tenant_config("acquiring", "saas", ["acquiring"], table=w.tenants)
    before = w.snapshot()
    r = w.put(tenant_id="acquiring", modules=["acquiring", "banking"])
    assert r["ok"] is False and "not created by onboarding" in r["error"]
    assert w.snapshot() == before


def test_put_twice_with_the_same_payload_is_a_noop_and_the_stamp_is_there(w, monkeypatch):
    clock = iter(["2026-11-10T12:00:00+00:00", "2026-11-11T09:00:00+00:00", "2026-11-12T09:00:00+00:00"])
    monkeypatch.setattr(onboard, "_now", lambda: next(clock))
    payload = {"expires_on": "2027-01-09", "email": "ana@acme.example", "login": "password"}
    first = w.put(**payload)
    before = w.snapshot()
    second = w.put(**payload)
    assert first["changed"] is True and first["user"] == "created"
    assert second["changed"] is False and second["user"] == "updated"
    assert w.snapshot() == before   # tenant row, login marker and Cognito user all identical
    stamp = w.tenants.items["pilot-acme"]["onboard"]
    assert set(stamp) == {"cohort", "expires_on", "by", "at", "request_id"}
    assert stamp["at"] == "2026-11-10T12:00:00+00:00"
    assert {**second, "changed": True, "user": "created",
            "login": {**second["login"], "invite": "suppressed"}} == first
    # a different payload IS a change, and re-stamps
    third = w.put(**{**payload, "expires_on": "2027-03-01", "request_id": "req-2"})
    assert third["changed"] is True and third["onboard"]["expires_on"] == "2027-03-01"
    assert w.tenants.items["pilot-acme"]["onboard"]["request_id"] == "req-2"


def test_put_without_an_expiry_stores_no_null_and_reports_null(w):
    r = w.put()
    assert r["onboard"] == {"cohort": "pilot", "expires_on": None}
    assert "expires_on" not in w.tenants.items["pilot-acme"]["onboard"]
    assert w.put()["changed"] is False


def test_a_reput_keeps_plane_and_role_arn_an_operator_set_by_hand(w):
    w.put()
    arn = "arn:aws:iam::123456789012:role/OncaResolveCallerRole"
    tc.put_tenant_config("pilot-acme", "saas", ["banking", "insurance"], plane="marketplace",
                         resolve_caller_role_arn=arn, table=w.tenants)   # the operator CLI path
    assert tc.is_onboarded(w.tenants.items["pilot-acme"])                 # ...keeps the stamp
    w.put(modules=["banking"])
    row = w.tenants.items["pilot-acme"]
    assert row["plane"] == "marketplace" and row["resolve_caller_role_arn"] == arn
    assert row["modules"] == ["banking"]


# ---- the stamp survives the other writers -----------------------------------------------------
def test_the_stamp_survives_a_reput_a_purchase_and_a_combo(w):
    w.put()
    stamp = copy.deepcopy(w.tenants.items["pilot-acme"]["onboard"])
    tc.put_tenant_config("pilot-acme", "saas", ["banking"], table=w.tenants)
    assert w.tenants.items["pilot-acme"]["onboard"] == stamp
    tc.apply_purchase("pilot-acme", "saas_mid", "acquiring", "grant", event_id="evt_1", table=w.tenants)
    assert w.tenants.items["pilot-acme"]["onboard"] == stamp
    tc.apply_combo_purchase("pilot-acme", "pagamentos", "grant", event_id="evt_2", table=w.tenants)
    assert w.tenants.items["pilot-acme"]["onboard"] == stamp
    tc.apply_purchase("pilot-acme", "saas_mid", "acquiring", "revoke", event_id="evt_3", table=w.tenants)
    assert w.tenants.items["pilot-acme"]["onboard"] == stamp


def test_is_onboarded_tells_a_comp_tenant_from_a_customer_for_the_reports(w):
    w.put()
    tc.put_tenant_config("acme-bank", "saas", ["banking"], table=w.tenants)
    rows = tc.list_tenant_items(table=w.tenants)
    assert sorted(r["tenant_id"] for r in rows if not tc.is_onboarded(r)) == ["acme-bank"]
    assert not tc.is_onboarded(None) and not tc.is_onboarded({"tenant_id": "x", "onboard": {}})
    # still true once the pilot buys: the comp history does not make it a comp forever, so a
    # report that wants paying pilots counted must also look at `subs`
    _purchase(w)
    assert tc.is_onboarded(w.tenants.items["pilot-acme"])


# ---- logins -----------------------------------------------------------------------------------
def test_a_password_login_is_created_with_the_invitation_suppressed_by_default(w):
    r = w.put(email=" Ana@Acme.Example ", login="password")
    assert r["user"] == "created"
    assert r["login"] == {"type": "password", "cohort": "pilot", "expires_on": None,
                          "invite": "suppressed", "tracked": True}
    (create,) = [kw for n, kw in w.cognito.calls if n == "admin_create_user"]
    assert create["MessageAction"] == "SUPPRESS" and "DesiredDeliveryMediums" not in create
    assert create["Username"] == "ana@acme.example" and create["UserPoolId"] == POOL
    assert w.cognito.users["ana@acme.example"]["attrs"]["custom:tenant"] == "pilot-acme"


def test_send_invite_true_lets_cognito_send_its_invitation(w):
    r = w.put(email="ana@acme.example", login="password", send_invite=True)
    (create,) = [kw for n, kw in w.cognito.calls if n == "admin_create_user"]
    assert "MessageAction" not in create and create["DesiredDeliveryMediums"] == ["EMAIL"]
    assert r["login"]["invite"] == "sent"


def test_send_invite_on_a_login_onboarding_created_resends_only_before_the_first_sign_in(w):
    w.put(email="ana@acme.example", login="password")
    r = w.call("link", email="ana@acme.example", login="password", send_invite=True)
    assert r["user"] == "updated" and r["login"]["invite"] == "resent"
    assert w.cognito.calls[-1] == ("admin_create_user", {
        "UserPoolId": POOL, "Username": "ana@acme.example", "MessageAction": "RESEND",
        "DesiredDeliveryMediums": ["EMAIL"]})
    w.cognito.users["ana@acme.example"]["UserStatus"] = "CONFIRMED"
    n = len(w.cognito.calls)
    assert w.call("link", email="ana@acme.example", login="password", send_invite=True)["login"]["invite"] == "none"
    assert "admin_create_user" not in [c[0] for c in w.cognito.calls[n:]]


def test_cognito_user_link_is_the_read_only_twin_of_the_upsert_guard():
    c = _Cognito()
    assert tc.cognito_user_link(POOL, "ana@acme.example", "acme", client=c) is None
    assert tc.cognito_upsert_user(POOL, "ana@acme.example", "acme", "saas", client=c) == "created"
    u = c.users["ana@acme.example"]
    assert tc.cognito_user_link(POOL, "ana@acme.example", "acme", client=c) == {
        "username": u["Username"], "tenant": "acme", "status": "FORCE_CHANGE_PASSWORD", "enabled": True}
    with pytest.raises(ValueError) as link:
        tc.cognito_user_link(POOL, "ana@acme.example", "other", client=c)
    with pytest.raises(ValueError) as upsert:
        tc.cognito_upsert_user(POOL, "ana@acme.example", "other", "saas", client=c)
    assert str(link.value) == str(upsert.value)   # one guard, one message
    assert [n for n in c.names() if n not in ("admin_get_user", "admin_create_user")] == []
    # the default is unchanged for every existing caller: Cognito sends its invitation
    (create,) = [kw for n, kw in c.calls if n == "admin_create_user"]
    assert create["DesiredDeliveryMediums"] == ["EMAIL"] and "MessageAction" not in create


def test_a_marker_is_never_keyed_by_an_email(w):
    """If a pool ever handed back the address as the username, tracking must stop loudly."""
    w.put()
    w.cognito.seed = lambda email, **kw: w.cognito.users.setdefault(email, {
        "Username": email, "attrs": {}, "UserStatus": "FORCE_CHANGE_PASSWORD", "Enabled": True})
    with pytest.raises(RuntimeError, match="not opaque"):
        w.call("link", email="ana@acme.example", login="password")
    assert not w.fed.items


def test_the_login_marker_holds_no_email_and_is_not_a_person_row(w):
    w.put(email="ana@acme.example", login="password")
    username = w.cognito.users["ana@acme.example"]["Username"]
    key = f"onboard#pilot-acme#{username}"
    assert set(w.fed.items) == {key}
    assert "ana@acme.example" not in w.fed.snapshot()
    assert not invites.is_person_row(key)   # never counted as a user of the tenant (account.py)
    assert w.fed.items[key]["onboard"]["cohort"] == "pilot"


def test_link_adds_a_second_email_and_a_google_login_without_changing_the_row(w):
    w.put(email="ana@acme.example", login="password")
    row = w.tenants.snapshot()
    r = w.call("link", email="bruno@acme.example", login="password", cohort="friends-family",
               expires_on="2027-01-09", tier="sovereign", modules=["crypto"])   # tier/modules ignored
    assert r["ok"] and r["user"] == "created" and r["tier"] == "saas"
    assert r["modules"] == ["banking", "insurance"]
    assert r["onboard"] == {"cohort": "pilot", "expires_on": None}   # the tenant's own stamp
    assert r["login"] == {"type": "password", "cohort": "friends-family", "expires_on": "2027-01-09",
                          "invite": "suppressed", "tracked": True}
    g = w.call("link", email="Carla@Acme.example", login="google")
    assert g["user"] == "mapped" and g["login"]["tracked"] is True
    assert w.fed.items["carla@acme.example"]["tenant_id"] == "pilot-acme"
    assert w.fed.items["carla@acme.example"]["tier"] == "saas"
    assert w.tenants.snapshot() == row and w.tenants.writes == 1
    assert {u["attrs"]["custom:tenant"] for u in w.cognito.users.values()} == {"pilot-acme"}
    assert len(w.cognito.users) == 2
    assert w.call("get")["logins"] == {"password": 2, "google": 1}


def test_link_accepts_several_emails_on_a_shared_demo_tenant_it_does_not_own(w):
    tc.put_tenant_config("acquiring", "saas", ["acquiring"], table=w.tenants)   # the deploy-time seed
    row = w.tenants.snapshot()
    for i in range(3):
        r = w.call("link", tenant_id="acquiring", cohort="friends-family", login="password",
                   email=f"friend{i}@example.org", request_id=f"req-{i}")
        assert r["ok"] and r["user"] == "created" and r["onboard"] is None
    assert w.tenants.snapshot() == row and w.tenants.writes == 1
    assert len(w.cognito.users) == 3


def test_link_works_on_a_billing_managed_tenant(w):
    w.tenants.items["entry-ab12"] = {"tenant_id": "entry-ab12", "tier": "entry", "modules": [],
                                     "plane": "portal", "billing_managed": True,
                                     "subs": {"banking": {"status": "active", "tier": "saas",
                                                          "band": "saas_premium"}}}
    row = w.tenants.snapshot()
    r = w.call("link", tenant_id="entry-ab12", email="cfo@acme.example", login="password")
    assert r["ok"] and r["tier"] == "saas" and r["modules"] == ["banking"]   # the effective view
    assert w.cognito.users["cfo@acme.example"]["attrs"]["custom:tier"] == "saas"
    assert w.tenants.snapshot() == row


@pytest.mark.parametrize("row", [
    {"billing_managed": True, "trial_until": "2099-01-01T00:00:00+00:00", "trial_modules": ["crypto"]},
    {"subs": {"banking": {"status": "active", "tier": "saas", "band": "saas_premium"}}},
])
def test_a_friends_family_guest_is_never_linked_to_a_customers_tenant(w, row):
    w.tenants.items["acme"] = {"tenant_id": "acme", "tier": "entry", "modules": [],
                               "plane": "portal", **row}
    before = w.snapshot()
    r = w.call("link", tenant_id="acme", cohort="friends-family", login="password",
               email="friend@example.org")
    assert r["ok"] is False and "customer's" in r["error"]
    assert w.snapshot() == before and w.writes() == 0
    # the same tenant still takes a pilot's second user
    assert w.call("link", tenant_id="acme", cohort="pilot", login="password",
                  email="cfo@acme.example")["ok"]


def test_link_to_an_unknown_tenant_is_rejected(w):
    r = w.call("link", email="ana@acme.example", login="password")
    assert r == {"ok": False, "request_id": "req-1", "error": "REJECTED: unknown tenant 'pilot-acme'"}
    assert w.writes() == 0


def test_relinking_an_email_that_belongs_to_another_tenant_is_rejected(w):
    w.put()
    w.cognito.seed("ana@acme.example", tenant="other-tenant")
    w.fed.items["carla@acme.example"] = {"email": "carla@acme.example", "tenant_id": "other-tenant",
                                         "tier": "saas"}
    before = w.snapshot()
    r = w.call("link", email="ana@acme.example", login="password")
    assert r["ok"] is False and "already linked to tenant 'other-tenant'" in r["error"]   # the existing guard
    g = w.call("link", email="carla@acme.example", login="google")
    assert g["ok"] is False and "already mapped to another tenant" in g["error"]
    # the same on put, BEFORE the row is written
    assert w.put(tenant_id="pilot-two", email="ana@acme.example", login="password")["ok"] is False
    assert w.put(tenant_id="pilot-two", email="carla@acme.example", login="google")["ok"] is False
    assert "pilot-two" not in w.tenants.items
    assert w.snapshot() == before and w.writes() == 1   # only the first put


def test_a_cognito_user_without_a_tenant_cannot_be_linked(w):
    """cognito_upsert_user would answer "updated", but custom:tenant can never be set afterwards."""
    w.put()
    w.cognito.seed("group-only@acme.example", tenant=None)
    before = w.snapshot()
    r = w.call("link", email="group-only@acme.example", login="password")
    assert r["ok"] is False and "no tenant" in r["error"]
    assert w.snapshot() == before


def test_a_login_that_existed_before_onboarding_is_linked_but_never_tracked(w):
    w.put()
    w.cognito.seed("old@acme.example", tenant="pilot-acme", tier="entry")
    w.fed.items["old-g@acme.example"] = {"email": "old-g@acme.example", "tenant_id": "pilot-acme",
                                         "tier": "entry"}
    r = w.call("link", email="old@acme.example", login="password")
    assert r["user"] == "updated" and r["login"]["tracked"] is False
    assert w.cognito.users["old@acme.example"]["attrs"]["custom:tier"] == "saas"
    g = w.call("link", email="old-g@acme.example", login="google")
    assert g["user"] == "mapped" and g["login"]["tracked"] is False
    assert "onboard" not in w.fed.items["old-g@acme.example"]
    assert w.call("get")["logins"] == {"password": 0, "google": 0}
    # ...so neither expire nor remove touches them
    assert w.call("expire", email="old@acme.example")["ok"] is False
    assert w.call("remove")["ok"] is True
    assert w.cognito.users["old@acme.example"]["Enabled"] is True
    assert "old-g@acme.example" in w.fed.items


# ---- expire / remove --------------------------------------------------------------------------
def test_expire_and_remove_on_a_tenant_without_the_stamp_are_rejected(w):
    tc.put_tenant_config("acme-bank", "saas", ["banking"], table=w.tenants)   # hand-provisioned
    w.cognito.seed("ops@acme.example", tenant="acme-bank")
    before, writes = w.snapshot(), w.writes()
    for action in ("expire", "remove"):
        r = w.call(action, tenant_id="acme-bank")
        assert r["ok"] is False and "no onboard stamp" in r["error"]
        r = w.call(action, tenant_id="acme-bank", email="ops@acme.example")   # a login it never made
        assert r["ok"] is False and "not created by onboarding" in r["error"]
        assert w.call(action, tenant_id="nobody")["error"] == "REJECTED: unknown tenant 'nobody'"
    assert w.snapshot() == before and w.writes() == writes


def test_on_an_unstamped_tenant_only_a_login_onboarding_linked_can_be_expired_or_removed(w):
    tc.put_tenant_config("acquiring", "saas", ["acquiring"], table=w.tenants)   # shared demo tenant
    w.cognito.seed("demo@onca.example", tenant="acquiring")                     # its seeded login
    for e in ("f1@example.org", "f2@example.org"):
        w.call("link", tenant_id="acquiring", cohort="friends-family", email=e, login="password")
    w.call("link", tenant_id="acquiring", cohort="friends-family", email="f3@example.org", login="google")
    row = w.tenants.snapshot()
    r = w.call("expire", tenant_id="acquiring", email="f1@example.org")
    assert r["ok"] and r["scope"] == "login" and r["mode"] == "disable"
    assert r["logins"] == {"disabled": 1, "deleted": 0, "gone": 0, "kept": False}
    assert w.cognito.users["f1@example.org"]["Enabled"] is False
    r = w.call("remove", tenant_id="acquiring", email="f2@example.org")
    assert r["ok"] and r["logins"]["deleted"] == 1 and "f2@example.org" not in w.cognito.users
    r = w.call("remove", tenant_id="acquiring", email="f3@example.org", login="google")
    assert r["ok"] and r["logins"]["deleted"] == 1 and "f3@example.org" not in w.fed.items
    assert w.call("remove", tenant_id="acquiring", email="demo@onca.example")["ok"] is False
    assert w.cognito.users["demo@onca.example"]["Enabled"] is True
    assert w.tenants.snapshot() == row   # the tenant row is not onboarding's: untouched throughout


def test_expire_disables_by_default_and_lapses_the_comp_modules(w, monkeypatch):
    monkeypatch.setattr(onboard, "_now", lambda: "2026-11-10T12:00:00+00:00")
    w.put(email="ana@acme.example", login="password", expires_on="2026-12-01")
    w.call("link", email="carla@acme.example", login="google")
    r = w.call("expire", cohort=...)   # the sweep need not know the cohort
    assert r["ok"] and r["mode"] == "disable" and r["modules"] == [] and r["scope"] == "tenant"
    assert r["logins"] == {"disabled": 1, "deleted": 1, "gone": 0, "kept": False}
    assert r["onboard"] == {"cohort": "pilot", "expires_on": "2026-12-01",
                            "expired_at": "2026-11-10T12:00:00+00:00"}
    row = w.tenants.items["pilot-acme"]
    assert row["modules"] == [] and row["onboard"]["expired_modules"] == ["banking", "insurance"]
    assert tc.get_tenant_config("pilot-acme", table=w.tenants)["modules"] == []   # fails closed
    assert w.cognito.users["ana@acme.example"]["Enabled"] is False                # not deleted
    assert "carla@acme.example" not in w.fed.items   # a mapping has no disabled state
    before = w.snapshot()
    assert w.call("expire")["logins"]["disabled"] == 1   # again: nothing new happens
    assert w.snapshot() == before


def test_a_put_after_expire_restores_the_comp_and_wakes_the_login(w):
    w.put(email="ana@acme.example", login="password")
    w.call("expire")
    r = w.put(email="ana@acme.example", login="password", expires_on="2027-06-01")
    assert r["changed"] is True and r["modules"] == ["banking", "insurance"] and r["user"] == "updated"
    assert "expired_at" not in w.tenants.items["pilot-acme"]["onboard"]
    assert w.cognito.users["ana@acme.example"]["Enabled"] is True
    assert "admin_enable_user" in w.cognito.names()
    (marker,) = w.fed.items.values()
    assert "disabled_at" not in marker and marker["onboard"]["expires_on"] == "2027-06-01"


def test_expire_mode_delete_deletes_the_logins_and_keeps_the_expired_row(w):
    w.put(email="ana@acme.example", login="password")
    r = w.call("expire", mode="delete")
    assert r["logins"] == {"disabled": 0, "deleted": 1, "gone": 0, "kept": False}
    assert not w.cognito.users and not w.fed.items
    assert w.tenants.items["pilot-acme"]["modules"] == []


def test_a_login_deleted_out_of_band_is_reported_gone_not_an_error(w):
    w.put(email="ana@acme.example", login="password")
    w.cognito.users.clear()   # e.g. the person deleted their own account
    r = w.call("expire")
    assert r["ok"] and r["logins"] == {"disabled": 0, "deleted": 0, "gone": 1, "kept": False}
    assert not w.fed.items


def test_stamped_tenant_then_a_storefront_purchase_then_expire_keeps_the_purchase(w):
    """Pilot hand-off: the purchase survives and only the comp modules lapse."""
    w.put(email="ana@acme.example", login="password")
    assert _purchase(w, module="acquiring") == {"ok": True, "action": "grant", "billing": "active"}
    assert _purchase(w, tier="saas_combo", combo="pagamentos", module="", evt="evt_2")["ok"]
    assert tc.is_onboarded(w.tenants.items["pilot-acme"])   # the purchase kept the stamp
    cfg = tc.get_tenant_config("pilot-acme", table=w.tenants)
    assert cfg["modules"] == ["acquiring", "banking", "fintech", "insurance"]
    subs = copy.deepcopy(w.tenants.items["pilot-acme"]["subs"])

    # a paying tenant is never rewritten or deleted from the panel
    assert "billing-managed" in w.put(modules=["banking"])["error"]
    assert "has a purchase" in w.call("remove")["error"]

    r = w.call("expire")
    assert r["ok"] and r["modules"] == ["acquiring", "fintech"]   # what was bought, nothing else
    assert r["logins"] == {"disabled": 0, "deleted": 0, "gone": 0, "kept": True}
    row = w.tenants.items["pilot-acme"]
    assert row["subs"] == subs and row["billing_managed"] is True and row["modules"] == []
    cfg = tc.get_tenant_config("pilot-acme", table=w.tenants)
    assert cfg["modules"] == ["acquiring", "fintech"] and cfg["billing"]["state"] == "active"
    assert tc.entitled(cfg, ["acquiring"]) and not tc.entitled(cfg, ["banking"])
    assert w.cognito.users["ana@acme.example"]["Enabled"] is True   # a customer's login now
    # and the storefront keeps working on the row afterwards
    _purchase(w, tier="saas_combo", combo="pagamentos", module="", evt="evt_3",
              event="customer.subscription.deleted", status="canceled")
    assert tc.get_tenant_config("pilot-acme", table=w.tenants)["modules"] == ["acquiring"]
    assert "has a purchase" in w.call("remove")["error"]   # a lapsed purchase is still a purchase


def test_remove_deletes_the_row_and_only_the_logins_onboarding_created(w):
    w.put(email="ana@acme.example", login="password")
    w.call("link", email="carla@acme.example", login="google")
    tc.put_tenant_config("acme-bank", "saas", ["banking"], table=w.tenants)
    w.fed.items["invite#abc"] = {"email": "invite#abc", "tenant_id": "pilot-acme", "tier": "saas"}
    w.fed.items["sub#123"] = {"email": "sub#123", "tenant_id": "pilot-acme", "tier": "saas"}
    r = w.call("remove")
    assert r["ok"] and r["removed"] is True and r["modules"] == []
    assert r["logins"] == {"disabled": 0, "deleted": 2, "gone": 0, "kept": False}
    assert set(w.tenants.items) == {"acme-bank"} and not w.cognito.users
    assert set(w.fed.items) == {"invite#abc", "sub#123"}   # invite + sub links are not onboarding's
    assert w.call("get")["error"] == "REJECTED: unknown tenant 'pilot-acme'"


def test_remove_can_be_repeated_after_a_failure_halfway(w):
    w.put(email="ana@acme.example", login="password")
    w.cognito.fail = "admin_delete_user"
    with pytest.raises(RuntimeError):   # unexpected: raised, not returned
        w.call("remove")
    assert "pilot-acme" in w.tenants.items   # logins go first, so the row is still there
    w.cognito.fail = None
    assert w.call("remove")["ok"] and not w.tenants.items and not w.cognito.users


def test_remove_keeps_the_row_when_a_purchase_lands_while_it_runs(w, monkeypatch):
    w.put()
    real = onboard._reverse_all

    def racing(ctx, tenant, logins, mode):
        _purchase(w)   # the storefront webhook, between the check and the delete
        return real(ctx, tenant, logins, mode)

    monkeypatch.setattr(onboard, "_reverse_all", racing)
    r = w.call("remove")
    assert r["ok"] is False and "the row was kept" in r["error"]
    assert w.tenants.items["pilot-acme"]["subs"]["acquiring"]["status"] == "active"


# ---- get --------------------------------------------------------------------------------------
def test_get_returns_the_entitlement_the_stamp_and_login_counts_without_writing(w):
    w.put(email="ana@acme.example", login="password", expires_on="2027-01-09")
    before = w.writes()
    r = w.call("get", cohort=..., actor=...)
    assert r == {"ok": True, "request_id": "req-1", "tenant_id": "pilot-acme", "tier": "saas",
                 "modules": ["banking", "insurance"], "user": "none", "plane": "saas",
                 "onboard": {"cohort": "pilot", "expires_on": "2027-01-09"}, "onboarded": True,
                 "billing_managed": False, "has_subs": False, "logins": {"password": 1, "google": 0}}
    assert w.writes() == before


# ---- validation, errors, logging --------------------------------------------------------------
@pytest.mark.parametrize("change, needle", [
    ({"action": "upsert"}, "action must be one of"),
    ({"request_id": ...}, "request_id is required"),
    ({"request_id": "a b"}, "request_id is required"),
    ({"tenant_id": "Acme Bank"}, "tenant_id must be a slug"),
    ({"tenant_id": "ana@acme.example"}, "tenant_id must be a slug"),
    ({"tenant_id": "BILLING-x"}, "tenant_id must be a slug"),
    ({"tenant_id": ...}, "tenant_id must be a slug"),
    ({"cohort": "vip"}, "cohort must be one of"),
    ({"cohort": ...}, "cohort must be one of"),
    ({"actor": ...}, "actor is required"),
    ({"actor": "ana@acme.example"}, "actor is required"),
    ({"tier": ...}, "tier is required"),
    ({"modules": ...}, "modules is required"),
    ({"modules": []}, "modules is required"),
    ({"modules": "banking"}, "modules is required"),
    ({"email": "not-an-email"}, "email must be one e-mail"),
    ({"email": "sub#1@x.example"}, "email must be one e-mail"),
    ({"email": "ana@acme.example"}, "login is required with email"),
    ({"email": "ana@acme.example", "login": "apple"}, "login must be one of"),
    ({"email": "ana@acme.example", "login": "google", "send_invite": True}, "send_invite only applies"),
    ({"email": "ana@acme.example", "login": "password", "send_invite": "false"}, "send_invite must be"),
    ({"expires_on": "09/01/2027"}, "expires_on must be YYYY-MM-DD"),
    ({"expires_on": "2027-02-30"}, "expires_on must be YYYY-MM-DD"),
])
def test_validation_errors_are_returned_not_raised_and_nothing_is_written(w, change, needle):
    r = w.put(**change)
    assert r["ok"] is False and r["error"].startswith("REJECTED: ") and needle in r["error"]
    assert set(r) == {"ok", "request_id", "error"}
    assert w.writes() == 0 and not w.cognito.calls


def test_action_specific_validation(w):
    assert "email must be one e-mail" in w.call("link")["error"]
    assert "mode must be one of" in w.call("expire", mode="downgrade")["error"]
    assert "send_invite only applies" in w.call("expire", email="a@b.example", login="password",
                                                send_invite=True)["error"]
    assert onboard.handle("put")["error"] == "REJECTED: the event must be a JSON object"
    assert onboard.handle(None) == {"ok": False, "request_id": None,
                                    "error": "REJECTED: the event must be a JSON object"}


def test_an_unexpected_failure_raises(w, capsys):
    def boom(Key):
        raise RuntimeError("dynamodb is down")

    w.tenants.get_item = boom
    with pytest.raises(RuntimeError, match="dynamodb is down"):
        w.put()
    line = json.loads(capsys.readouterr().out)
    assert line["outcome"] == "error" and line["reason"] == "RuntimeError"


def test_a_missing_pool_id_is_a_deploy_fault_raised_before_anything_is_written(w, monkeypatch):
    monkeypatch.delenv("ONCA_USER_POOL_ID", raising=False)
    with pytest.raises(RuntimeError, match="ONCA_USER_POOL_ID"):
        onboard.handle({"action": "put", "request_id": "r", "actor": "a", "cohort": "pilot",
                        "tenant_id": "pilot-acme", "tier": "saas", "modules": ["banking"],
                        "email": "ana@acme.example", "login": "password"},
                       table=w.tenants, federated_table=w.fed, cognito=w.cognito)
    assert not w.tenants.items


def test_one_structured_log_line_per_call_and_never_an_email(w, capsys):
    w.put(email="Ana@Acme.example", login="password", cohort="friends-family")
    w.cognito.seed("taken@acme.example", tenant="other-tenant")
    w.call("link", email="taken@acme.example", login="password")   # the guard's message names the e-mail
    w.put(tenant_id="ana@acme.example")                             # an e-mail typed in the wrong field
    w.call("get", cohort=..., actor=...)
    w.call("expire", email="ana@acme.example")
    w.call("remove")
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 6
    logs = [json.loads(line) for line in lines]
    assert "@" not in "".join(lines) and "acme.example" not in "".join(lines)
    assert logs[0] == {"fn": "onca-onboard", "action": "put", "tenant": "pilot-acme",
                       "cohort": "friends-family", "request_id": "req-1", "actor": "fleet-monitor",
                       "outcome": "ok", "user": "created",
                       "email_sha": onboard.email_hash("ana@acme.example")}
    assert re.fullmatch(r"[0-9a-f]{12}", logs[0]["email_sha"])
    assert (logs[1]["outcome"], logs[1]["reason"], logs[1]["action"]) == ("rejected", "cognito", "link")
    assert logs[2]["outcome"] == "rejected" and logs[2]["tenant"] is None and logs[2]["action"] == "put"
    assert [x["outcome"] for x in logs[3:]] == ["ok", "ok", "ok"]
    assert [x["action"] for x in logs] == ["put", "link", "put", "get", "expire", "remove"]
    assert all(x["request_id"] == "req-1" for x in logs)


def test_lambda_handler_resolves_its_stores_from_the_environment(w, monkeypatch):
    w.put()
    asked = []

    def table(t=None):
        asked.append(("tenants", t))
        return w.tenants

    def fed(t=None):
        asked.append(("fed", t))
        return w.fed

    monkeypatch.setattr(tc, "_table", table)
    monkeypatch.setattr(tc, "_federated_table", fed)
    r = onboard.lambda_handler({"action": "get", "request_id": "r-9", "tenant_id": "pilot-acme"}, None)
    assert r["ok"] and r["request_id"] == "r-9" and r["onboarded"] is True
    # nothing injected: the first ask for each store is for the env-named table
    assert asked[0] == ("tenants", None) and ("fed", None) in asked


# ---- the CLI and the Lambda share their writes -------------------------------------------------
def test_the_cli_and_the_lambda_write_the_same_row_for_the_same_input(w, monkeypatch, capsys):
    import boto3

    cli_tenants, cli_fed, cli_cognito = _Table("tenant_id"), _Table("email"), _Cognito()
    monkeypatch.setattr(cli, "_resolve_table", lambda name: (cli_tenants, "onca-tenant-config"))
    monkeypatch.setattr(boto3, "client", lambda service, **kw: cli_cognito)
    assert cli.main(["put", "pilot-acme", "saas", "insurance", "banking", "--email",
                     "ana@acme.example", "--user-pool-id", POOL]) == 0
    monkeypatch.setattr(tc, "_federated_table", lambda table=None: cli_fed)
    assert cli.main(["put", "pilot-acme", "saas", "insurance", "banking", "--google-email",
                     "carla@acme.example"]) == 0
    monkeypatch.undo()

    w.put(modules=["insurance", "banking"], email="ana@acme.example", login="password", send_invite=True)
    w.call("link", email="carla@acme.example", login="google")

    lam = copy.deepcopy(w.tenants.items["pilot-acme"])
    stamp = lam.pop("onboard")
    assert lam == cli_tenants.items["pilot-acme"]          # the same row...
    assert set(stamp) == {"cohort", "by", "at", "request_id"}   # ...plus the stamp only the Lambda writes
    assert "onboard" not in cli_tenants.items["pilot-acme"]
    # the same Cognito user and the same Google mapping
    assert w.cognito.users["ana@acme.example"]["attrs"] == cli_cognito.users["ana@acme.example"]["attrs"]
    create = lambda c: [kw for n, kw in c.calls if n == "admin_create_user"]   # noqa: E731
    assert create(w.cognito) == create(cli_cognito)
    lam_map = copy.deepcopy(w.fed.items["carla@acme.example"])
    lam_map.pop("onboard")
    assert lam_map == cli_fed.items["carla@acme.example"]
