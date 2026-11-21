"""Historic board episodes with answer keys (#339, ADR 031 Board M2).

An episode is a past event: the S&P, Fitch, Moody's and 2018 S&P sovereign downgrades, the
arrest of BTG's controller, and three bank failures (Cruzeiro do Sul, BVA, Rural). For each,
``episode_doc`` writes:

* a **feed frozen at ``as_of``**, built only from the M1 stores (prudential history #333,
  board minutes and material facts #331, rating events #332, Selic #334, complaints #335,
  B3 prices #336, the REF stress-test section #337). It has the shape of ``feed.json``, so
  the real ``pack.build_pack`` and proximity score run on it unchanged (as for the synthetic
  ``competitor-v2``), and ``scripts/board_dry_run.py --scenario`` replays it;
* an **answer key** read after the event: the focal board's decisions in the following
  ``ANSWER_WINDOW_DAYS`` from CVM minutes (each tagged with a decision category), the share
  price reaction, the next quarters' prudential numbers, rating actions filed after, and for
  failures the BCB regime that followed;
* a curated **rubric** (topics a good brief should raise), coverage counts, and ``usable``.

No look-ahead: every dated feed item is on or before ``as_of`` (``lookahead`` lists any that
is not, and ``episode_doc`` refuses to emit one). Point-in-time rules:

* IF.data for a quarter is taken as public ``IFDATA_LAG_DAYS`` (90) after the quarter end, a
  conservative lag: BVA's 2012-06 quarter is visible before its 2012-10 intervention,
  Cruzeiro do Sul's 2012-03 quarter is not visible before its 2012-06-04 RAET;
* the complaints ranking for a period is taken as public ``COMPLAINTS_LAG_DAYS`` after it;
* price windows end the day **before** ``as_of`` (BTG fell on the day of the arrest);
* the REF edition is the one whose conservative date (end of cover month) is on or before.

Failed banks have no registry entity (they left the system before the registry existed);
an episode names them ``ep-<slug>`` with their CNPJ root, and the script builds their points
from the stored IF.data extracts and B3 year files. No registry write.

Decision categories come from minute subjects only: CVM's document viewer sits behind a
CAPTCHA (never bypassed), so the text of a decision is not read.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "eval" / "board" / "episodes" / "episodes.json"
OUT_DIR = ROOT / "eval" / "board" / "episodes"
SCHEMA = "board/v1/episode"
IFDATA_LAG_DAYS = 90
COMPLAINTS_LAG_DAYS = 60
NEWS_DAYS = 120
ANSWER_WINDOW_DAYS = 120
MARKET_DAYS = 60
MARKET_SIGNAL_PP = 20
REF_EXCERPT_CHARS = 700

# Decision categories, first match wins (folded subject, no accents).
CATEGORIES = (
    ("capital_return", r"remuneracao (dos|aos) acionistas|payout|dividend|juros sobre (o )?capital|\bjcp\b|recompra|tesouraria|proprias acoes"),
    ("capital_raise", r"aumento d[eo] capital|emissao de acoes|subscricao|capitalizacao"),
    ("funding", r"debenture|letras? financeira|notas? promissoria|\bnotes?\b|bonds?|emissao|captac|divida subordinada"),
    ("strategic", r"aquisic|alienac|incorporac|fusao|\bvenda\b|participacao|joint|cisao|reorganizac"),
    ("independent_review", r"comite independente|investigac|apuracao|auditoria (independente|forense)|forensic"),
    ("governance", r"eleic|nomeac|membro do ca\b|falecimento|renuncia|conselheir|diretor|presidente|administrac|comite|destituic|mandato"),
    ("accounts", r"demonstrac|contabe|balanc|\bitr\b|resultado|informacoes trimestrais|parecer"),
    ("risk", r"rating|provis|risco|liquidez|capital regulat|basileia"),
)
ROUTINE = frozenset({"accounts"})


def fold(s: Any) -> str:
    a = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", a)


def category(subject: str) -> str:
    t = fold(subject)
    for name, pat in CATEGORIES:
        if re.search(pat, t):
            return name
    return "other"


def load_config(path: Path | str = CONFIG) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --- dates --------------------------------------------------------------------------

def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(str(s)[:10])


def quarter_end(q: int) -> dt.date:
    y, m = divmod(int(q), 100)
    nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
    return nxt - dt.timedelta(days=1)


def visible_quarter(as_of: str, lag_days: int = IFDATA_LAG_DAYS) -> int:
    """The latest quarter (YYYYMM) whose IF.data would be public on ``as_of``."""
    d = _d(as_of) - dt.timedelta(days=lag_days)
    q_month = (d.month - 1) // 3 * 3 + 3
    q = d.year * 100 + q_month
    if quarter_end(q) > d:
        q = (d.year - 1) * 100 + 12 if q_month == 3 else d.year * 100 + q_month - 3
    return q


def next_quarters(q: int, n: int) -> list[int]:
    out, y, m = [], *divmod(q, 100)
    for _ in range(n):
        y, m = (y + 1, 3) if m == 12 else (y, m + 3)
        out.append(y * 100 + m)
    return out


def shift(day: str, days: int) -> str:
    return (_d(day) + dt.timedelta(days=days)).isoformat()


# --- feed ---------------------------------------------------------------------------

_FUND_KEYS = ("roe_pct", "roa_pct", "basileia_headroom_pp", "credito_captacoes_pct", "carteira_share_pct",
              "lucro_share_pct", "ativo_bi", "pl_bi", "leverage")


def latest(points: Iterable[Mapping[str, Any]], upto: int) -> dict[str, Any] | None:
    pts = [p for p in points if p.get("as_of") and p["as_of"] <= upto]
    return dict(max(pts, key=lambda p: p["as_of"])) if pts else None


def entity_feed(eid: str, label: str, ifdata: Iterable[Mapping[str, Any]], capital: Iterable[Mapping[str, Any]],
                as_of: str) -> dict[str, Any]:
    """One feed entity from history points visible on ``as_of`` (fundamentals + soundness)."""
    q = visible_quarter(as_of)
    e: dict[str, Any] = {"entity": eid, "label": label, "industries": ["banking"]}
    f = latest(ifdata, q)
    if f:
        e["fundamentals"] = {**{k: f.get(k) for k in _FUND_KEYS if f.get(k) is not None}, "base_date": f["as_of"]}
        if f.get("carteira_share_pct") is not None:
            e["market_share_pct"] = f["carteira_share_pct"]
    c = latest(capital, q)
    if c and c.get("indice_basileia") is not None:
        e["soundness"] = {k: c.get(k) for k in ("indice_basileia", "capital_principal", "capital_nivel_i",
                                                 "razao_alavancagem", "band") if c.get(k) is not None}
        e["soundness"]["base_date"] = c["as_of"]
    return e


def _price_fact(series: Mapping[str, Any], eid: str, label: str, as_of: str) -> dict[str, Any] | None:
    from src.ingest import cotahist_history as ch
    end = shift(as_of, -1)
    r = ch.reaction(series, eid, end, before=MARKET_DAYS, after=0)
    if not r:
        return None
    bench = f" (mediana do setor {r['sector_median_pct']}%)" if r.get("sector_median_pct") is not None else ""
    return {"id": f"b3-{eid}-{end}", "kind": "market:b3", "entities": [eid], "date": end, "data_date": end,
            "abnormal_pct": r.get("abnormal_pct"),
            "narrative": f"{label}: ação {r['ticker']} variou {r['return_pct']}% de {r['from']} a {r['to']}"
                         f"{bench}. Preços sem ajuste por proventos."}


def build_feed(ep: Mapping[str, Any], stores: Mapping[str, Any]) -> dict[str, Any]:
    """The feed as of ``ep['as_of']``. ``stores`` holds what the script loaded:
    ``prudential`` (series), ``episode_points`` ({ep entity: {"ifdata": [...]}}), ``labels``,
    ``ipe_rows``, ``rating_events``, ``selic``, ``complaints``, ``b3``, ``ref_manifest`` and
    ``ref_text`` ({KB key: text})."""
    from src.ingest import prudential_history as ph, reclamacoes_history as rc
    from src.ingest import juros_history as jh, ref_history as rh
    as_of = ep["as_of"]
    q = visible_quarter(as_of)
    labels = dict(stores.get("labels") or {})
    labels.update({k: v["label"] for k, v in (ep.get("episode_entities") or {}).items()})
    scope = [ep["focal"], *ep.get("peers", [])]
    ents = []
    for eid in scope:
        label = labels.get(eid, eid)
        if eid in (ep.get("episode_entities") or {}):
            pts = (stores.get("episode_points") or {}).get(eid) or {}
            ents.append(entity_feed(eid, label, pts.get("ifdata") or [], pts.get("capital") or [], as_of))
        else:
            ser = stores.get("prudential") or {}
            ents.append(entity_feed(eid, label, ph.series_for(ser, eid, "ifdata", end=q),
                                    ph.series_for(ser, eid, "capital", end=q), as_of))
    feed_rows: list[dict[str, Any]] = []
    since = shift(as_of, -NEWS_DAYS)
    for r in stores.get("ipe_rows") or []:
        if r.get("entity_id") in scope and r.get("doc_class") in ("fato_relevante", "comunicado") \
                and since <= (r.get("date") or "") <= as_of:
            feed_rows.append({"id": f"ipe-{r['id']}", "kind": f"filing:{r['doc_class']}", "entities": [r["entity_id"]],
                              "date": r["date"], "data_date": r["date"],
                              "narrative": f"{labels.get(r['entity_id'], r['entity_id'])} ({r['doc_class'].replace('_', ' ')} na CVM): {r.get('subject')}"})
    for r in stores.get("rating_events") or []:
        if r.get("entity_id") in scope and since <= (r.get("date") or "") <= as_of:
            feed_rows.append({"id": f"rating-{re.sub(r'[^a-z0-9]+', '-', fold(r['id']))[:60]}", "kind": "rating:filing",
                              "entities": [r["entity_id"]], "date": r["date"], "data_date": r["date"],
                              "narrative": f"{labels.get(r['entity_id'], r['entity_id'])}: {r.get('subject')} (agência {r.get('agency')}, ação {r.get('action')})."})
    for eid in scope:
        pf = _price_fact(stores.get("b3") or {}, eid, labels.get(eid, eid), as_of)
        if pf:
            feed_rows.append(pf)
    events = []
    if ep.get("trigger") and ep["event_date"] <= as_of:
        t = ep["trigger"]
        events.append({"id": f"trigger-{ep['id']}", "date": ep["event_date"], "title": t["text"], "industry": "banking",
                       "entities": [], "sources": [{"source": t["source"], "url": t.get("url")}]})
    ref = rh.stress_at(stores.get("ref_manifest") or {}, as_of)
    if ref and ref["sections"]:
        sec = ref["sections"][0]
        body = (stores.get("ref_text") or {}).get(sec["key"]) or ""
        excerpt = _ref_excerpt(body)
        events.append({"id": f"ref-{ref['edition']}", "date": ref["date"], "industry": "banking", "entities": [],
                       "title": f"REF {ref['edition']} (BCB), {sec['label']}: {excerpt}".strip(),
                       "sources": [{"source": "BCB Relatório de Estabilidade Financeira", "url": ref["url"]}]})
    feed: dict[str, Any] = {"as_of": as_of, "generated_at": f"{as_of}T23:59:59+00:00", "episode": ep["id"],
                            "entities": ents, "feed": sorted(feed_rows, key=lambda x: x["date"]),
                            "sector_events": events}
    sel = jh.selic_at(stores.get("selic") or [], as_of)
    if sel is not None:
        last = _selic_last_change(stores.get("selic") or [], as_of)
        feed["macro"] = {"selic": {"current": sel, "as_of": as_of, "last_decision": last}}
    rep = []
    comp = stores.get("complaints") or {}
    for eid in scope:
        rows = [p for p in rc.series_for(comp, eid)
                if shift(rc.period_end(_period(p["period"])), COMPLAINTS_LAG_DAYS) <= as_of]
        if rows:
            p = rows[-1]
            rep.append({"id": f"rk-{eid}-{p['period']}", "entity": eid, "source": "BCB ranking de reclamações",
                        "company": p.get("name"), "rank": complaint_rank(comp, p), "index": p.get("indice"),
                        "category": p.get("category"), "period": p["period"]})
    if rep:
        feed["reputation"] = rep
    return feed


def complaint_rank(series: Mapping[str, Any], row: Mapping[str, Any]) -> int | None:
    """1 = most complaints per client, among the attributed rows of the same period, size
    band and method (the files carry the index, not the published position)."""
    if row.get("indice") is None:
        return None
    peers = [p.get("indice") for rows in (series.get("entities") or {}).values() for p in rows
             if p["period"] == row["period"] and p.get("category") == row.get("category")
             and p.get("method") == row.get("method") and p.get("indice") is not None]
    return 1 + sum(1 for v in peers if v > row["indice"])


def _period(label: str) -> dict[str, Any]:
    """'2015-S2' / '2017-T3' → the reclamacoes period dict."""
    y, p = label.split("-")
    return {"ano": int(y), "periodicidade": "SEMESTRAL" if p[0] == "S" else "TRIMESTRAL", "periodo": int(p[1:])}


def _selic_last_change(selic: list[list[Any]], as_of: str) -> dict[str, Any] | None:
    prev = last = None
    for d, v in selic:
        if d > as_of:
            break
        if last is not None and v != last[1]:
            prev = {"date": d, "previous": last[1], "value": v}
        last = (d, v)
    return prev


def _ref_excerpt(body: str, n: int = REF_EXCERPT_CHARS) -> str:
    """The first sentences of the stress section after the KB header lines."""
    text = body.split("\n\n", 1)[-1]
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= n:
        return text
    cut = text[:n]
    return (cut.rsplit(". ", 1)[0] + ".") if ". " in cut else cut.rsplit(" ", 1)[0] + "…"


def lookahead(feed: Mapping[str, Any]) -> list[str]:
    """Every dated feed item after ``feed['as_of']`` (must be empty)."""
    as_of = feed["as_of"]
    bad = []
    for x in feed.get("feed") or []:
        if (x.get("date") or "") > as_of:
            bad.append(x["id"])
    for x in feed.get("sector_events") or []:
        if (x.get("date") or "") > as_of:
            bad.append(x["id"])
    q = visible_quarter(as_of)
    for e in feed.get("entities") or []:
        for k in ("fundamentals", "soundness"):
            if (e.get(k) or {}).get("base_date", 0) > q:
                bad.append(f"{e['entity']}:{k}")
    for r in feed.get("reputation") or []:
        if shift(_period_end_label(r["period"]), COMPLAINTS_LAG_DAYS) > as_of:
            bad.append(r["id"])
    return bad


def _period_end_label(label: str) -> str:
    from src.ingest import reclamacoes_history as rc
    return rc.period_end(_period(label))


# --- answer key ------------------------------------------------------------------------

def answer_key(ep: Mapping[str, Any], stores: Mapping[str, Any]) -> dict[str, Any]:
    from src.ingest import cotahist_history as ch, prudential_history as ph
    ev, focal = ep["event_date"], ep["focal"]
    end = shift(ev, ANSWER_WINDOW_DAYS)
    decisions = []
    for r in stores.get("ipe_rows") or []:
        if r.get("entity_id") == focal and r.get("answer_key") and ev <= (r.get("date") or "") <= end:
            decisions.append({"date": r["date"], "doc_class": r["doc_class"], "subject": r.get("subject"),
                              "category": category(r.get("subject") or ""), "url": r.get("url")})
    decisions.sort(key=lambda d: (d["date"], d["subject"] or ""))
    filings = [{"date": r["date"], "doc_class": r["doc_class"], "subject": r.get("subject"), "url": r.get("url")}
               for r in stores.get("ipe_rows") or []
               if r.get("entity_id") == focal and r.get("doc_class") in ("fato_relevante",)
               and ev <= (r.get("date") or "") <= end]
    ratings = [{"date": r["date"], "agency": r.get("agency"), "action": r.get("action"), "subject": r.get("subject")}
               for r in stores.get("rating_events") or []
               if r.get("entity_id") == focal and ev <= (r.get("date") or "") <= end]
    market = {}
    for label, after in (("5d", 5), ("30d", 30)):
        r = ch.reaction(stores.get("b3") or {}, focal, ev, before=1, after=after)
        if r:
            market[label] = {k: r[k] for k in ("ticker", "from", "to", "return_pct", "sector_median_pct", "abnormal_pct")}
    q0 = visible_quarter(ep["as_of"])
    if focal in (ep.get("episode_entities") or {}):
        pts = ((stores.get("episode_points") or {}).get(focal) or {}).get("ifdata") or []
    else:
        pts = ph.series_for(stores.get("prudential") or {}, focal, "ifdata", start=q0)
    by_q = {p["as_of"]: p for p in pts}
    prudential = [{"quarter": q, **{k: by_q[q].get(k) for k in ("roe_pct", "basileia_headroom_pp", "pl_bi", "ativo_bi")}}
                  for q in [q0, *next_quarters(q0, 3)] if q in by_q]
    counts: dict[str, int] = {}
    for d in decisions:
        counts[d["category"]] = counts.get(d["category"], 0) + 1
    out = {"window": [ev, end], "board_decisions": decisions, "decision_categories": counts,
           "material_facts_after": filings, "ratings_after": ratings, "market_reaction": market,
           "prudential_after": prudential}
    if ep.get("outcome"):
        out["outcome"] = ep["outcome"]
    return out


def pre_event_signal(feed: Mapping[str, Any], focal: str) -> dict[str, Any]:
    """Whether the frozen public data already shows distress for the focal entity: capital
    headroom below zero or ROE below -15% in the visible quarter. For a failure the brief is
    expected to flag it when visible, and to say the data cannot rule it out when not.

    The share price counts too: a fall of ``MARKET_SIGNAL_PP`` or more below the sector over
    the pre-event window. Cruzeiro do Sul's last visible quarter looked healthy (the losses
    were a fraud), but its shares fell 44% in the 60 days before the RAET, 29 points worse
    than the sector."""
    e = next((x for x in feed.get("entities") or [] if x["entity"] == focal), {})
    f = e.get("fundamentals") or {}
    reasons, basis = [], []
    if (f.get("basileia_headroom_pp") is not None) and f["basileia_headroom_pp"] < 0:
        reasons.append(f"folga de Basileia {f['basileia_headroom_pp']} p.p. (abaixo do mínimo)")
        basis.append("prudential")
    if (f.get("roe_pct") is not None) and f["roe_pct"] < -15:
        reasons.append(f"ROE {f['roe_pct']}%")
        basis.append("prudential")
    for x in feed.get("feed") or []:
        if x.get("kind") == "market:b3" and focal in (x.get("entities") or []) \
                and (x.get("abnormal_pct") is not None) and x["abnormal_pct"] <= -MARKET_SIGNAL_PP:
            reasons.append(f"ação {x['abnormal_pct']} p.p. abaixo do setor em {MARKET_DAYS} dias")
            basis.append("market")
    return {"visible": bool(reasons), "quarter": f.get("base_date"), "reasons": reasons,
            "basis": sorted(set(basis))}


# --- episode ----------------------------------------------------------------------------

def coverage(feed: Mapping[str, Any], focal: str) -> dict[str, Any]:
    ents = {e["entity"]: e for e in feed.get("entities") or []}
    kinds: dict[str, int] = {}
    for x in feed.get("feed") or []:
        kinds[x["kind"]] = kinds.get(x["kind"], 0) + 1
    return {"entities_with_fundamentals": sorted(e for e, v in ents.items() if v.get("fundamentals")),
            "entities_with_soundness": sorted(e for e, v in ents.items() if v.get("soundness")),
            "focal_fundamentals": bool((ents.get(focal) or {}).get("fundamentals")),
            "feed_items": kinds, "sector_events": len(feed.get("sector_events") or []),
            "selic": bool(feed.get("macro")), "reputation": len(feed.get("reputation") or [])}


def usable(ep: Mapping[str, Any], cov: Mapping[str, Any], key: Mapping[str, Any]) -> tuple[bool, str]:
    if not cov["focal_fundamentals"]:
        return False, "sem dados prudenciais da instituição focal na data"
    material = [d for d in key["board_decisions"] if d["category"] not in ROUTINE]
    if ep["kind"] == "failure":
        return (True, "desfecho regulatório conhecido (registro do BCB)") if key.get("outcome") else \
            (False, "sem desfecho registrado")
    if material or key["market_reaction"]:
        return True, f"{len(material)} decisão(ões) não rotineira(s) do conselho; reação de mercado " \
                     f"{'disponível' if key['market_reaction'] else 'ausente'}"
    return False, "sem decisões do conselho nem reação de mercado na janela"


def episode_doc(ep: Mapping[str, Any], stores: Mapping[str, Any], rubrics: Mapping[str, Any],
                built_at: str) -> dict[str, Any]:
    feed = build_feed(ep, stores)
    bad = lookahead(feed)
    if bad:
        raise ValueError(f"{ep['id']}: look-ahead in the frozen feed: {bad[:5]}")
    key = answer_key(ep, stores)
    if ep["kind"] == "failure":
        key["pre_event_signal"] = pre_event_signal(feed, ep["focal"])
    cov = coverage(feed, ep["focal"])
    ok, why = usable(ep, cov, key)
    return {"schema": SCHEMA, "scenario": ep["id"], "kind": ep["kind"], "built_at": built_at,
            "note": "EPISÓDIO HISTÓRICO (#339). Pacote congelado na data com dados públicos; gabarito lido depois do evento.",
            "focal": ep["focal"], "subject": ep["focal"], "as_of": ep["as_of"], "event_date": ep["event_date"],
            "trigger": ep.get("trigger"), "task": ep["task"], "feed": feed,
            "rubric": {"id": ep["rubric"], "curated": True, "items": rubrics.get(ep["rubric"]) or []},
            "answer_key": key, "coverage": cov, "usable": ok, "usable_reason": why}
