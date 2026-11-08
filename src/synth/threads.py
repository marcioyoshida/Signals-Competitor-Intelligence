"""Wave 2 (ADR 003) — the Threaded / incident axis + the shared thread store.

ADR-003 Shift 1: some narratives are not independent daily cards but **developments
of one ongoing story** — an acquisition unfolding, a lawsuit, an investigation, a
funding round. This axis threads those into a **living incident document** keyed by a
stable id (independent of run_date), updated in place as new developments land
(`emit-on-update`), with an explicit lifecycle **open → developing → resolved**
(the ADR's "never prune, but *close*").

Design:
- **Deterministic event identity, no LLM.** A keyword event-type taxonomy assigns each
  ACTIVITY narrative to zero+ incident-worthy event types (M&A, litígio, investigação,
  vazamento, captação, parceria, lançamento, liderança, autorização, expansão,
  reestruturação). A thread is `(primary_entity, event_type)` — the coarse but honest
  v1 answer to the "false-nexus problem"; routine cadence types (earnings) are excluded
  (they are the thematic axis's job). Mis-threading risk is bounded by same-entity +
  same-event-type + a recency window.
- **Recomputed derived state (the thread store).** Rebuilt each run from the durable
  narrative history into `threads/{incident_id}.json` (full, evidence-linked) +
  `threads/index.json` (compact, feed-ready) — no mutable store, same discipline as the
  feature/SWOT stores; the observable result is one living thread whose developments grow.
- **A real arc, not a single card.** Gated to ≥2 developments across ≥2 distinct dates,
  so only genuine stories thread. Grounded — each development cites its source; the
  *grouping* is the labeled inference (`is_inference` / `axis="threaded"`).
- **Lifecycle & scoring.** resolved when no update within CLOSE_DAYS (kept as history,
  scored down); an active hot incident (peak ≥ alert floor) flags as an alert.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import unicodedata
from typing import Any, Iterator

import boto3

from src.synth import feature_store
from src.synth.synthesize import ENTITY_LABELS, run_at_now, run_date_today

AXIS = "threaded"
THREADS_PREFIX = "threads/"
INDEX_KEY = "threads/index.json"

# --- Gate (env-overridable) -------------------------------------------------
MIN_DEVELOPMENTS = 2   # a thread needs >= this many developments ...
MIN_DATES = 2          # ... spread across >= this many distinct dates (a real arc)
CLOSE_DAYS = 14        # no update within this many days -> resolved (kept as history)
DEVELOPING_MIN = 3     # >= this many developments while active -> "developing"
ALERT_PEAK = 0.7       # an active incident peaking here or above flags as an alert
FEED_MAX_AGE = 30      # threads not updated within this many days drop out of the index


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except (TypeError, ValueError):
        return float(default)


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def _days_between(a: str, b: str) -> int | None:
    try:
        return (feature_store._parse(a) - feature_store._parse(b)).days
    except Exception:
        return None


# --- Event-type taxonomy (incident-worthy only) -----------------------------
EVENT_TYPES: dict[str, dict[str, Any]] = {
    "acquisition": {"label": "Aquisição / fusão",
        "patterns": [r"aquisic", r"\bfusao\b", r"incorpora", r"\bm&a\b", r"adquiri",
                     r"compra d[ao]", r"comprou"]},
    "litigation": {"label": "Litígio / processo",
        "patterns": [r"process[oa]", r"acao judicial", r"litigio", r"\bdisputa",
                     r"condena", r"\bmulta\b", r"acordo judicial", r"indeniza"]},
    "investigation": {"label": "Investigação / fiscalização",
        "patterns": [r"investiga", r"policia federal", r"\bopera[çc]ao\b", r"fiscaliza",
                     r"\bapura", r"inquerito", r"suspeit"]},
    "breach": {"label": "Incidente de segurança / vazamento",
        "patterns": [r"vazamento", r"dados vazad", r"invasao", r"\bataque", r"hacker",
                     r"ransomware", r"incidente de seguranca"]},
    # #375: a service failure customers feel. Specific phrasings only — a bare "instabilidade"
    # is usually macro/financial news, not an app or Pix failure.
    "service_outage": {"label": "Instabilidade / falha de serviço",
        "patterns": [r"fora do ar", r"instabilidade (no|do|nos|em|n[ao]s?) ?(app|aplicativo|sistema|pix|servic|plataforma)",
                     r"falhas? (no|do|nos|em|n[ao]) ?(app|aplicativo|sistema|pix|servic|plataforma)",
                     r"\bpane (no|do|nos|em|n[ao])", r"indisponibilidade (do|no|de|da|n[ao]) ?(app|aplicativo|sistema|servic|pix|plataforma)",
                     r"\bapp (caiu|travou|fora)", r"clientes sem acesso"]},
    "funding": {"label": "Captação / oferta",
        "patterns": [r"captac", r"oferta de acoes", r"\bipo\b", r"follow-on", r"\baporte",
                     r"\brodada\b", r"levantou r\$", r"emissao de cotas", r"capta r\$"]},
    "partnership": {"label": "Parceria / acordo",
        "patterns": [r"parceria", r"acordo com", r"joint venture", r"\balianca",
                     r"integra[çc]ao com"]},
    "product_launch": {"label": "Lançamento de produto",
        "patterns": [r"lancament", r"lancou", r"nova plataforma", r"novo produto",
                     r"estreia", r"lanca "]},
    "leadership": {"label": "Mudança de liderança",
        "patterns": [r"novo ceo", r"novo cfo", r"novo presidente", r"nomea",
                     r"assumiu o comando", r"renuncia", r"saida de", r"substitui"]},
    "authorization": {"label": "Autorização / licença",
        "patterns": [r"autoriza", r"\blicenca\b", r"aprovacao do banco central",
                     r"banco multiplo", r"credenciad", r"aprovacao preliminar"]},
    "expansion": {"label": "Expansão / internacionalização",
        "patterns": [r"expansao", r"internacional", r"nos eua", r"novos mercados",
                     r"abertura de", r"expandind"]},
    "restructuring": {"label": "Reestruturação / cortes",
        "patterns": [r"reestrutura", r"demissao", r"corte de", r"layoff",
                     r"fechamento de", r"encerramento de operac"]},
}
_EVENT_PATS = {k: [re.compile(p) for p in v["patterns"]] for k, v in EVENT_TYPES.items()}
_STATUS_PT = {"open": "Em aberto", "developing": "Em desenvolvimento", "resolved": "Encerrado"}


# Taxonomy order = salience priority (earlier = stronger), used to break ties when a
# card matches several event types with the same number of pattern hits.
_EVENT_PRIORITY = {et: i for i, et in enumerate(EVENT_TYPES)}


def event_type_matches(narrative: dict[str, Any]) -> dict[str, int]:
    """{event_type: number of distinct patterns that hit} for the narrative text."""
    text = _norm(narrative.get("narrative") or "")
    if not text:
        return {}
    out = {et: sum(1 for p in pats if p.search(text)) for et, pats in _EVENT_PATS.items()}
    return {et: c for et, c in out.items() if c}


def event_types_of(narrative: dict[str, Any]) -> list[str]:
    """ALL event types the narrative touches — breadth (behavioral multi-front,
    relational dispute, predictive precursors want every type)."""
    return list(event_type_matches(narrative).keys())


def primary_event_type(narrative: dict[str, Any]) -> str | None:
    """The single event type a (bundled) card is most *about*, for thread identity.

    A daily entity-fusion card bundles many signals and often matches several event
    types (e.g. 'autorização … banco múltiplo nos EUA' hits both authorization AND
    expansion). Threading it into every match makes the same bundle the latest
    development of multiple incidents — near-duplicate cards. So a card seeds exactly
    ONE thread: the type with the most pattern hits (specificity), ties broken by
    taxonomy salience (acquisition/litigation/… before expansion/product_launch)."""
    matches = event_type_matches(narrative)
    if not matches:
        return None
    return min(matches, key=lambda et: (-matches[et], _EVENT_PRIORITY[et]))


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _entity_label(entity: str, n: dict[str, Any]) -> str:
    return (n.get("entity_label") or n.get("label") or ENTITY_LABELS.get(entity)
            or str(entity).replace("_", " ").title())


RADAR_EVENT_TYPE = "service_outage"
RADAR_YT_SCORE = 0.5       # a confirmed YouTube outage/complaint: below ALERT_PEAK on its own
RADAR_APPSTORE_SCORE = 0.7  # an App Store review spike (count >= 10 AND z >= 3): alert-grade


def radar_developments(radar: dict[str, Any] | None, *, as_of: str, window: int) -> list[dict[str, Any]]:
    """#375: CPO-radar failure evidence as ``service_outage`` developments (label-only).

    Confirmed YouTube ``outage``/``complaint`` events (official channel or >= 2 channels) are
    NOT alertable — creators talking about a failure never page an executive on their own.
    App Store baseline alerts are alertable. Nothing from YouTube is kept but the radar id and
    Onça's labels (YouTube API terms, 30-day deletion)."""
    from src.synth import radar_evidence

    out: list[dict[str, Any]] = []
    evs = radar_evidence.confirmed_events(radar or {}, types=radar_evidence.FAILURE_TYPES)
    for ev, score, alertable in ([(e, RADAR_YT_SCORE, False) for e in evs]
                                 + [(a, RADAR_APPSTORE_SCORE, True)
                                    for a in radar_evidence.appstore_alerts(radar or {})]):
        gap = _days_between(as_of, ev["date"])
        if gap is None or not 0 <= gap <= window:
            continue
        out.append({
            "entity": ev["entity"], "label": ev.get("product_label"),
            "date": str(ev["date"])[:10], "narrative_id": ev["id"],
            "summary": f"Radar de produto: {radar_evidence.claim(ev)}.",
            "threat_score": score,
            "citations": [radar_evidence.citation(ev)], "source_ids": [],
            "is_alert": False, "radar": True, "alertable": alertable,
        })
    return out


