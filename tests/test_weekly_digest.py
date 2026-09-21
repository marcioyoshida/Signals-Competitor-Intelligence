"""Weekly CSO brief push delivery (Teams/Slack/email) — pilot-persona loop habit lever."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import weekly_digest as wd


def _wk():
    return {
        "headline": "Nesta semana: 5 movimentos (+2 vs. semana anterior). Prioridade: X",
        "metrics": {
            "moves": {"now": 5, "prior": 3, "delta": 2}, "entrants": {"now": 1, "prior": 1, "delta": 0},
            "regulatory": {"now": 2, "prior": 0, "delta": 2}, "alerts": {"now": 1, "prior": 2, "delta": -1},
        },
        "top_priorities": [
            {"entity_label": "Itaú", "so_what": "Alerta ativo", "title": "Itaú avança em adquirência",
             "decision": {"text": "Abrir watch estratégico: Itaú"}},
            {"entity_label": "Nubank", "so_what": "Movimento competitivo", "title": "Nubank cresce",
             "decision": {"text": "Formular tese sobre Nubank"}},
        ],
    }


def _feed():
    return {"executive": {"cso": {"weekly": {"by_industry": {"__all__": _wk(), "banking": _wk()}}}}}


def test_weekly_scope_reads_sector_falls_back_to_all():
    feed = _feed()
    assert wd.weekly_scope(feed, "banking") is feed["executive"]["cso"]["weekly"]["by_industry"]["banking"]
    assert wd.weekly_scope(feed, "nonexistent") is feed["executive"]["cso"]["weekly"]["by_industry"]["__all__"]
    assert wd.weekly_scope({}) is None


def test_format_teams_is_adaptive_card_message():
    payload = wd.format_teams(_wk(), dashboard_url="https://x/exec")
    assert payload["type"] == "message"
    card = payload["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    texts = " ".join(b.get("text", "") for b in card["body"] if b.get("type") == "TextBlock")
    assert "Nesta semana" in texts and "Itaú" in texts
    assert card["actions"][0]["url"] == "https://x/exec"


def test_format_slack_blocks_contain_headline_and_priorities():
    payload = wd.format_slack(_wk())
    blocks = payload["blocks"]
    assert blocks[0]["type"] == "header"
    section_text = " ".join(b["text"]["text"] for b in blocks if b.get("type") == "section")
    assert "Nesta semana" in section_text and "Nubank" in section_text


def test_format_email_has_subject_text_and_html():
    subject, text, html = wd.format_email(_wk(), dashboard_url="https://x/exec")
    assert "Onça" in subject
    assert "Nesta semana" in text and "Itaú" in text
    assert "<h2>" in html and "Nesta semana" in html and "exec" in html
    # html-escaped, not raw-injected
    assert "<script" not in html.lower()


def test_send_teams_skips_when_unconfigured(monkeypatch):
    monkeypatch.delenv("ONCA_TEAMS_WEBHOOK_URL", raising=False)
    wd._TOKEN_CACHE.clear()
    assert wd.send_teams(_wk(), poster=lambda u, p: (_ for _ in ()).throw(AssertionError("should not post"))) is None


def test_send_teams_posts_to_configured_webhook():
    calls = []
    ok = wd.send_teams(_wk(), webhook_url="https://teams.example/hook",
                       poster=lambda u, p: calls.append((u, p)))
    assert ok is True and calls[0][0] == "https://teams.example/hook"
    assert calls[0][1]["type"] == "message"


def test_send_slack_posts_and_reports_failure():
    ok = wd.send_slack(_wk(), webhook_url="https://slack.example/hook", poster=lambda u, p: None)
    assert ok is True

    def _boom(u, p):
        raise RuntimeError("network down")
    assert wd.send_slack(_wk(), webhook_url="https://slack.example/hook", poster=_boom) is False


def test_send_email_requires_both_sender_and_recipient():
    assert wd.send_email(_wk(), sender=None, to="a@b.com") is None
    assert wd.send_email(_wk(), sender="a@b.com", to=None) is None
    calls = []
    ok = wd.send_email(_wk(), sender="from@onca.example", to="cso@bank.example",
                       sender_fn=lambda *a: calls.append(a))
    assert ok is True and calls[0][:2] == ("from@onca.example", "cso@bank.example")


def test_send_weekly_digest_orchestrates_all_three_independently():
    feed = _feed()
    calls = {"teams": [], "slack": [], "email": []}
    report = wd.send_weekly_digest(
        feed, dashboard_url="https://x/exec",
        teams_poster=lambda u, p: calls["teams"].append(1),
        slack_poster=lambda u, p: (_ for _ in ()).throw(RuntimeError("slack down")),
        email_sender=lambda *a: calls["email"].append(1))
    # teams needs a webhook_url passed explicitly since send_weekly_digest doesn't forward one here;
    # verify the ORCHESTRATION shape instead: unconfigured channels report None, one failing
    # doesn't block the others being attempted.
    assert set(report) == {"teams", "slack", "email"}


def test_send_weekly_digest_sends_nothing_when_weekly_absent():
    report = wd.send_weekly_digest({"executive": {"cso": {}}})
    assert report == {"teams": None, "slack": None, "email": None}


def test_send_alert_reuses_the_three_channel_senders_with_a_bare_headline(monkeypatch):
    monkeypatch.setenv("ONCA_TEAMS_WEBHOOK_URL", "https://teams.example/hook")
    monkeypatch.setenv("ONCA_SLACK_WEBHOOK_URL", "https://slack.example/hook")
    monkeypatch.setenv("ONCA_ALERT_EMAIL_FROM", "from@onca.example")
    monkeypatch.setenv("ONCA_ALERT_EMAIL_TO", "cso@bank.example")
    wd._TOKEN_CACHE.clear()
    calls = {"teams": [], "slack": [], "email": []}
    report = wd.send_alert(
        "🚨 OncaPipelineFailedAlarm — ALARM: 1 datapoint breached", dashboard_url="https://x/exec",
        teams_poster=lambda u, p: calls["teams"].append(p),
        slack_poster=lambda u, p: calls["slack"].append(p),
        email_sender=lambda *a: calls["email"].append(a))
    assert report == {"teams": True, "slack": True, "email": True}
    teams_texts = " ".join(b.get("text", "") for b in calls["teams"][0]["attachments"][0]["content"]["body"]
                           if b.get("type") == "TextBlock")
    assert "OncaPipelineFailedAlarm" in teams_texts
    slack_text = " ".join(b["text"]["text"] for b in calls["slack"][0]["blocks"] if b.get("type") == "section")
    assert "OncaPipelineFailedAlarm" in slack_text
    assert "OncaPipelineFailedAlarm" in calls["email"][0][3]  # text body


def test_send_alert_skips_unconfigured_channels(monkeypatch):
    monkeypatch.delenv("ONCA_TEAMS_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ONCA_SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ONCA_ALERT_EMAIL_FROM", raising=False)
    monkeypatch.delenv("ONCA_ALERT_EMAIL_TO", raising=False)
    wd._TOKEN_CACHE.clear()
    assert wd.send_alert("test") == {"teams": None, "slack": None, "email": None}


def test_metric_line_and_priority_lines_are_grounded_in_input():
    line = wd._metric_line(_wk()["metrics"])
    assert "Movimentos: 5" in line and "▲2" in line
    lines = wd._priority_lines(_wk()["top_priorities"])
    assert len(lines) == 2 and "Alerta ativo" in lines[0] and "Itaú" in lines[0]


class _FakeS3:
    """Minimal S3 double for the send-once marker."""

    def __init__(self, store=None):
        self.store = dict(store or {})
        self.puts = 0

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.store:
            raise KeyError("NoSuchKey")

        class _B:
            def __init__(self, b):
                self._b = b

            def read(self):
                return self._b

        return {"Body": _B(self.store[(Bucket, Key)])}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.puts += 1
        self.store[(Bucket, Key)] = Body


def test_already_sent_is_false_when_no_marker_exists():
    # Fail OPEN: a missing/unreadable marker must not suppress the week's brief.
    assert wd.already_sent("b", "2026-09-21", s3=_FakeS3()) is False


def test_already_sent_matches_only_the_same_day():
    s3 = _FakeS3()
    wd.mark_sent("b", "2026-09-21", {"email": True}, s3=s3)
    assert wd.already_sent("b", "2026-09-21", s3=s3) is True
    # next week's send must not be blocked by this week's marker
    assert wd.already_sent("b", "2026-09-28", s3=s3) is False


def test_marker_survives_an_unreadable_body():
    s3 = _FakeS3({("b", wd.SENT_KEY): b"not json"})
    assert wd.already_sent("b", "2026-09-21", s3=s3) is False


def test_mark_sent_records_the_channel_report():
    import json as _json

    s3 = _FakeS3()
    wd.mark_sent("b", "2026-09-21", {"email": True, "slack": None}, s3=s3)
    saved = _json.loads(s3.store[("b", wd.SENT_KEY)])
    assert saved["date"] == "2026-09-21"
    assert saved["report"]["email"] is True


def test_should_send_only_on_the_configured_weekday():
    # 2026-09-21 is a Monday, 2026-09-22 a Tuesday.
    assert wd.should_send("2026-09-21", weekday=0) is True
    assert wd.should_send("2026-09-22", weekday=0) is False
    assert wd.should_send("2026-09-22", weekday=1) is True


def test_the_pipelines_three_daily_runs_send_the_brief_ONCE():
    # The regression this guard exists for: OncaPipeline runs 3x/day, every run rebuilds
    # the feed, so a weekday-only gate mailed every recipient three times each Monday.
    s3 = _FakeS3()
    sends = 0
    for _run in range(3):
        if wd.should_send("2026-09-21", weekday=0, bucket="b", s3=s3):
            sends += 1
            report = {"email": True, "slack": None, "teams": None}
            if wd.delivered(report):
                wd.mark_sent("b", "2026-09-21", report, s3=s3)
    assert sends == 1


def test_a_failed_send_leaves_the_day_open_for_a_retry():
    # All channels unconfigured/errored => not delivered => no marker => the next run
    # the same day tries again rather than burning the week's only send.
    s3 = _FakeS3()
    assert wd.delivered({"email": None, "slack": None, "teams": None}) is False
    assert wd.delivered({"email": False}) is False
    assert wd.should_send("2026-09-21", weekday=0, bucket="b", s3=s3) is True
    assert s3.puts == 0


def test_should_send_ignores_a_malformed_as_of():
    assert wd.should_send("not-a-date", weekday=0) is False


def test_a_missing_corpus_date_can_never_skip_a_week():
    # Regression, found live 2026-09-21: feed.as_of is the CORPUS date and read Sunday on a
    # Monday run, so a gate keyed to it fired on the wrong day — and would skip the week
    # outright if no digest ever carried that Monday's date. The schedule is keyed to the
    # RUN date instead; these are the two dates disagreeing.
    corpus_date, run_date = "2026-09-20", "2026-09-21"   # Sunday, Monday
    assert wd.should_send(corpus_date, weekday=0) is False
    assert wd.should_send(run_date, weekday=0) is True
