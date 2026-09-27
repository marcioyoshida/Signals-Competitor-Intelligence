"""#178 — coverage alarm on the REAL news history (lambda-digests/news/, 2026-08-19..09-26)."""
import copy
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import coverage_alarm as ca
from src.synth import sector_events as se

FIX = Path(__file__).resolve().parent / "fixtures" / "sector_events"


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


HIST = _load("news_history_2026-08-19_09-26.json")
ENTITIES = _load("entities.json")["entities"]
RUNS = _load("news_runs_2026-09-25_26.json")["runs"]
DOU = _load("dou_2026-09-25.json")


def history_items():
    """Expand the compact fixture: verbatim sector/change items + per-company counts (whose
    titles carry no sector or change signal, so an empty title is faithful for the series)."""
    items = list(HIST["items"])
    for day, per in HIST["counts"].items():
        for company, n in per.items():
            for k in range(n):
                items.append({"id": f"h:{day}:{company}:{k}", "date": day, "company": company,
                              "title": "", "publisher": None})
    return items


TMAP = ca.term_industry_map(ENTITIES)
COVERED = {i for s in TMAP.values() for i in s}


def series_store():
    store = {"covered_since": "2026-08-19"}
    ca.add_items(store, history_items(), TMAP, covered=COVERED)
    return store


INDEX = {it["id"]: it for it in HIST["items"]}


def sector_event_store():
    store = {}
    for i, run in enumerate(RUNS):
        digest = {"news": copy.deepcopy(run["news"])}
        off, news = se.split_digest(digest)
        store, _ = se.build_events(store, off, news, today=dt.date.fromisoformat(run["run"][:10]))
    return store


def test_term_map_maps_news_terms_to_industries():
    assert "betting" in TMAP[se._fold("Betano")]
    # the ingester searches Caixa as "Caixa Econômica" (entity_registry.NEWS_TERM_OVERRIDES)
    assert "banking" in TMAP[se._fold("Caixa Econômica")]


def test_prefix_replay_fires_for_betting_on_0926():
    alarms = ca.evaluate(series_store(), as_of=dt.date(2026, 9, 26), events=[], items_index=INDEX)
    bet = [a for a in alarms if a["industry"] == "betting"]
    assert len(bet) == 1 and not bet[0]["suppressed"]
    a = bet[0]
    assert "regulatory_mentions" in a["triggers"] and a["reg_outlets"] == 3
    # raw volume did NOT spike (entity-bound queries) — which is exactly why incident #173 was missed
    assert "volume" not in a["triggers"]
    heads = [h["title"] for h in a["top_headlines"]]
    assert any("proibição das bets" in h for h in heads)
    text = ca.alert_text(a)
    assert text.startswith("possível evento setorial não capturado: betting")
    # only betting fires that day
    assert [x["industry"] for x in alarms if not x["suppressed"]] == ["betting"]


def test_silent_once_the_sector_event_exists():
    events = sector_event_store()["events"]
    assert events and events[0]["industry"] == "betting"
    alarms = ca.evaluate(series_store(), as_of=dt.date(2026, 9, 26), events=events, items_index=INDEX)
    bet = [a for a in alarms if a["industry"] == "betting"]
    assert bet and bet[0]["suppressed"] and bet[0]["matching_event"] == events[0]["id"]
    sent = ca.notify(alarms, store={}, as_of=dt.date(2026, 9, 26), topic_arn="arn:x",
                     publisher=lambda *a: (_ for _ in ()).throw(AssertionError("must not send")),
                     enabled=True)
    assert sent == []


def test_quiet_on_a_normal_week_0914_to_0920():
    store = series_store()
    fired = []
    for k in range(7):
        day = dt.date(2026, 9, 14) + dt.timedelta(days=k)
        fired += [(day, a["industry"]) for a in ca.evaluate(store, as_of=day, events=[], items_index=INDEX)]
    assert fired == []


def test_single_entity_burst_is_not_a_sector_spike():
    # 09-14/15: a Monashees fund mandate syndicated ~20x drove private-markets volume 5-6 sigma;
    # the breadth guard keeps one company's news from reading as a sector event.
    store = series_store()
    for day in (dt.date(2026, 9, 14), dt.date(2026, 9, 15)):
        assert not [a for a in ca.evaluate(store, as_of=day, events=[]) if a["industry"] == "private-markets"]


def test_insufficient_history_never_alarms():
    store = {"covered_since": "2026-09-20"}
    ca.add_items(store, [i for i in history_items() if i["date"] >= "2026-09-20"], TMAP, covered=COVERED)
    assert ca.evaluate(store, as_of=dt.date(2026, 9, 26), events=[]) == []


