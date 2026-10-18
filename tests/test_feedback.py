"""Client reports from the Ask chat ("Reportar"): private store, 90-day TTL, operator-only triage,
nothing client-identifying in the journal, the owner e-mail or the public-ticket draft."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import act_api, feedback


class _Table:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def put_item(self, Item):
        self.items[Item["pk"]] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ExpressionAttributeNames=None,
                    ConditionExpression=None, ReturnValues=None):
        pk = Key["pk"]
        if ConditionExpression and pk not in self.items:
            raise RuntimeError("ConditionalCheckFailed")
        it = self.items.setdefault(pk, {"pk": pk})
        if UpdateExpression.startswith("ADD n"):
            it["n"] = it.get("n", 0) + 1
            it.setdefault("ttl", ExpressionAttributeValues[":ttl"])
            return {"Attributes": {"n": it["n"]}}
        it["status"] = ExpressionAttributeValues[":s"]
        it["triaged_at"] = ExpressionAttributeValues[":t"]
        if ":u" in ExpressionAttributeValues:
            it["ticket_url"] = ExpressionAttributeValues[":u"]
        return {}

    def scan(self, **kw):
        return {"Items": list(self.items.values())}

    def delete_item(self, Key):
        self.items.pop(Key["pk"], None)


TURN = {"q": "Quem está em alerta agora?", "a": "O Itaú está em alerta.", "cited": ["cand-ent-itau"]}


def test_report_is_stored_privately_with_90_day_ttl():
    t = _Table()
    r = feedback.record_report(kind="defeito", text="A resposta citou o banco errado", sub="user-1",
                               tenant="acme", turn=TURN, context={"officer": "cso", "view": "painel"}, table=t)
    assert r["ok"]
    item = t.items[f"FB#{r['report_id']}"]
    assert item["status"] == "novo" and item["tenant"] == "acme" and item["turn"]["cited"] == ["cand-ent-itau"]
    assert abs(item["ttl"] - (time.time() + 90 * 86400)) < 120


def test_invalid_reports_and_the_daily_cap(monkeypatch):
    t = _Table()
    assert feedback.record_report(kind="elogio", text="ok ok", sub="u", tenant=None, table=t)["reason"] == "kind"
    assert feedback.record_report(kind="sugestao", text=" ", sub="u", tenant=None, table=t)["reason"] == "text"
    monkeypatch.setattr(feedback, "DAILY_CAP", 2)
    oks = [feedback.record_report(kind="sugestao", text="ideia %d" % i, sub="u", tenant=None, table=t) for i in range(3)]
    assert [o["ok"] for o in oks] == [True, True, False] and oks[2]["reason"] == "daily-limit"


def test_ticket_draft_carries_no_client_data():
    t = _Table()
    rid = feedback.record_report(kind="defeito", text="Itaú aparece em alerta sem motivo", sub="user-1",
                                 tenant="acme-bank", turn=TURN, context={"officer": "cso"}, table=t)["report_id"]
    row = feedback.list_reports(table=t)[0]
    draft = json.dumps(row["ticket_draft"], ensure_ascii=False)
    for secret in ("acme-bank", "user-1", "Itaú", "alerta agora", "cand-ent-itau"):
        assert secret not in draft, secret
    assert row["report_id"] == rid and len(row["sub"]) <= 8


def test_triage_status_and_ticket_url():
    t = _Table()
    rid = feedback.record_report(kind="sugestao", text="Exportar em PDF", sub="u", tenant=None, table=t)["report_id"]
    assert feedback.set_status(rid, "ticket", ticket_url="https://github.com/o/r/issues/1", table=t)
    assert t.items[f"FB#{rid}"]["ticket_url"].endswith("/issues/1")
    assert not feedback.set_status(rid, "aprovado", table=t)
    assert not feedback.set_status("0" * 16, "ticket", table=t)          # unknown report
    feedback.set_status(rid, "descartado", ticket_url="javascript:alert(1)", table=t)
    assert t.items[f"FB#{rid}"]["ticket_url"].endswith("/issues/1")      # non-GitHub URL ignored
    assert [r["report_id"] for r in feedback.list_reports(status="descartado", table=t)] == [rid]


def test_delete_for_user_removes_only_their_reports():
    t = _Table()
    feedback.record_report(kind="sugestao", text="a a a", sub="u1", tenant=None, table=t)
    feedback.record_report(kind="sugestao", text="b b b", sub="u2", tenant=None, table=t)
    assert feedback.delete_for_user("u1", table=t) == 1
    assert [r["sub"] for r in feedback.list_reports(table=t)] == ["u2"]


def test_owner_email_is_content_free(monkeypatch):
    sent = {}

    class _Ses:
        def send_email(self, **kw):
            sent.update(kw)

    monkeypatch.setenv("ONCA_CONTACT_FORWARD_TO", "owner@example.com")
    assert feedback.notify_owner("defeito", ses=_Ses())
    body = json.dumps(sent["Message"], ensure_ascii=False)
    assert "defeito" in body and "/v2/admin/#relatos" in body and sent["Source"] == "contato@onssa.org"


# --- act_api wiring -----------------------------------------------------------------------
def _jwt_event(body, groups=None):
    claims = {"sub": "user-9", "custom:tenant": "acme"}
    if groups:
        claims["cognito:groups"] = groups
    return {"requestContext": {"authorizer": {"jwt": {"claims": claims}}, "http": {"method": "POST"}},
            "body": json.dumps(body)}


def test_client_can_report_but_not_triage(monkeypatch):
    t = _Table()
    monkeypatch.setattr(feedback, "_table", lambda table=None: t)
    monkeypatch.setattr(feedback, "notify_owner", lambda *a, **k: True)
    journal = []
    monkeypatch.setattr(act_api, "_journal", lambda *a, **k: journal.append(a))
    monkeypatch.setattr(act_api, "_put_act", lambda *a, **k: None)
    monkeypatch.setattr(act_api, "_get_act", lambda *a, **k: None)
    r = act_api.lambda_handler(_jwt_event({"intent": "report_feedback", "idempotency_key": "k1",
                                           "args": {"kind": "defeito", "text": "Resposta errada",
                                                    "tenant": "someone-else", "turn": TURN}}), None)
    assert r["statusCode"] == 200, r
    item = next(i for i in t.items.values() if i.get("type") == "report")
    assert item["tenant"] == "acme" and item["sub"] == "user-9"     # verified claims, not client args
    assert journal == []                                             # text never reaches the journal
    for intent in ("list_feedback", "set_feedback_status"):
        denied = act_api.lambda_handler(_jwt_event({"intent": intent, "args": {}}), None)
        assert denied["statusCode"] == 403, intent


def test_report_without_identity_is_refused():
    r = act_api.lambda_handler({"requestContext": {"http": {"method": "POST"}},
                                "body": json.dumps({"intent": "report_feedback", "args": {}})}, None)
    assert r["statusCode"] == 403
