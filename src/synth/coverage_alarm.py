"""Coverage alarm (#178, part of incident #173) — a per-industry news-volume spike with no
matching sector event is an operator alert: "possível evento setorial não capturado".

On 2026-09-25/26 the betting ban was all over the press, but Onça's queries are entity-bound,
so the only trace in its own ingest was a handful of ban headlines attached to B3 / Caixa /
Serasa. Nothing asked "why is the betting sector suddenly in the news?". This is that question,
as a safety net *independent of* the #177 detector: if the detector (or a producer) misses an
event, the alarm still fires; once the event exists, the alarm stays silent.

**Series.** Per industry, per publication date, the distinct news item ids attributed to it —
(a) entity-query items mapped through the registry (term → entity → industries), (b) sector-query
items (#176, ``query_kind: "sector"``, ``industries``), and (c) items whose headline names the
sector (``sector_events.INDUSTRY_TERMS``). Two metrics per day:

- ``volume``  — distinct items;
- ``reg``     — distinct OUTLETS carrying a qualifying sector-change headline
  (``sector_events.assess_headline`` verdict ``report`` — polls/protests/hypotheticals/operator
  actions excluded).

**Test.** Over a 2-day window ending on the evaluated date, against the same 2-day sums over the
prior 30 days: ``z = (x − mean) / max(std, √mean, floor)`` (floor 1 for volume, 0.5 for outlets). Alarm when (z ≥ 3 and volume ≥
``min_volume``) or (z ≥ 3 and reg outlets ≥ ``min_reg``), with ≥ ``min_history`` baseline days,
and no sector event for that industry in the window. The ``reg`` metric is what catches 09-25/26:
betting's raw item volume actually FELL that week (operator promo items dominate it), but three
outlets reporting a ban against a zero baseline is a 3σ event.

Delivery: the existing operator path — the ``OncaAlertsTopic`` SNS topic → ``alert_notifier`` →
the weekly-digest channels. The message is shaped like a CloudWatch alarm so the notifier needs no
change. Gated by ``ONCA_COVERAGE_ALARM_NOTIFY`` (default off) and ``ONCA_ALERTS_TOPIC_ARN``:
unset ⇒ the alarm is computed and persisted, nothing is sent.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
from typing import Any, Callable, Iterable

from src.synth import sector_events as se

STORE_KEY = "sector_events/news_volume.json"
NEWS_PREFIX = "lambda-digests/news/"

BASELINE_DAYS = 30
WINDOW_DAYS = 2
Z_THRESHOLD = 3.0
MIN_VOLUME = 12
MIN_REG_OUTLETS = 2
MIN_HISTORY_DAYS = 14
KEEP_DAYS = 45
REALERT_DAYS = 2
EVENT_MATCH_DAYS = 7
#: The volume trigger needs BREADTH: no single entity may account for more than this share.
MAX_TOP_SHARE = 0.4
#: ... and at least this many distinct entities / sector-attributed items in the window.
MIN_GROUPS = 4


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


# --- attribution ---------------------------------------------------------------------------
def term_industry_map(entities: Iterable[dict[str, Any]]) -> dict[str, set[str]]:
    """{folded news term / label / id: industries} for ACTIVE registry entities — the key the
    news ingester stamps on each item (``company``) is the entity's news query term."""
    try:
        from src.synth.entity_registry import news_query_term
    except Exception:  # pragma: no cover - registry module unavailable
        news_query_term = None  # type: ignore[assignment]
    out: dict[str, set[str]] = {}
    for e in entities or []:
        if not isinstance(e, dict) or e.get("active", True) is False:
            continue
        inds = {str(i) for i in (e.get("industries") or [])}
        if not inds:
            continue
        eid = e.get("entity_id") or e.get("entity") or ""
        name = e.get("display_name") or e.get("label") or eid
        keys = {eid, name, re.sub(r"\(.*?\)", "", str(name).split("/")[0]).strip()}
        if news_query_term is not None:
            keys.add(news_query_term(eid, name, stored=e.get("news_term")))
        for k in keys:
            fk = se._fold(k).strip()
            if fk:
                out.setdefault(fk, set()).update(inds)
    return out


def item_industries(item: dict[str, Any], term_map: dict[str, set[str]]) -> set[str]:
    inds: set[str] = {str(i) for i in (item.get("industries") or [])}  # #176 tags (any query_kind)
    inds |= term_map.get(se._fold(item.get("company") or item.get("name")).strip(), set())
    inds |= set(se.industries_in_text(item.get("title")))
    return inds