def test_add_items_is_idempotent_across_overlapping_digests():
    items = [i for i in history_items() if i["date"] == "2026-09-26"]
    a = ca.add_items({}, items, TMAP)
    b = ca.add_items(copy.deepcopy(a), items, TMAP)
    assert a == b


def test_sector_query_items_count_for_their_industry():
    store = ca.add_items({}, [{"id": "s1", "date": "2026-09-26", "query_kind": "sector",
                               "industries": ["crypto"], "title": "Regras novas", "company": "cripto"}], TMAP)
    assert "s1" in store["series"]["crypto"]["2026-09-26"]["ids"]


# --- delivery ---------------------------------------------------------------------------------
def _alarm():
    return ca.evaluate(series_store(), as_of=dt.date(2026, 9, 26), events=[], items_index=INDEX)


def test_notify_is_off_by_default(monkeypatch):
    monkeypatch.delenv("ONCA_COVERAGE_ALARM_NOTIFY", raising=False)
    monkeypatch.delenv("ONCA_ALERTS_TOPIC_ARN", raising=False)
    calls = []
    sent = ca.notify(_alarm(), store={}, as_of=dt.date(2026, 9, 26),
                     publisher=lambda arn, msg: calls.append(msg))
    assert calls == [] and sent == [{"industry": "betting", "sent": False, "reason": "disabled"}]


def test_notify_publishes_cloudwatch_shaped_message_the_notifier_understands(monkeypatch):
    calls = []
    store = {}
    sent = ca.notify(_alarm(), store=store, as_of=dt.date(2026, 9, 26), topic_arn="arn:aws:sns:x",
                     publisher=lambda arn, msg: calls.append((arn, msg)), enabled=True)
    assert sent == [{"industry": "betting", "sent": True}] and store["alerted"]["betting"] == "2026-09-26"
    arn, msg = calls[0]
    assert msg["AlarmName"] == "OncaCoverageAlarm-betting" and msg["NewStateValue"] == "ALARM"
    assert "possível evento setorial não capturado: betting" in msg["NewStateReason"]

    # the existing operator path renders it unchanged (alert_notifier → weekly_digest.send_alert)
    from src.dashboard import alert_notifier, weekly_digest
    heads = []
    monkeypatch.setattr(weekly_digest, "send_alert", lambda h, **kw: heads.append(h) or {"email": None})
    alert_notifier.lambda_handler({"Records": [{"Sns": {"Message": json.dumps(msg)}}]}, None)
    assert heads and "possível evento setorial não capturado: betting" in heads[0]

    # re-alert suppression: the 15:30 and 20:30 runs of the same day send nothing
    again = ca.notify(_alarm(), store=store, as_of=dt.date(2026, 9, 26), topic_arn="arn:aws:sns:x",
                      publisher=lambda arn, msg: calls.append((arn, msg)), enabled=True)
    assert again == [] and len(calls) == 1


# --- orchestrator with a fake S3 (history backfill from lambda-digests/news/) ------------------
class _Body:
    def __init__(self, b):
        self._b = b

    def read(self):
        return self._b


class FakeS3:
    def __init__(self, objects):
        self.objects = dict(objects)  # key -> (bytes, LastModified)

    def get_paginator(self, name):
        s3 = self

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k, "LastModified": lm} for k, (_, lm) in s3.objects.items()
                                    if k.startswith(Prefix)]}
        return P()

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": _Body(self.objects[Key][0])}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = (Body, dt.datetime.now(dt.timezone.utc))


def test_run_backfills_from_s3_history_and_alarms(monkeypatch):
    monkeypatch.delenv("ONCA_COVERAGE_ALARM_NOTIFY", raising=False)
    lm = dt.datetime(2026, 9, 26, 12, tzinfo=dt.timezone.utc)
    # one synthetic "digest" per day holding that day's real items (the series is by pub date)
    by_day = {}
    for it in history_items():
        by_day.setdefault(it["date"], []).append(it)
    objs = {f"lambda-digests/news/{d}.json": (json.dumps({"news": {"items": rows}}).encode(), lm)
            for d, rows in by_day.items()}
    s3 = FakeS3(objs)
    out = ca.run({"news": RUNS[-1]["news"]}, "b", events=[], entities=ENTITIES, s3=s3,
                 today=dt.date(2026, 9, 26))
    assert out["industries"] == ["betting"] and out["sent"] == 0
    saved = json.loads(s3.objects[ca.STORE_KEY][0])
    assert saved["last_run"]["alarms"][0]["industry"] == "betting"
    # second run the same day: no re-backfill needed, still one unsuppressed alarm, still unsent
    out2 = ca.run({"news": RUNS[-1]["news"]}, "b", events=[], entities=ENTITIES, s3=s3,
                  today=dt.date(2026, 9, 26))
    assert out2["industries"] == ["betting"]
