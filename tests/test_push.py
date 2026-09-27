"""#167 content-free push: scoping, quiet hours, cap, pruning, no-history-blast, API ownership."""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import push

BRT = push.BRT
NOON = dt.datetime(2026, 9, 28, 12, 0, tzinfo=BRT)
FEED = {"product_radar": {"alerts": [{"id": "radar:1", "date": "2026-09-27", "industries": ["banking"]}]},
        "sector_events": [{"id": "se:1", "severity": "critical", "date": "2026-09-27", "industries": ["betting"]},
                          {"id": "se:2", "severity": "medium", "date": "2026-09-27"}],
        "distress": []}


def _sub(**kw):
    s = {"pk": "SUB#a", "endpoint": "https://push.example/a", "tenant": "t1", "groups": [],
         "officer": "cso", "prefs": {"types": list(push.TYPES), "daily_cap": 2, "quiet": [21, 7]},
         "seen": [], "pending": 0, "fails": 0, "primed": True}
    s.update(kw)
    return s


def _run(subs, *, now=NOON, status=201, mods=("banking",), feed=FEED):
    sent, saved, dropped, stats = [], [], [], []
    tally = push.notify(feed, subs, now=now, send=lambda ep: sent.append(ep) or status,
                        save=saved.append, drop=dropped.append, bump=stats.append,
                        scope=lambda f, m: f if "banking" in m else {}, modules=lambda t, g: list(mods))
    return tally, sent, saved, dropped, stats


def test_sends_one_contentless_push_for_new_events_and_dedupes():
    s = _sub()
    tally, sent, *_ = _run([s])
    assert tally["sent"] == 1 and sent == ["https://push.example/a"]
    assert s["last_count"] == 2 and s["pending"] == 0            # radar + critical sector event
    tally, sent, *_ = _run([s])                                  # same events again: nothing new
    assert tally["sent"] == 0 and not sent


def test_first_run_primes_without_blasting_history():
    s = _sub(primed=False)
    tally, sent, *_ = _run([s])
    assert tally["primed"] == 1 and not sent and s["primed"] is True and len(s["seen"]) == 2


def test_quiet_hours_hold_and_later_roll_up():
    s = _sub()
    tally, sent, *_ = _run([s], now=dt.datetime(2026, 9, 28, 22, 30, tzinfo=BRT))
    assert tally["skipped_quiet"] == 1 and not sent and s["pending"] == 2
    tally, sent, *_ = _run([s], now=dt.datetime(2026, 9, 29, 8, 0, tzinfo=BRT))
    assert tally["sent"] == 1 and s["last_count"] == 2


def test_daily_cap_rolls_overflow_forward():
    s = _sub(sent_day="2026-09-28", sent_today=2)
    tally, sent, *_ = _run([s])
    assert tally["capped"] == 1 and not sent and s["pending"] == 2


def test_gone_subscription_is_pruned_and_repeated_failures_too():
    s = _sub()
    tally, _, _, dropped, stats = _run([s], status=410)
    assert dropped == [s] and "pruned" in stats
    s2 = _sub(fails=2)
    tally, _, _, dropped, _ = _run([s2], status=500)
    assert dropped == [s2]


def test_lost_entitlement_stops_pushes():
    s = _sub()
    tally, sent, _, dropped, _ = _run([s], mods=())
    assert not sent and dropped == [s]


def test_officer_types_scope_what_counts():
    s = _sub(officer="cpo", prefs={"types": ["radar"], "daily_cap": 3, "quiet": [21, 7]})
    _run([s])
    assert s["last_count"] == 1


def test_quiet_window_wraps_midnight():
    assert push.in_quiet(dt.datetime(2026, 9, 28, 23, 0, tzinfo=BRT))
    assert push.in_quiet(dt.datetime(2026, 9, 28, 6, 59, tzinfo=BRT))
    assert not push.in_quiet(dt.datetime(2026, 9, 28, 7, 0, tzinfo=BRT))


class _T:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        return {"Item": self.items.get(Key["pk"])} if Key["pk"] in self.items else {}

    def put_item(self, Item):
        self.items[Item["pk"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["pk"], None)

    def update_item(self, Key, **kw):
        if Key["pk"] in self.items and ":z" in (kw.get("ExpressionAttributeValues") or {}):
            self.items[Key["pk"]]["pending"] = 0


def _ev(path, body=None, claims=None, method="POST"):
    ev = {"rawPath": path, "body": json.dumps(body or {}), "requestContext": {"http": {"method": method}}}
    if claims:
        ev["requestContext"]["authorizer"] = {"jwt": {"claims": claims}}
    return ev


def test_api_subscribe_is_owner_scoped_and_pending_returns_only_a_count(monkeypatch):
    t = _T()
    monkeypatch.setattr(push, "modules_for", lambda tenant, groups: ["banking"])
    sub = {"subscription": {"endpoint": "https://push.example/z"}, "officer": "cro"}
    assert push.api_handler(_ev("/api/me/push/subscribe", sub), None, table=t)["statusCode"] == 403
    r = push.api_handler(_ev("/api/me/push/subscribe", sub, {"sub": "u1", "custom:tenant": "t1"}), None, table=t)
    assert r["statusCode"] == 200 and json.loads(r["body"])["prefs"]["types"] == ["regulatory"]
    # another user cannot remove it
    push.api_handler(_ev("/api/me/push/unsubscribe", sub, {"sub": "u2"}), None, table=t)
    assert t.items
    t.items[push.sub_key("https://push.example/z")]["last_count"] = 3
    body = json.loads(push.api_handler(_ev("/api/push/pending", {"endpoint": "https://push.example/z"}), None, table=t)["body"])
    assert body == {"count": 3, "target": push.TARGET}
    push.api_handler(_ev("/api/me/push/unsubscribe", sub, {"sub": "u1"}), None, table=t)
    assert not t.items


def test_subscribe_without_entitlement_is_refused(monkeypatch):
    monkeypatch.setattr(push, "modules_for", lambda tenant, groups: [])
    r = push.api_handler(_ev("/api/me/push/subscribe", {"endpoint": "https://p.example/1"}, {"sub": "u"}), None, table=_T())
    assert r["statusCode"] == 403