# --- series store ----------------------------------------------------------------------------
def add_items(store: dict[str, Any], items: Iterable[dict[str, Any]],
              term_map: dict[str, set[str]], *, covered: set[str] | None = None) -> dict[str, Any]:
    """Fold news items into ``store["series"][industry][date] = {"ids": {id: group}, "reg": {id: outlet}}``.
    Idempotent on item id — overlapping digests (items + context re-sent across runs) never double
    count."""
    series = store.setdefault("series", {})
    for it in items or []:
        iid, date = it.get("id"), str(it.get("date") or "")[:10]
        if not iid or not se._d(date):
            continue
        inds = item_industries(it, term_map)
        if covered is not None:
            inds &= covered
        if not inds:
            continue
        tagged = it.get("industries") or None
        a = se.assess_headline(it.get("title"), industries=tagged,
                               company=None if it.get("query_kind") == "sector" else it.get("company"))
        # Group key for the breadth check: the entity the item was fetched for — unless the
        # item is attributed by SECTOR (tag or headline), which is breadth by definition.
        by_sector = bool(tagged) or bool(set(se.industries_in_text(it.get("title"))) & inds)
        group = f"sector:{iid}" if by_sector else (se._fold(it.get("company") or it.get("name")).strip() or iid)
        for ind in inds:
            day = series.setdefault(ind, {}).setdefault(date, {"ids": {}, "reg": {}})
            day["ids"].setdefault(iid, group)
            if a["verdict"] == "report" and ind in a["industries"]:
                day["reg"][iid] = se.publisher_key(it)
    return store


def prune(store: dict[str, Any], today: dt.date, keep_days: int = KEEP_DAYS) -> dict[str, Any]:
    cutoff = (today - dt.timedelta(days=keep_days)).isoformat()
    for ind, days in list((store.get("series") or {}).items()):
        store["series"][ind] = {d: v for d, v in days.items() if d >= cutoff}
    return store


def _window(days: dict[str, Any], end: dt.date, width: int) -> tuple[int, int, dict[str, str]]:
    """(distinct items, distinct reg-mention outlets, {id: group}) over ``width`` days to ``end``."""
    ids: dict[str, str] = {}
    outlets: set[str] = set()
    for k in range(width):
        row = days.get((end - dt.timedelta(days=k)).isoformat()) or {}
        ids.update(row.get("ids") or {})
        outlets |= set((row.get("reg") or {}).values())
    return len(ids), len(outlets), ids


def top_group_share(groups: dict[str, str]) -> float:
    """Share of the window's items that belong to its single largest entity. A burst driven by
    ONE company (a Monashees fund mandate syndicated 20×) is that company's news — its entity
    narrative covers it — not a sector event."""
    if not groups:
        return 0.0
    counts: dict[str, int] = {}
    for g in groups.values():
        counts[g] = counts.get(g, 0) + 1
    return max(counts.values()) / len(groups)


def _z(x: float, base: list[float], *, floor: float = 1.0) -> tuple[float, float, float]:
    """z-score against the baseline, with the sd floored at max(√mean, ``floor``) so a
    near-empty baseline (the usual state of a quiet sector) can't turn 1 item into 10σ."""
    if not base:
        return 0.0, 0.0, 0.0
    mean = sum(base) / len(base)
    var = sum((b - mean) ** 2 for b in base) / len(base)
    sd = max(math.sqrt(var), math.sqrt(mean), floor)
    return (x - mean) / sd, mean, math.sqrt(var)


def history_start(store: dict[str, Any]) -> dt.date | None:
    """First date the series covers: the explicit backfill/first-run marker when present
    (an industry with zero items on a day is a real zero only inside the covered span)."""
    d = se._d(store.get("covered_since"))
    if d:
        return d
    dates = [x for days in (store.get("series") or {}).values() for x in days]
    return se._d(min(dates)) if dates else None


def _matching_event(events: Iterable[dict[str, Any]], industry: str, end: dt.date) -> dict[str, Any] | None:
    lo = end - dt.timedelta(days=EVENT_MATCH_DAYS)
    for e in events or []:
        if e.get("industry") != industry:
            continue
        dates = [se._d(e.get("date")), se._d(e.get("last_evidence"))]
        if any(x and lo <= x <= end + dt.timedelta(days=1) for x in dates):
            return e
    return None