def build_threads(
    narratives: list[dict[str, Any]], *, as_of: str | None = None, window: int = 90,
    radar: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Pure: activity narratives (+ #375 radar failure evidence) -> {incident_id: thread doc}.
    Deterministic, no I/O."""
    as_of = as_of or run_date_today()
    close_days = int(_f("ONCA_THREAD_CLOSE_DAYS", CLOSE_DAYS))
    min_dev = int(_f("ONCA_THREAD_MIN_DEVELOPMENTS", MIN_DEVELOPMENTS))
    min_dates = int(_f("ONCA_THREAD_MIN_DATES", MIN_DATES))
    developing_min = int(_f("ONCA_THREAD_DEVELOPING_MIN", DEVELOPING_MIN))

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for n in narratives:
        if not feature_store.is_activity_narrative(n):
            continue
        ent = n.get("entity")
        date = feature_store._date_of(n)
        if not ent or not date:
            continue
        # A card seeds exactly ONE thread — its PRIMARY event type — so a bundled
        # fusion card that touches several types no longer becomes the latest
        # development of multiple incidents (the near-duplicate-cards bug).
        et = primary_event_type(n)
        if not et:
            continue
        g = groups.setdefault((ent, et), {
            "entity": ent, "event_type": et, "label": _entity_label(ent, n),
            "devs": {}, "lenses": set()})
        # One development per DATE — the day's card advances the story. (The daily
        # fusion card reuses a stable id across dates, so we key by date, not id,
        # or a multi-day arc would collapse into one development.) Keep the day's
        # highest-threat card as that date's development.
        summary = (n.get("narrative") or "").strip()
        score = _score(n.get("threat_score"))
        dev = g["devs"].get(date)
        if dev is None or score > dev["threat_score"]:
            g["devs"][date] = {
                "date": date, "narrative_id": n.get("id"), "summary": summary,
                "threat_score": score,
                "citations": [c for c in (n.get("citations") or []) if isinstance(c, dict)],
                "source_ids": list(n.get("source_ids") or []),
                "is_alert": bool(n.get("is_alert")),
            }
        for lens in n.get("lenses") or []:
            g["lenses"].add(lens)

    # #375: radar failure evidence joins (entity, service_outage). A narrative development
    # keeps its date (the radar citation rides along); radar fills dates no narrative covers.
    for rd in radar_developments(radar, as_of=as_of, window=window):
        ent = rd["entity"]
        g = groups.setdefault((ent, RADAR_EVENT_TYPE), {
            "entity": ent, "event_type": RADAR_EVENT_TYPE,
            "label": _entity_label(ent, {"label": rd.get("label")}), "devs": {}, "lenses": set()})
        dev = g["devs"].get(rd["date"])
        if dev is None:
            g["devs"][rd["date"]] = {k: v for k, v in rd.items() if k not in ("entity", "label")}
        elif dev.get("radar"):  # two radar items on one day: keep the alert-grade one
            if rd["alertable"] and not dev.get("alertable"):
                rd = {**rd, "citations": rd["citations"] + dev["citations"]}
                g["devs"][rd["date"]] = {k: v for k, v in rd.items() if k not in ("entity", "label")}
            else:
                dev["citations"] = dev["citations"] + rd["citations"]
        else:
            dev["citations"] = dev["citations"] + rd["citations"]
            dev["alertable"] = True

    out: dict[str, dict[str, Any]] = {}
    for (ent, et), g in groups.items():
        devs = sorted(g["devs"].values(), key=lambda d: d["date"])
        dates = {d["date"] for d in devs}
        if len(devs) < min_dev or len(dates) < min_dates:
            continue
        narr_devs = [d for d in devs if not d.get("radar")]
        radar_backed = (len(narr_devs) < min_dev or len({d["date"] for d in narr_devs}) < min_dates)
        alert_devs = [d for d in devs if not d.get("radar") or d.get("alertable")]
        first_seen, last_updated = devs[0]["date"], devs[-1]["date"]
        peak = max(d["threat_score"] for d in devs)
        latest = devs[-1]["threat_score"]
        since_update = _days_between(as_of, last_updated)
        if since_update is not None and since_update > close_days:
            status = "resolved"
        elif len(devs) >= developing_min:
            status = "developing"
        else:
            status = "open"
        incident_id = f"{ent}--{et}"
        out[incident_id] = {
            "incident_id": incident_id,
            "entity": ent,
            "entities": sorted({e for d in devs for e in [ent]}),
            "event_type": et,
            "event_label": EVENT_TYPES[et]["label"],
            "title": f"{g['label']} — {EVENT_TYPES[et]['label']}",
            "status": status,
            "first_seen": first_seen,
            "last_updated": last_updated,
            "n_developments": len(devs),
            "n_dates": len(dates),
            "peak_threat": round(peak, 3),
            "latest_threat": round(latest, 3),
            # #375: only narrative / App Store developments can raise the alert.
            "alert_peak": round(max((d["threat_score"] for d in alert_devs), default=0.0), 3),
            "radar_backed": radar_backed,
            "lenses": sorted(g["lenses"]),
            "developments": devs,
        }
    return out


def _thread_score(thread: dict[str, Any]) -> float:
    """Active incidents keep some of their peak severity; resolved ones score down."""
    peak, latest = thread["peak_threat"], thread["latest_threat"]
    if thread["status"] == "resolved":
        return round(0.3 * peak, 3)
    return round(min(1.0, max(latest, 0.7 * peak)), 3)


def _agg_citations(devs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for d in reversed(devs):  # newest developments' sources first
        for c in d.get("citations") or []:
            key = c.get("url") or c.get("id") or json.dumps(c, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
    return out


def build_card(thread: dict[str, Any]) -> dict[str, Any]:
    """A feed-ready narrative-like card for one incident thread (grounded, labeled)."""
    devs = thread["developments"]
    score = _thread_score(thread)
    alert_peak = thread.get("alert_peak", thread["peak_threat"])
    alert = thread["status"] != "resolved" and alert_peak >= _f("ONCA_THREAD_ALERT_PEAK", ALERT_PEAK)
    latest_summary = (devs[-1]["summary"] or "").strip()

    def _fmt(d: str) -> str:
        try:
            return feature_store._parse(d).strftime("%d/%m")
        except Exception:
            return d

    head = (
        f"Fio de incidente: {thread['title']} — {_STATUS_PT[thread['status']]}, "
        f"{thread['n_developments']} desenvolvimentos entre {_fmt(thread['first_seen'])} "
        f"e {_fmt(thread['last_updated'])}."
    )
    if latest_summary:
        head += f" Última atualização: {latest_summary[:220]}"
    tail = (
        " Agrupamento derivado de sinais já reportados (fio de eventos relacionados por "
        "entidade e tipo de evento) — cada desenvolvimento cita a própria fonte."
    )
    return {
        "id": f"threaded-{thread['incident_id']}",
        "kind": "threaded",
        "axis": AXIS,
        "subject_type": "incident",
        "incident_title": thread["title"],
        "incident_id": thread["incident_id"],
        "event_type": thread["event_type"],
        "status": thread["status"],
        "entity": thread["entity"],
        "entities": thread["entities"],
        "lenses": list(thread["lenses"]),
        "is_alert": alert,
        "is_inference": True,
        "radar_backed": bool(thread.get("radar_backed")),
        "threat_score": score,
        "threat_factors": {
            "n_developments": thread["n_developments"],
            "peak_threat": thread["peak_threat"],
            "latest_threat": thread["latest_threat"],
            "status": thread["status"],
        },
        "threat_score_note": "estimated_v1_threaded",
        "n_developments": thread["n_developments"],
        # The card this thread's latest development re-displays — so the feed builder
        # can drop the thread when that exact card is already standalone in the feed
        # (else the thread reads as a verbatim duplicate of today's daily card).
        "latest_dev_id": devs[-1].get("narrative_id"),
        "latest_dev_date": devs[-1].get("date"),
        "narrative": head + tail,
        "citations": _agg_citations(devs),
        "source_ids": sorted({s for d in devs for s in d.get("source_ids") or []}),
        "mode": "derived",
        "run_date": thread["last_updated"],
        "run_at": run_at_now(),
        "as_of": thread["last_updated"],
        "data_as_of": {"first_seen": thread["first_seen"], "last_updated": thread["last_updated"]},
    }


def publish(threads: dict[str, dict[str, Any]], bucket: str, *, s3: Any | None = None,
            as_of: str | None = None, max_age: int | None = None) -> dict[str, Any]:
    """Overwrite threads/{incident_id}.json (full) + threads/index.json (feed cards)."""
    s3 = s3 or boto3.client("s3")
    as_of = as_of or run_date_today()
    max_age = int(max_age if max_age is not None else _f("ONCA_THREAD_FEED_MAX_AGE", FEED_MAX_AGE))
    for tid, thread in threads.items():
        s3.put_object(
            Bucket=bucket, Key=f"{THREADS_PREFIX}{tid}.json",
            Body=json.dumps(thread, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json", CacheControl="no-cache",
        )
    # index carries recent threads only, as feed-ready cards
    cards = []
    for thread in threads.values():
        age = _days_between(as_of, thread["last_updated"])
        if age is not None and age <= max_age:
            cards.append(build_card(thread))
    cards.sort(key=lambda c: (c["run_date"], c["threat_score"]), reverse=True)
    index = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "as_of": as_of, "cards": cards,
    }
    s3.put_object(
        Bucket=bucket, Key=INDEX_KEY,
        Body=json.dumps(index, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json", CacheControl="no-cache",
    )
    return index


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Rebuild the incident thread store from the durable narrative history."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}

    window = int(_f("ONCA_THREAD_WINDOW_DAYS", _f("ONCA_FEATURE_WINDOW_DAYS", 90)))
    s3 = boto3.client("s3")
    run_date = run_date_today()

    narratives = feature_store.load_history(digests_bucket, window, s3=s3)
    from src.synth import radar_evidence
    radar = radar_evidence.load(digests_bucket, s3=s3)
    threads = build_threads(narratives, as_of=run_date, window=window, radar=radar)

    published = None
    try:
        idx = publish(threads, digests_bucket, s3=s3, as_of=run_date)
        published = f"s3://{digests_bucket}/{INDEX_KEY}"
        n_cards = len(idx["cards"])
    except Exception as exc:  # pragma: no cover - publish best-effort
        print(f"Warning: thread publish failed: {exc}")
        n_cards = 0

    by_status: dict[str, int] = {}
    for t in threads.values():
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1

    return {
        "statusCode": 200,
        "body": json.dumps({
            "status": "ok",
            "as_of": run_date,
            "window_days": window,
            "threads": len(threads),
            "by_status": by_status,
            "radar_backed": sorted(t for t, th in threads.items() if th.get("radar_backed")),
            "index_cards": n_cards,
            "published": published,
        }),
    }
