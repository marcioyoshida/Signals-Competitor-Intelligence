"""Entity-tagged corporate-distress store (Recuperação Judicial / falência).

**Decision (2026-08-25, "option A"):** compile an entity-level RJ/distress dataset
by *mining a stream we already ingest* — the trade-press news — and **persisting**
matched events to a durable store, rather than standing up a new named-process
ingestion.

Rationale (from an audit of the raw corpus): the CNJ **DataJud** source is
party-name-scrubbed (only aggregate `macro.distress` counts survive — see
`src/ingest/datajud.py`), raw **DOU** is organ-filtered (no judicial section), and
**news** — where "<empresa> pede recuperação judicial" headlines live *with the
company name* — is ephemeral (digest-only, overwritten each run). So the richest
entity-tied RJ evidence was being thrown away. This module classifies distress in
the news slice, resolves it to registry entities, and folds it into a durable
`distress/index.json` keyed by (entity, kind) with first/last-seen — turning a
one-shot headline into a persisted status. DataJud stays the anonymized macro
*trend* behind it.

Precision-first (same discipline as the news corroboration floor): a title must
match a distress phrase AND resolve to a tracked entity via `resolve_entities`
(anchored, ambiguity-gated) — a bare mention never creates a distress record.

Pure core (`classify_distress`, `detect_distress_events`, `merge_distress`) is unit
tested; S3 I/O is thin adapters.
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from typing import Any, Callable, Iterable

INDEX_KEY = "distress/index.json"

# A distressed status is durable (a company stays in RJ for years); keep records
# long and only drop when nothing has referenced them for this window.
DEFAULT_TTL_DAYS = 720


def _norm(text: Any) -> str:
    t = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


# kind -> label + accent-folded title patterns. Order matters: extrajudicial is
# checked before the plain "recuperacao judicial" so the more specific wins.
DISTRESS_KINDS: dict[str, dict[str, Any]] = {
    "recuperacao_extrajudicial": {
        "label": "Recuperação Extrajudicial",
        "patterns": [r"recuperacao extrajudicial"],
    },
    "recuperacao_judicial": {
        "label": "Recuperação Judicial",
        "patterns": [
            r"recuperacao judicial",
            r"pedido de recuperacao",
            r"pede recuperacao",
            r"em recuperacao judicial",
            r"\brj\b(?=.*credor)",  # "RJ" only when clearly the process (with credor)
        ],
    },
    "falencia": {
        "label": "Falência",
        "patterns": [
            r"falencia",
            r"decretacao de falencia",
            r"pedido de falencia",
            r"massa falida",
        ],
    },
}

_COMPILED = {
    kind: [re.compile(p) for p in spec["patterns"]]
    for kind, spec in DISTRESS_KINDS.items()
}


def classify_distress(title: str) -> str | None:
    """Return the distress kind a headline signals, or None. Most-specific first."""
    t = _norm(title)
    if not t:
        return None
    for kind in ("recuperacao_extrajudicial", "recuperacao_judicial", "falencia"):
        for rx in _COMPILED[kind]:
            if rx.search(t):
                return kind
    return None


def label_for(kind: str) -> str:
    return DISTRESS_KINDS.get(kind, {}).get("label", kind)


def detect_distress_events(
    news_items: Iterable[dict[str, Any]],
    *,
    resolver: Callable[[dict[str, Any]], list[str]],
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Scan news items for distress headlines that resolve to a tracked entity.

    Returns one event per (entity, item): a title must both classify as distress
    AND resolve to at least one registry entity — a bare mention never qualifies.
    """
    today = today or dt.date.today()
    out: list[dict[str, Any]] = []
    for item in news_items or []:
        title = item.get("title") or item.get("subject") or ""
        kind = classify_distress(title)
        if not kind:
            continue
        try:
            entities = resolver(item) or []
        except Exception:  # pragma: no cover - resolver best-effort
            entities = []
        if not entities:
            continue
        date = str(item.get("date") or today.isoformat())[:10]
        for eid in entities:
            out.append({
                "entity": eid,
                "kind": kind,
                "label": label_for(kind),
                "date": date,
                "title": title.strip(),
                "url": item.get("url"),
                "source": item.get("source") or "News",
                "evidence_id": item.get("id"),
            })
    return out