def evaluate(
    store: dict[str, Any],
    *,
    as_of: dt.date,
    events: Iterable[dict[str, Any]] | None = None,
    items_index: dict[str, dict[str, Any]] | None = None,
    z_threshold: float | None = None,
    min_volume: int | None = None,
    min_reg: int | None = None,
    min_history: int = MIN_HISTORY_DAYS,
) -> list[dict[str, Any]]:
    """Every industry whose 2-day window ending ``as_of`` is a spike. Each row says whether a
    sector event already covers it (``suppressed``) — only unsuppressed rows are alerts."""
    z_threshold = z_threshold if z_threshold is not None else _env_float("ONCA_COVERAGE_ALARM_Z", Z_THRESHOLD)
    min_volume = int(min_volume if min_volume is not None else _env_float("ONCA_COVERAGE_ALARM_MIN_VOLUME", MIN_VOLUME))
    min_reg = int(min_reg if min_reg is not None else _env_float("ONCA_COVERAGE_ALARM_MIN_REG", MIN_REG_OUTLETS))
    start = history_start(store)
    out: list[dict[str, Any]] = []
    for ind, days in sorted((store.get("series") or {}).items()):
        vol, reg, ids = _window(days, as_of, WINDOW_DAYS)
        base_v: list[float] = []
        base_r: list[float] = []
        for k in range(WINDOW_DAYS, WINDOW_DAYS + BASELINE_DAYS):
            end = as_of - dt.timedelta(days=k)
            if start and end - dt.timedelta(days=WINDOW_DAYS - 1) < start:
                break
            v, r, _ = _window(days, end, WINDOW_DAYS)
            base_v.append(v)
            base_r.append(r)
        if len(base_v) < min_history:
            continue
        zv, mv, sv = _z(vol, base_v)
        zr, mr, sr = _z(reg, base_r, floor=0.5)  # outlets: small counts, min_reg gates the floor
        triggers = []
        share = top_group_share(ids)
        n_groups = len(set(ids.values()))
        if zv >= z_threshold and vol >= min_volume and share <= MAX_TOP_SHARE and n_groups >= MIN_GROUPS:
            triggers.append("volume")
        if zr >= z_threshold and reg >= min_reg:
            triggers.append("regulatory_mentions")
        if not triggers:
            continue
        ev = _matching_event(events or [], ind, as_of)
        heads = []
        for iid in sorted(ids):
            it = (items_index or {}).get(iid)
            if it:
                heads.append(it)
        reg_ids = {i for k in range(WINDOW_DAYS)
                   for i in ((days.get((as_of - dt.timedelta(days=k)).isoformat()) or {}).get("reg") or {})}
        heads.sort(key=lambda it: (it.get("id") not in reg_ids, str(it.get("date") or "")), reverse=False)
        out.append({
            "industry": ind, "as_of": as_of.isoformat(), "window_days": WINDOW_DAYS,
            "triggers": triggers, "volume": vol, "z_volume": round(zv, 2),
            "baseline_volume_mean": round(mv, 2), "baseline_volume_sd": round(sv, 2),
            "reg_outlets": reg, "z_reg": round(zr, 2), "baseline_reg_mean": round(mr, 2),
            "baseline_days": len(base_v), "top_entity_share": round(share, 2),
            "n_groups": n_groups,
            "suppressed": bool(ev), "matching_event": ev.get("id") if ev else None,
            "top_headlines": [{"title": h.get("title"), "publisher": h.get("publisher"),
                               "date": h.get("date"), "url": h.get("url")} for h in heads[:5]],
        })
    return out


def alert_text(alarm: dict[str, Any]) -> str:
    heads = "; ".join(f"\"{h.get('title')}\" ({h.get('publisher') or '?'}, {h.get('date')})"
                      for h in (alarm.get("top_headlines") or [])[:3])
    why = []
    if "regulatory_mentions" in alarm["triggers"]:
        why.append(f"{alarm['reg_outlets']} veículos com manchete de mudança regulatória "
                   f"(z={alarm['z_reg']}, base {alarm['baseline_reg_mean']}/janela)")
    if "volume" in alarm["triggers"]:
        why.append(f"{alarm['volume']} notícias (z={alarm['z_volume']}, base "
                   f"{alarm['baseline_volume_mean']}/janela)")
    return (f"possível evento setorial não capturado: {alarm['industry']} — "
            f"{'; '.join(why)} em {alarm['window_days']}d até {alarm['as_of']}, sem evento setorial "
            f"correspondente. Principais manchetes: {heads or '—'}")


# --- delivery (operator path) ------------------------------------------------------------------
Publisher = Callable[[str, dict[str, Any]], Any]


def _sns_publish(topic_arn: str, message: dict[str, Any]) -> Any:
    import boto3
    return boto3.client("sns").publish(TopicArn=topic_arn, Subject="Onça coverage alarm",
                                       Message=json.dumps(message, ensure_ascii=False))


