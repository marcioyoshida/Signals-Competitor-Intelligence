"""Weekly CSO brief PUSH delivery — the pilot-persona loop's habit-forming lever.

`executive.build_cso_weekly` (feed.executive.cso.weekly) already synthesizes the delta-framed
weekly brief and renders it in `/exec`. A dashboard you have to visit doesn't create a habit; a
brief that arrives does. This module formats that same data for three channels (Teams, Slack,
email/SES) and sends it — best-effort per channel, fail-closed when a channel's config is
absent, and NEVER fabricated content (every number/priority comes straight from
`feed.executive.cso.weekly`, nothing invented here).

Config: each channel reads from its env var, falling back to the shared
``signalscompetitor/onca/api-key`` secret (JSON) — the same env→secret pattern as
`search_expansion.token()` / `gov_dados.token()`.
  Teams   ONCA_TEAMS_WEBHOOK_URL   — an Incoming Webhook / Power Automate "when a Teams webhook
                                     request is received" URL, created in the target Teams
                                     channel by its owner (this code cannot create one).
  Slack   ONCA_SLACK_WEBHOOK_URL   — a Slack Incoming Webhook URL (Slack app → Incoming Webhooks).
  Email   ONCA_ALERT_EMAIL_FROM / ONCA_ALERT_EMAIL_TO — both must be SES-verified identities
                                     (the account is in the SES sandbox as of 2026-09-08: sender
                                     AND recipient need verification, or a production-access
                                     request). Sent via SES `send_email` (boto3), region from
                                     AWS_REGION/us-east-1.

HONESTY NOTE on the Teams payload shape: Microsoft has been retiring the classic Office 365
Connector (legacy `MessageCard` JSON) in favor of Workflows (Power Automate) webhooks that expect
an Adaptive Card wrapped in a `message` attachment. This module targets that CURRENT shape — but
it has NOT been verified against a real webhook (none was available to test against, unlike the
Tavily REST contract which WAS verified live). Treat the Teams path as unverified until a real
webhook URL is supplied and a live send is confirmed; Slack (stable, well-documented Block Kit)
and SES (standard boto3 API) are lower-risk.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

_SECRET_ID = "signalscompetitor/onca/api-key"
_TOKEN_CACHE: dict[str, str | None] = {}


def _config(env_key: str) -> str | None:
    """env_key from os.environ, else the api-key secret (JSON). Cached; None if absent."""
    if env_key in _TOKEN_CACHE:
        return _TOKEN_CACHE[env_key]
    val = os.environ.get(env_key)
    if not val:
        try:
            import boto3
            raw = boto3.client("secretsmanager").get_secret_value(SecretId=_SECRET_ID)["SecretString"]
            val = (json.loads(raw) or {}).get(env_key)
        except Exception as exc:  # pragma: no cover - secret unavailable locally
            print(f"Warning: {env_key} unavailable: {exc}")
            val = None
    _TOKEN_CACHE[env_key] = val
    return val


_METRIC_LABEL = {"moves": "Movimentos", "entrants": "Entrantes",
                 "regulatory": "Regulatório", "alerts": "Alertas"}


def _metric_line(m: dict[str, Any]) -> str:
    parts = []
    for key, label in _METRIC_LABEL.items():
        d = (m.get(key) or {})
        now, delta = d.get("now", 0), d.get("delta", 0)
        arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "■")
        parts.append(f"{label}: {now} ({arrow}{abs(delta)})" if delta else f"{label}: {now}")
    return " · ".join(parts)


def _priority_lines(top: list[dict[str, Any]]) -> list[str]:
    out = []
    for p in top[:3]:
        dec = p.get("decision") or {}
        out.append(f"[{p.get('so_what') or '—'}] {p.get('entity_label') or '—'} — "
                    f"{dec.get('text') or (p.get('title') or '')[:100]}")
    return out


def weekly_scope(feed: dict[str, Any], sector: str = "__all__") -> dict[str, Any] | None:
    """Pull the CSO weekly scope from an already-built feed. None if absent (e.g. build failed)."""
    wk = (((feed.get("executive") or {}).get("cso") or {}).get("weekly") or {}).get("by_industry") or {}
    return wk.get(sector) or wk.get("__all__")


# --- Teams (Adaptive Card via Workflows webhook) --------------------------------------
def format_teams(w: dict[str, Any], *, dashboard_url: str | None = None) -> dict[str, Any]:
    facts = [{"title": label, "value": str((w.get("metrics") or {}).get(key, {}).get("now", "—"))}
             for key, label in _METRIC_LABEL.items()]
    items: list[dict[str, Any]] = [
        {"type": "TextBlock", "text": "Briefing semanal do CSO", "weight": "Bolder", "size": "Medium"},
        {"type": "TextBlock", "text": w.get("headline") or "", "wrap": True},
        {"type": "FactSet", "facts": facts},
    ]
    lines = _priority_lines(w.get("top_priorities") or [])
    if lines:
        items.append({"type": "TextBlock", "text": "Top prioridades", "weight": "Bolder", "spacing": "Medium"})
        items.append({"type": "TextBlock", "text": "\n".join(f"• {l}" for l in lines), "wrap": True})
    card: dict[str, Any] = {
        "type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4", "body": items,
    }
    if dashboard_url:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Abrir painel", "url": dashboard_url}]
    return {"type": "message", "attachments": [
        {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}


# --- Slack (Block Kit) -----------------------------------------------------------------
def format_slack(w: dict[str, Any], *, dashboard_url: str | None = None) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Briefing semanal do CSO"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": w.get("headline") or ""}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": _metric_line(w.get("metrics") or {})}]},
    ]
    lines = _priority_lines(w.get("top_priorities") or [])
    if lines:
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": "*Top prioridades*\n" + "\n".join(f"• {l}" for l in lines)}})
    if dashboard_url:
        blocks.append({"type": "actions", "elements": [{"type": "button",
                       "text": {"type": "plain_text", "text": "Abrir painel"}, "url": dashboard_url}]})
    return {"blocks": blocks}


# --- Email (plain + a lightly-styled HTML body) -----------------------------------------
def format_email(w: dict[str, Any], *, dashboard_url: str | None = None) -> tuple[str, str, str]:
    """Returns (subject, text_body, html_body)."""
    subject = "Briefing semanal do CSO — Onça"
    lines = _priority_lines(w.get("top_priorities") or [])
    text = (w.get("headline") or "") + "\n\n" + _metric_line(w.get("metrics") or {})
    if lines:
        text += "\n\nTop prioridades:\n" + "\n".join(f"- {l}" for l in lines)
    if dashboard_url:
        text += f"\n\nAbrir painel: {dashboard_url}"
    html_lines = "".join(f"<li>{_esc(l)}</li>" for l in lines)
    html = (f"<h2>Briefing semanal do CSO</h2><p>{_esc(w.get('headline') or '')}</p>"
            f"<p style='color:#666'>{_esc(_metric_line(w.get('metrics') or {}))}</p>"
            + (f"<h3>Top prioridades</h3><ul>{html_lines}</ul>" if lines else "")
            + (f"<p><a href='{_esc(dashboard_url)}'>Abrir painel</a></p>" if dashboard_url else ""))
    return subject, text, html


def _esc(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# --- senders (best-effort; each returns True/False/None — None means "not configured") ------
Poster = Callable[[str, dict[str, Any]], Any]


def _default_post(url: str, payload: dict[str, Any]) -> Any:
    import requests
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    return resp


def send_teams(w: dict[str, Any], *, webhook_url: str | None = None,
              dashboard_url: str | None = None, poster: Poster | None = None) -> bool | None:
    url = webhook_url or _config("ONCA_TEAMS_WEBHOOK_URL")
    if not url:
        return None
    try:
        (poster or _default_post)(url, format_teams(w, dashboard_url=dashboard_url))
        return True
    except Exception as exc:  # pragma: no cover - network best-effort
        print(f"Warning: Teams digest send failed: {exc}")
        return False


def send_slack(w: dict[str, Any], *, webhook_url: str | None = None,
              dashboard_url: str | None = None, poster: Poster | None = None) -> bool | None:
    url = webhook_url or _config("ONCA_SLACK_WEBHOOK_URL")
    if not url:
        return None
    try:
        (poster or _default_post)(url, format_slack(w, dashboard_url=dashboard_url))
        return True
    except Exception as exc:  # pragma: no cover - network best-effort
        print(f"Warning: Slack digest send failed: {exc}")
        return False


EmailSender = Callable[[str, str, str, str, str], Any]


def _default_send_email(sender: str, to: str, subject: str, text: str, html: str) -> Any:
    import boto3
    client = boto3.client("ses", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return client.send_email(
        Source=sender, Destination={"ToAddresses": [to]},
        Message={"Subject": {"Data": subject, "Charset": "UTF-8"},
                 "Body": {"Text": {"Data": text, "Charset": "UTF-8"},
                          "Html": {"Data": html, "Charset": "UTF-8"}}})


def send_email(w: dict[str, Any], *, sender: str | None = None, to: str | None = None,
              dashboard_url: str | None = None, sender_fn: EmailSender | None = None) -> bool | None:
    sender = sender or _config("ONCA_ALERT_EMAIL_FROM")
    to = to or _config("ONCA_ALERT_EMAIL_TO")
    if not sender or not to:
        return None
    subject, text, html = format_email(w, dashboard_url=dashboard_url)
    try:
        (sender_fn or _default_send_email)(sender, to, subject, text, html)
        return True
    except Exception as exc:  # pragma: no cover - network/SES best-effort
        print(f"Warning: email digest send failed: {exc}")
        return False


def send_weekly_digest(feed: dict[str, Any], *, sector: str = "__all__",
                       dashboard_url: str | None = None,
                       teams_poster: Poster | None = None, slack_poster: Poster | None = None,
                       email_sender: EmailSender | None = None) -> dict[str, bool | None]:
    """Send the weekly CSO brief to every CONFIGURED channel. Each channel is independent —
    one failing/missing never blocks another. Returns {channel: True|False|None} (None =
    not configured). Fabricates nothing: if `feed.executive.cso.weekly` is absent, sends nothing."""
    w = weekly_scope(feed, sector)
    if not w or not w.get("headline"):
        return {"teams": None, "slack": None, "email": None}
    return {
        "teams": send_teams(w, dashboard_url=dashboard_url, poster=teams_poster),
        "slack": send_slack(w, dashboard_url=dashboard_url, poster=slack_poster),
        "email": send_email(w, dashboard_url=dashboard_url, sender_fn=email_sender),
    }