def merge_distress(
    existing: dict[str, Any] | None,
    events: list[dict[str, Any]],
    *,
    today: dt.date | None = None,
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> dict[str, Any]:
    """Upsert distress events into the index, keyed by (entity, kind).

    Keeps first_seen/last_seen, latest title/url, an evidence sample and a mention
    count; prunes records untouched for `ttl_days`. Escalation to `falencia` is
    tracked as its own record (a company can appear under both).
    """
    today = today or dt.date.today()
    idx = dict(existing or {})
    records: dict[str, dict[str, Any]] = dict(idx.get("records") or {})

    for ev in events:
        key = f"{ev['entity']}#{ev['kind']}"
        date = ev["date"]
        rec = records.get(key)
        if rec is None:
            records[key] = {
                "entity": ev["entity"],
                "kind": ev["kind"],
                "label": ev["label"],
                "first_seen": date,
                "last_seen": date,
                "latest_title": ev["title"],
                "latest_url": ev.get("url"),
                "mentions": 1,
                "evidence": [e for e in [ev.get("evidence_id")] if e][:5],
            }
        else:
            rec["mentions"] = int(rec.get("mentions", 0)) + 1
            if date < rec.get("first_seen", date):
                rec["first_seen"] = date
            if date >= rec.get("last_seen", ""):
                rec["last_seen"] = date
                rec["latest_title"] = ev["title"]
                rec["latest_url"] = ev.get("url")
                rec["label"] = ev["label"]
            ev_id = ev.get("evidence_id")
            if ev_id and ev_id not in (rec.get("evidence") or []):
                rec["evidence"] = ((rec.get("evidence") or []) + [ev_id])[-5:]

    # Prune stale records (nothing referenced them within the TTL window).
    cutoff = (today - dt.timedelta(days=ttl_days)).isoformat()
    pruned = {
        k: r for k, r in records.items()
        if str(r.get("last_seen") or "") >= cutoff
    }

    return {
        "as_of": today.isoformat(),
        "count": len(pruned),
        "records": pruned,
    }


# --- entity-facing accessor ----------------------------------------------

def entity_status(index: dict[str, Any], entity_id: str) -> dict[str, Any] | None:
    """The most severe active distress record for an entity, or None.

    Severity: falência > recuperação judicial > extrajudicial.
    """
    rank = {"falencia": 3, "recuperacao_judicial": 2, "recuperacao_extrajudicial": 1}
    best: dict[str, Any] | None = None
    for rec in (index.get("records") or {}).values():
        if rec.get("entity") != entity_id:
            continue
        if best is None or rank.get(rec.get("kind"), 0) > rank.get(best.get("kind"), 0):
            best = rec
    return best


def list_records(index: dict[str, Any]) -> list[dict[str, Any]]:
    """Flat, sorted (most-recent first) list of distress records for the feed."""
    recs = list((index.get("records") or {}).values())
    recs.sort(key=lambda r: str(r.get("last_seen") or ""), reverse=True)
    return recs


# --- S3 adapters + orchestrator ------------------------------------------

def load_index(bucket: str, *, s3: Any | None = None) -> dict[str, Any]:
    import json
    import boto3
    s3 = s3 or boto3.client("s3")
    try:
        body = s3.get_object(Bucket=bucket, Key=INDEX_KEY)["Body"].read()
        data = json.loads(body)
        return data if isinstance(data, dict) else {}
    except Exception:  # pragma: no cover - first run / missing object
        return {}


def publish(index: dict[str, Any], bucket: str, *, s3: Any | None = None) -> str:
    import json
    import boto3
    s3 = s3 or boto3.client("s3")
    s3.put_object(
        Bucket=bucket,
        Key=INDEX_KEY,
        Body=json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{bucket}/{INDEX_KEY}"


def update_from_news(
    news_items: Iterable[dict[str, Any]],
    bucket: str,
    *,
    resolver: Callable[[dict[str, Any]], list[str]] | None = None,
    s3: Any | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """End-to-end: detect distress in the news slice, merge into the durable
    index, persist. Returns a small summary. Best-effort by the caller."""
    if resolver is None:
        from src.synth.entities import resolve_entities as resolver  # lazy
    events = detect_distress_events(news_items, resolver=resolver, today=today)
    index = load_index(bucket, s3=s3)
    merged = merge_distress(index, events, today=today)
    publish(merged, bucket, s3=s3)
    return {"new_events": len(events), "records": merged.get("count", 0)}