def notify(alarms: list[dict[str, Any]], *, store: dict[str, Any], as_of: dt.date,
           topic_arn: str | None = None, publisher: Publisher | None = None,
           enabled: bool | None = None) -> list[dict[str, Any]]:
    """Send each UNSUPPRESSED alarm once per industry per ``REALERT_DAYS`` through the alerts SNS
    topic, as a CloudWatch-alarm-shaped message (``alert_notifier`` renders it unchanged).
    Disabled unless ``ONCA_COVERAGE_ALARM_NOTIFY`` is true AND a topic ARN is configured."""
    if enabled is None:
        enabled = os.environ.get("ONCA_COVERAGE_ALARM_NOTIFY", "false").lower() in ("1", "true", "yes")
    topic_arn = topic_arn or os.environ.get("ONCA_ALERTS_TOPIC_ARN")
    sent: list[dict[str, Any]] = []
    last = store.setdefault("alerted", {})
    for a in alarms:
        if a.get("suppressed"):
            continue
        prev = se._d(last.get(a["industry"]))
        if prev and (as_of - prev).days < REALERT_DAYS:
            continue
        msg = {"AlarmName": f"OncaCoverageAlarm-{a['industry']}", "NewStateValue": "ALARM",
               "NewStateReason": alert_text(a), "Trigger": {"Kind": "coverage_alarm", **{
                   k: a[k] for k in ("industry", "as_of", "triggers", "volume", "reg_outlets")}}}
        if not enabled or not topic_arn:
            sent.append({"industry": a["industry"], "sent": False,
                         "reason": "disabled" if not enabled else "no topic"})
            continue
        try:
            (publisher or _sns_publish)(topic_arn, msg)
            last[a["industry"]] = as_of.isoformat()
            sent.append({"industry": a["industry"], "sent": True})
        except Exception as exc:  # pragma: no cover - best-effort
            print(f"Warning: coverage alarm publish failed: {exc}")
            sent.append({"industry": a["industry"], "sent": False, "reason": str(exc)[:120]})
    return sent


# --- S3 adapters + orchestrator ----------------------------------------------------------------
def load_store(bucket: str, *, s3: Any | None = None) -> dict[str, Any]:
    import boto3
    s3 = s3 or boto3.client("s3")
    try:
        data = json.loads(s3.get_object(Bucket=bucket, Key=STORE_KEY)["Body"].read())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def publish(store: dict[str, Any], bucket: str, *, s3: Any | None = None) -> None:
    import boto3
    s3 = s3 or boto3.client("s3")
    s3.put_object(Bucket=bucket, Key=STORE_KEY,
                  Body=json.dumps(store, ensure_ascii=False).encode("utf-8"),
                  ContentType="application/json")


def history_items(bucket: str, *, days: int, today: dt.date, s3: Any | None = None) -> list[dict[str, Any]]:
    """Read-only backfill from the news digests already in S3 (``lambda-digests/news/``)."""
    import boto3
    s3 = s3 or boto3.client("s3")
    cutoff = dt.datetime.combine(today - dt.timedelta(days=days), dt.time(), tzinfo=dt.timezone.utc)
    keys: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=NEWS_PREFIX):
        for o in page.get("Contents") or []:
            if o["Key"].endswith(".json") and o["LastModified"] >= cutoff:
                keys.append(o["Key"])
    items: list[dict[str, Any]] = []
    for k in sorted(keys):
        try:
            body = json.loads(s3.get_object(Bucket=bucket, Key=k)["Body"].read())
            items += se._section_items(body.get("news"))
        except Exception as exc:  # pragma: no cover - one bad object never blocks the rest
            print(f"Warning: coverage history read {k} failed: {exc}")
    return items


def run(
    digest: dict[str, Any],
    bucket: str,
    *,
    events: list[dict[str, Any]],
    entities: list[dict[str, Any]] | None = None,
    s3: Any | None = None,
    today: dt.date | None = None,
    publisher: Publisher | None = None,
) -> dict[str, Any]:
    """One synth run: fold this run's news into the durable series (backfilling from S3 history
    the first time), evaluate the window ending today and yesterday, alert operators."""
    today = today or dt.date.today()
    tmap = term_industry_map(entities or [])
    covered = {i for s in tmap.values() for i in s} or None
    store = load_store(bucket, s3=s3)
    _, news = se.split_digest(digest)
    index = {it["id"]: it for it in news if it.get("id")}
    if not store.get("series") or not store.get("covered_since"):
        hist = history_items(bucket, days=BASELINE_DAYS + WINDOW_DAYS + 3, today=today, s3=s3)
        add_items(store, hist, tmap, covered=covered)
        store["covered_since"] = (today - dt.timedelta(days=BASELINE_DAYS + WINDOW_DAYS + 3)).isoformat()
        index.update({it["id"]: it for it in hist if it.get("id")})
    add_items(store, news, tmap, covered=covered)
    prune(store, today)
    alarms = evaluate(store, as_of=today, events=events, items_index=index)
    sent = notify(alarms, store=store, as_of=today, publisher=publisher)
    store["last_run"] = {"as_of": today.isoformat(), "alarms": alarms, "sent": sent}
    publish(store, bucket, s3=s3)
    return {"alarms": len(alarms), "unsuppressed": sum(1 for a in alarms if not a["suppressed"]),
            "sent": sum(1 for s in sent if s.get("sent")),
            "industries": [a["industry"] for a in alarms if not a["suppressed"]]}
