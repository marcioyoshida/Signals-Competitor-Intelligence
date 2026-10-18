"""Client defect / suggestion reports from the Ask chat ("Reportar"), triaged by the operator.

Why not straight to GitHub: the Onça repo is PUBLIC. A client's report carries their tenant, the
entities they watch and the strategic question they asked — publishing that leaks client interest.
So the report lands HERE (private, DynamoDB `OncaFeedback`), the operator triages it in /v2/admin
("Relatos"), and promotes it to a ticket written WITHOUT client data (`ticket_draft`).

Retention (owner decision 2026-10-18): 90 days, enforced by the table TTL (`ttl`) — and see the
privacy policy (docs/privacy.html). The owner notification e-mail is content-free: kind + count +
admin link, never the client's text, so nothing outlives the 90 days in a mailbox.

Fleet pattern: docs/patterns/in-chat-feedback.md.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import time
import uuid
from typing import Any

RETENTION_DAYS = 90
KINDS = {"defeito": "Defeito", "sugestao": "Sugestão"}
STATUSES = ("novo", "ticket", "descartado")
MAX_TEXT = 1000
MAX_Q = 500
MAX_A = 1200
MAX_CITED = 12
DAILY_CAP = int(os.environ.get("ONCA_FEEDBACK_DAILY_CAP", "10"))
ADMIN_URL = "https://onssa.org/v2/admin/#relatos"
_ID = re.compile(r"^[a-f0-9]{16}$")


def _table(table: Any | None = None) -> Any:
    if table is not None:
        return table
    name = os.environ.get("ONCA_FEEDBACK_TABLE")
    if not name:
        raise RuntimeError("ONCA_FEEDBACK_TABLE not configured")
    import boto3

    return boto3.resource("dynamodb").Table(name)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _clip(v: Any, n: int) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _turn(raw: Any) -> dict[str, Any] | None:
    """The chat turn the report is about — the same sanitised shape the chat sends as history."""
    if not isinstance(raw, dict):
        return None
    q, a = _clip(raw.get("q"), MAX_Q), _clip(raw.get("a"), MAX_A)
    if not q and not a:
        return None
    cited = [str(c)[:120] for c in (raw.get("cited") or []) if isinstance(c, (str, int))][:MAX_CITED]
    return {"q": q, "a": a, "cited": cited}


def _take_quota(sub: str, table: Any) -> bool:
    """One atomic counter per user per UTC day (same idea as OncaAskQuota). Fails open."""
    day = _now().strftime("%Y-%m-%d")
    try:
        r = table.update_item(
            Key={"pk": f"FBQ#{sub}#{day}"},
            UpdateExpression="ADD n :one SET #t = if_not_exists(#t, :ttl)",
            ExpressionAttributeNames={"#t": "ttl"},
            ExpressionAttributeValues={":one": 1, ":ttl": int(time.time()) + 3 * 86400},
            ReturnValues="UPDATED_NEW")
        return int(r["Attributes"]["n"]) <= DAILY_CAP
    except Exception as exc:  # pragma: no cover - never blocks a report on the counter
        print(f"Warning: feedback quota skipped: {exc}")
        return True


def record_report(*, kind: str, text: str, sub: str, tenant: str | None, turn: Any = None,
                  context: Any = None, table: Any | None = None) -> dict[str, Any]:
    """Store one report. Returns {"ok": True, "report_id"} or {"ok": False, "reason"}."""
    kind = str(kind or "").strip().lower()
    if kind not in KINDS:
        return {"ok": False, "reason": "kind"}
    text = _clip(text, MAX_TEXT)
    if len(text) < 3:
        return {"ok": False, "reason": "text"}
    if not sub:
        return {"ok": False, "reason": "identity"}
    t = _table(table)
    if not _take_quota(sub, t):
        return {"ok": False, "reason": "daily-limit", "limit": DAILY_CAP}
    ctx = context if isinstance(context, dict) else {}
    rid = uuid.uuid4().hex[:16]
    now = _now()
    item = {
        "pk": f"FB#{rid}", "type": "report", "report_id": rid, "kind": kind, "text": text,
        "status": "novo", "sub": sub, "tenant": tenant or None,
        "created_at": now.isoformat(timespec="seconds"),
        "ttl": int(now.timestamp()) + RETENTION_DAYS * 86400,
        "context": {k: _clip(ctx.get(k), 80) for k in ("officer", "sector", "view", "feed_as_of", "surface")
                    if ctx.get(k)},
    }
    tr = _turn(turn)
    if tr:
        item["turn"] = tr
    t.put_item(Item={k: v for k, v in item.items() if v is not None})
    return {"ok": True, "report_id": rid}


def list_reports(status: str | None = None, table: Any | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Operator triage list, newest first. Small volume → a filtered scan is fine."""
    t = _table(table)
    items: list[dict[str, Any]] = []
    kw: dict[str, Any] = {}
    while True:
        r = t.scan(**kw)
        items += [i for i in r.get("Items") or [] if i.get("type") == "report"]
        if not r.get("LastEvaluatedKey"):
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    if status:
        items = [i for i in items if i.get("status") == status]
    items.sort(key=lambda i: str(i.get("created_at") or ""), reverse=True)
    return [_public(i) for i in items[:limit]]


