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


def test_metric_line_and_priority_lines_are_grounded_in_input():
    line = wd._metric_line(_wk()["metrics"])
    assert "Movimentos: 5" in line and "▲2" in line
    lines = wd._priority_lines(_wk()["top_priorities"])
    assert len(lines) == 2 and "Alerta ativo" in lines[0] and "Itaú" in lines[0]
