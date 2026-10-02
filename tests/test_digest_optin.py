"""Per-sector weekly digest opt-in: prefs API (auth, licence filter, consent), hashed one-click
unsubscribe, the per-user sender (grouping, lapsed sectors, failure isolation), Monday gate."""
import datetime as dt
import email
import email.policy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import digest_optin as d
from src.dashboard import push, weekly_digest

NOW = dt.datetime(2026, 10, 5, 9, 0, tzinfo=dt.timezone.utc)   # a Monday


class _T:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        return {"Item": dict(self.items[Key["pk"]])} if Key["pk"] in self.items else {}

    def put_item(self, Item):
        self.items[Item["pk"]] = dict(Item)

    def scan(self, **kw):
        return {"Items": [dict(v) for v in self.items.values()]}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, **kw):
        it = self.items[Key["pk"]]
        sets, _, removes = UpdateExpression.partition(" REMOVE ")
        for part in sets.replace("SET ", "").split(","):
            k, v = [x.strip() for x in part.split("=")]
            it[k] = ExpressionAttributeValues[v]
        for k in filter(None, (x.strip() for x in removes.split(","))):
            it.pop(k, None)


def _ev(path, body=None, claims=None, method="GET", qs=None):
    ev = {"rawPath": path, "body": json.dumps(body) if body is not None else None,
          "requestContext": {"http": {"method": method}}, "queryStringParameters": qs}
    if claims:
        ev["requestContext"]["authorizer"] = {"jwt": {"claims": claims}}
    return ev


U1 = {"sub": "u1", "custom:tenant": "t1", "email": "ana@bank.example", "email_verified": "true"}
MODS = lambda tenant, groups: ["banking", "insurance"]   # noqa: E731


def _api(ev, t):
    r = d.api_handler(ev, None, table=t, modules=MODS, now=NOW)
    return r["statusCode"], (json.loads(r["body"]) if "json" in r["headers"]["content-type"] else r["body"])


def test_prefs_require_a_verified_identity():
    t = _T()
    assert _api(_ev("/api/me/digest"), t)[0] == 403
    assert _api(_ev("/api/me/digest", {"sectors": ["banking"], "consent": True}, method="PUT"), t)[0] == 403
    assert not t.items


def test_get_lists_only_licensed_sectors_and_the_login_email():
    code, body = _api(_ev("/api/me/digest", claims=U1), _T())
    assert code == 200 and body["available"] == ["banking", "insurance"] and body["sectors"] == []
    assert body["email"] == "ana@bank.example" and body["policy_url"].endswith("/docs/privacy.html")


def test_put_rejects_sectors_outside_the_licence():
    t = _T()
    code, body = _api(_ev("/api/me/digest", {"sectors": ["banking", "crypto"], "consent": True}, U1, "PUT"), t)
    assert code == 403 and body["sectors"] == ["crypto"] and not t.items


def test_put_requires_explicit_consent_and_records_it():
    t = _T()
    assert _api(_ev("/api/me/digest", {"sectors": ["banking"]}, U1, "PUT"), t)[0] == 400
    code, body = _api(_ev("/api/me/digest", {"sectors": ["banking"], "consent": True}, U1, "PUT"), t)
    assert code == 200 and body["sectors"] == ["banking"]
    row = t.items["DIGEST#u1"]
    assert row["email"] == "ana@bank.example" and row["tenant"] == "t1"
    assert row["consent_at"] == NOW.isoformat(timespec="seconds")
    assert row["consent_version"] == d.CONSENT_VERSION and row["updated_at"]


def test_unverified_email_cannot_opt_in():
    claims = {**U1, "email_verified": "false"}
    assert _api(_ev("/api/me/digest", {"sectors": ["banking"], "consent": True}, claims, "PUT"), _T())[0] == 400


def test_empty_put_withdraws_and_drops_the_address():
    t = _T()
    _api(_ev("/api/me/digest", {"sectors": ["banking"], "consent": True}, U1, "PUT"), t)
    code, body = _api(_ev("/api/me/digest", {"sectors": []}, U1, "PUT"), t)
    row = t.items["DIGEST#u1"]
    assert code == 200 and row["sectors"] == [] and row["withdrawn_at"] and "email" not in row
    assert row["consent_version"] == d.CONSENT_VERSION      # the record of past consent stays


def test_push_api_delegates_digest_routes(monkeypatch):
    t = _T()
    monkeypatch.setattr(push, "modules_for", lambda tenant, groups: ["banking"])
    r = push.api_handler(_ev("/api/me/digest", claims=U1), None, table=t)
    assert r["statusCode"] == 200 and json.loads(r["body"])["available"] == ["banking"]