def _public(i: dict[str, Any]) -> dict[str, Any]:
    out = {k: i.get(k) for k in ("report_id", "kind", "text", "status", "tenant", "created_at",
                                 "context", "turn", "ticket_url", "triaged_at")}
    out["sub"] = str(i.get("sub") or "")[:8]  # enough to group one user's reports, not to identify
    out["ticket_draft"] = ticket_draft(i)
    return out


def set_status(report_id: str, status: str, *, ticket_url: str | None = None,
               table: Any | None = None) -> bool:
    if not _ID.match(str(report_id or "")) or status not in STATUSES:
        return False
    url = str(ticket_url or "").strip()
    if url and not url.startswith("https://github.com/"):
        url = ""
    expr, vals, names = "SET #s = :s, triaged_at = :t", {":s": status, ":t": _now().isoformat(timespec="seconds")}, {"#s": "status"}
    if url:
        expr += ", ticket_url = :u"
        vals[":u"] = url
    try:
        _table(table).update_item(Key={"pk": f"FB#{report_id}"}, UpdateExpression=expr,
                                  ConditionExpression="attribute_exists(pk)",
                                  ExpressionAttributeNames=names, ExpressionAttributeValues=vals)
        return True
    except Exception as exc:
        print(f"Warning: feedback set_status failed for {report_id}: {type(exc).__name__}")
        return False


def delete_for_user(sub: str, table: Any | None = None) -> int:
    """Account deletion (#251): remove every report a user filed (and their daily-cap counters,
    ``FBQ#<sub>#<day>``). Returns how many reports."""
    t = _table(table)
    n = 0
    for i in _scan_all(t):
        if i.get("type") == "report" and i.get("sub") == sub:
            t.delete_item(Key={"pk": i["pk"]})
            n += 1
        elif str(i.get("pk") or "").startswith(f"FBQ#{sub}#"):
            t.delete_item(Key={"pk": i["pk"]})
    return n


def _scan_all(t: Any) -> list[dict[str, Any]]:
    out, kw = [], {}
    while True:
        r = t.scan(**kw)
        out += r.get("Items") or []
        if not r.get("LastEvaluatedKey"):
            return out
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def ticket_draft(i: dict[str, Any]) -> dict[str, str]:
    """A starting point for the PUBLIC ticket: no tenant, no user, no question/answer text, no
    card ids (they name the entities the client watches). The operator writes the defect itself."""
    ctx = i.get("context") or {}
    kind = KINDS.get(str(i.get("kind")), "Relato")
    where = " · ".join(x for x in (ctx.get("surface") or "chat", ctx.get("officer"), ctx.get("view")) if x)
    body = (f"**Origem:** relato de cliente ({kind.lower()}), {str(i.get('created_at') or '')[:10]}, {where}.\n"
            f"**Feed:** {ctx.get('feed_as_of') or 'n/d'}\n\n"
            "**O que acontece:** <descreva sem dados do cliente: sem tenant, pergunta, entidades ou ids>\n\n"
            "**Esperado:** <…>\n\n_Detalhes do relato ficam no painel de triagem (privado, 90 dias)._")
    return {"title": f"Relato de cliente ({kind.lower()}): <resumo>", "body": body}


def notify_owner(kind: str, *, ses: Any | None = None, ssm: Any | None = None) -> bool:
    """Content-free heads-up to the owner (the contact-form recipient). Best-effort."""
    try:
        from src.dashboard.contact_forward import FROM_ADDRESS, _client, _forward_to

        to = _forward_to(ssm)
        if not to:
            return False
        label = KINDS.get(kind, "Relato")
        _client(ses).send_email(
            Source=FROM_ADDRESS, Destination={"ToAddresses": [to]},
            Message={"Subject": {"Data": f"Onça · novo relato de cliente ({label.lower()})", "Charset": "UTF-8"},
                     "Body": {"Text": {"Charset": "UTF-8", "Data": (
                         f"Um cliente enviou um relato ({label.lower()}) pelo chat da Onça.\n\n"
                         f"Triagem: {ADMIN_URL}\n\n"
                         "O texto do relato fica só no painel (privado, retido por 90 dias).")}}})
        return True
    except Exception as exc:  # pragma: no cover - notification never blocks the report
        print(f"Warning: feedback notify skipped: {type(exc).__name__}")
        return False