def test_unsubscribe_token_is_stored_only_as_a_hash_and_one_click_clears():
    t = _T()
    _api(_ev("/api/me/digest", {"sectors": ["banking", "insurance"], "consent": True}, U1, "PUT"), t)
    tok = d.mint_token("u1", table=t, now=NOW)
    assert not any(tok in json.dumps(v) or tok in k for k, v in t.items.items())
    assert "DIGESTTOK#" + d.token_hash(tok) in t.items
    # a GET (link prefetch) only shows the confirmation form
    code, page = _api(_ev(d.UNSUB_PATH, qs={"t": tok}), t)
    assert code == 200 and "method='post'" in page and t.items["DIGEST#u1"]["sectors"]
    code, page = _api(_ev(d.UNSUB_PATH, body=None, method="POST", qs={"t": tok}), t)
    row = t.items["DIGEST#u1"]
    assert code == 200 and row["sectors"] == [] and row["withdrawn_at"] and "email" not in row


def test_unsubscribe_with_a_bad_token_changes_nothing():
    t = _T()
    _api(_ev("/api/me/digest", {"sectors": ["banking"], "consent": True}, U1, "PUT"), t)
    assert _api(_ev(d.UNSUB_PATH, method="POST", qs={"t": "nope"}), t)[0] == 404
    assert _api(_ev(d.UNSUB_PATH, method="POST", qs={}), t)[0] == 400
    assert t.items["DIGEST#u1"]["sectors"] == ["banking"]


# ---- sender ---------------------------------------------------------------------------------
def _w(h):
    return {"headline": h, "metrics": {}, "top_priorities": []}


FEED = {"executive": {"cso": {"weekly": {"by_industry": {
    "banking": _w("Bancos: semana X"), "insurance": _w("Seguros: semana Y"),
    "__all__": _w("todos")}}}}, "sector_events": []}


def _row(sub, sectors, tenant="t1", **kw):
    return {"pk": "DIGEST#" + sub, "sub": sub, "tenant": tenant, "groups": [], "sectors": sectors,
            "email": f"{sub}@x.example", **kw}


def _send(t, *, mods=None, fail_for=()):
    sent = []

    def raw(sender, to, raw_bytes):
        if to in fail_for:
            raise RuntimeError("SES throttled")
        sent.append((to, email.message_from_bytes(raw_bytes, policy=email.policy.default)))

    tally = d.send_sector_digests(FEED, today="2026-10-05", table=t,
                                  modules=mods or (lambda tenant, g: {"t1": ["banking", "insurance"],
                                                                      "t2": ["banking"]}.get(tenant, [])),
                                  scope=lambda f, m: f, raw_sender=raw, sender="briefing@onssa.org", now=NOW)
    return tally, sent


def test_sender_sends_one_email_per_user_with_a_section_per_sector_and_unsub_headers():
    t = _T()
    t.put_item(Item=_row("a", ["banking", "insurance"]))
    t.put_item(Item=_row("b", []))                               # withdrawn: never mailed
    tally, sent = _send(t)
    assert tally["sent"] == 1 and len(sent) == 1
    to, msg = sent[0]
    assert to == "a@x.example" and "Bancos" in msg["Subject"] and "Seguros" in msg["Subject"]
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert msg["List-Unsubscribe"].startswith("<https://onssa.org/api/push/digest/unsubscribe?t=")
    body = msg.get_body(("plain",)).get_content()
    assert "Bancos: semana X" in body and "Seguros: semana Y" in body and "todos" not in body
    assert "https://onssa.org/exec" in body and "Cancelar inscrição" in body
    assert t.items["DIGEST#a"]["last_sent"] == "2026-10-05"
    tok = msg["List-Unsubscribe"].split("t=")[1].rstrip(">")
    assert "DIGESTTOK#" + d.token_hash(tok) in t.items


def test_sender_skips_sectors_the_tenant_no_longer_licenses():
    t = _T()
    t.put_item(Item=_row("a", ["banking", "insurance"], tenant="t2"))   # t2 lost insurance
    t.put_item(Item=_row("c", ["insurance"], tenant="t2"))              # nothing left licensed
    tally, sent = _send(t)
    assert tally["sent"] == 1 and tally["no_licence"] == 1
    assert "Seguros" not in sent[0][1]["Subject"] and "Seguros: semana Y" not in sent[0][1].as_string()


def test_one_users_ses_failure_does_not_stop_the_others_and_is_retried():
    t = _T()
    for s in ("a", "b", "c"):
        t.put_item(Item=_row(s, ["banking"]))
    tally, sent = _send(t, fail_for={"b@x.example"})
    assert tally["sent"] == 2 and tally["failed"] == 1 and len(sent) == 2
    assert "last_sent" not in t.items["DIGEST#b"]
    tally, sent = _send(t)                                       # the day's later run
    assert tally["sent"] == 1 and tally["already_sent"] == 2 and sent[0][0] == "b@x.example"


def test_no_sender_configured_sends_nothing(monkeypatch):
    monkeypatch.setattr(weekly_digest, "_config", lambda k: None)
    t = _T()
    t.put_item(Item=_row("a", ["banking"]))
    assert d.send_sector_digests(FEED, today="2026-10-05", table=t, raw_sender=lambda *a: 1/0)["sent"] == 0


def test_monday_gate_is_untouched():
    assert weekly_digest.should_send("2026-10-05", weekday=0)          # Monday
    assert not weekly_digest.should_send("2026-10-06", weekday=0)      # Tuesday
