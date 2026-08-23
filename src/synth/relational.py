"""Wave 3 (ADR 003) — the Relational / dyadic axis: A and B, as a typed edge.

The first consumer of the **relationship graph** (Shift 2's third derived-state
store) and the first axis whose subject is an **entity pair**, not an entity. It
accumulates typed edges between tracked competitors from the durable narrative
history and narrates the deltas that matter — but under the ADR's strongest
guardrails, because a dyadic claim is about *two* real named firms:

- **co_mention** (factual) — A and B named together in the same real activity
  signal, repeatedly. Not defamatory (they *were* co-reported) → may surface as a
  labeled-inference card.
- **convergence** (interpretive) — A and B persistently active in the same **niche**
  competitive arena (a theme with few participants), across several dates. The
  merger/"converging projects" cousin → **PROPOSED only, review-gated**, never a card.
- **dispute** (interpretive, highest-stakes) — a litigation/investigation signal
  names both. A false "A vs. B in court" is defamatory → **PROPOSED only**.

Design (ADR 003 Decisions 4 & 5, precision-first):
- **Deterministic nominator, no LLM.** Edges are counted, not judged.
- **Grounded + labeled inference.** Every edge cites its underlying signals; cards
  carry `is_inference`/`mode="derived"`/`axis="relational"` and never assert a
  relationship as fact.
- **Interpretive edges never auto-publish.** convergence & dispute go to
  `graph/relational_proposals.json` (idempotent review queue) exactly like new
  entities earn `news_safe` and SWOT contradictions propose — a false edge is a
  legal risk, not just a quality one.
- **Input-gated, honestly.** Genuine two-competitor co-mentions and litigation
  co-parties are rare in the current ingestion; the mechanism emits what the data
  supports (today: niche-theme convergence proposals) and reports a gate status
  otherwise — it activates with no code change when richer signals land.
- **No feedback loop.** `relational` is a derived axis (excluded from feature-store
  activity), so an edge never re-shapes a baseline.

The full graph is published to `graph/edges.json` (every typed edge + evidence, the
API-product substrate); factual co_mention cards to `graph/index.json`.
"""
from __future__ import annotations

import datetime as dt
import itertools
import json
import os
from typing import Any, Iterable

import boto3

from src.synth import feature_store
from src.synth.synthesize import ENTITY_LABELS, run_at_now, run_date_today

AXIS = "relational"
GRAPH_EDGES_KEY = "graph/edges.json"
GRAPH_INDEX_KEY = "graph/index.json"
RELATIONAL_PROPOSALS_KEY = "graph/relational_proposals.json"

# Event types (from the thread taxonomy) that make a co-mention a *dispute* edge.
_ADVERSARIAL = {"litigation", "investigation"}

# --- gates (env-overridable) -----------------------------------------------
MIN_COMENTION = 2        # co_mention: >= this many real co-reported signals ...
MIN_COMENTION_DATES = 2  # ... across >= this many distinct dates (persistent, not a one-off)
NICHE_MAX = 6            # a theme with <= this many participants is a *niche* arena
MIN_CONV_DATES = 2       # convergence: pair co-present in a niche theme on >= this many dates
MAX_PAIRS = 200          # safety cap on emitted proposals per run


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except (TypeError, ValueError):
        return float(default)


def _label(ent: str, fallback: dict[str, str] | None = None) -> str:
    if fallback and ent in fallback:
        return fallback[ent]
    return ENTITY_LABELS.get(ent) or str(ent).replace("_", " ").title()


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _event_types_of(n: dict[str, Any]) -> set[str]:
    """Best-effort event-type tags on a narrative (reuses the threads taxonomy)."""
    try:
        from src.synth import threads

        return set(threads.event_types_of(n))
    except Exception:  # pragma: no cover - threads import best-effort
        return set()


def build_graph(narratives: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Pure: narratives -> {(a,b,relation): edge}. Deterministic, no I/O.

    - **co_mention / dispute** from *activity* narratives (real signals) naming >=2
      tracked entities — dispute when the signal is litigation/investigation.
    - **convergence** from *thematic* cards (theme membership), but only for **niche**
      themes (<= NICHE_MAX participants), so 'all big banks post quarterly results'
      never becomes a relationship.
    """
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    labels: dict[str, str] = {}

    def _edge(a: str, b: str, rel: str) -> dict[str, Any]:
        key = (*_pair(a, b), rel)
        return edges.setdefault(key, {
            "a": key[0], "b": key[1], "relation": rel,
            "evidence": {}, "dates": set(), "themes": set(),
        })

    for n in narratives:
        if not isinstance(n, dict):
            continue
        date = feature_store._date_of(n)
        ents = sorted({e for e in (n.get("entities") or []) if e})
        if len(ents) < 2:
            continue
        for e in ents:
            labels.setdefault(e, n.get("entity_label") or _label(e))

        if n.get("axis") == "thematic":
            # niche-theme convergence only (a crowded theme is not a relationship)
            if len(ents) > int(_f("ONCA_RELATIONAL_NICHE_MAX", NICHE_MAX)):
                continue
            theme = (n.get("swot_hint") or {}).get("theme") or n.get("theme_display")
            for a, b in itertools.combinations(ents, 2):
                edge = _edge(a, b, "convergence")
                if date:
                    edge["dates"].add(date)
                if theme:
                    edge["themes"].add(theme)
                nid = n.get("id")
                if nid:
                    edge["evidence"][f"{nid}@{date}"] = {"id": nid, "date": date, "theme": theme}
        elif feature_store.is_activity_narrative(n):
            # a real signal that names >=2 competitors: co_mention (or dispute)
            rel = "dispute" if _event_types_of(n) & _ADVERSARIAL else "co_mention"
            for a, b in itertools.combinations(ents, 2):
                edge = _edge(a, b, rel)
                if date:
                    edge["dates"].add(date)
                nid = n.get("id")
                if nid:
                    edge["evidence"][nid] = {"id": nid, "date": date,
                                             "lenses": n.get("lenses") or []}

    # finalize derived counts
    for edge in edges.values():
        ev = sorted(edge["evidence"].values(), key=lambda e: e.get("date") or "", reverse=True)
        edge["evidence"] = ev
        edge["weight"] = len(ev)
        edge["n_dates"] = len(edge["dates"])
        edge["first_seen"] = min(edge["dates"]) if edge["dates"] else None
        edge["last_seen"] = max(edge["dates"]) if edge["dates"] else None
        edge["themes"] = sorted(edge["themes"])
        del edge["dates"]
        edge["a_label"] = labels.get(edge["a"], _label(edge["a"]))
        edge["b_label"] = labels.get(edge["b"], _label(edge["b"]))
    return edges


def _factual(edge: dict[str, Any]) -> bool:
    """co_mention edges that clear the persistence gate may auto-publish (non-defamatory)."""
    return (
        edge["relation"] == "co_mention"
        and edge["weight"] >= int(_f("ONCA_RELATIONAL_MIN_COMENTION", MIN_COMENTION))
        and edge["n_dates"] >= int(_f("ONCA_RELATIONAL_MIN_COMENTION_DATES", MIN_COMENTION_DATES))
    )


def _proposable(edge: dict[str, Any]) -> bool:
    """Interpretive edges (convergence/dispute) that clear their gate -> review queue."""
    if edge["relation"] == "convergence":
        return edge["n_dates"] >= int(_f("ONCA_RELATIONAL_MIN_CONV_DATES", MIN_CONV_DATES))
    if edge["relation"] == "dispute":
        return edge["weight"] >= 1  # any sourced litigation co-party is worth a review
    return False


def nominate(edges: dict[tuple[str, str, str], dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Split gated edges into factual cards vs. review-gated proposals."""
    factual = [e for e in edges.values() if _factual(e)]
    proposals = [e for e in edges.values() if _proposable(e)]
    factual.sort(key=lambda e: (e["weight"], e["n_dates"]), reverse=True)
    proposals.sort(key=lambda e: (e["relation"] == "dispute", e["n_dates"], e["weight"]),
                   reverse=True)
    cap = int(_f("ONCA_RELATIONAL_MAX_PAIRS", MAX_PAIRS))
    return {"factual": factual[:cap], "proposals": proposals[:cap]}


_REL_PT = {"co_mention": "citados juntos", "convergence": "convergência de arena",
           "dispute": "disputa (não confirmada)"}


def build_card(edge: dict[str, Any]) -> dict[str, Any]:
    """Feed-ready card for a FACTUAL (co_mention) edge — labeled inference, cites signals."""
    a, b = edge["a_label"], edge["b_label"]
    cites = [c for e in edge["evidence"] for c in (e.get("citations") or []) if isinstance(c, dict)]
    head = (
        f"Relacional: {a} e {b} aparecem juntos em {edge['weight']} sinais "
        f"({edge['n_dates']} dias) — coocorrência factual, não uma relação afirmada."
    )
    return {
        "id": f"relational-{edge['a']}-{edge['b']}-{edge['relation']}",
        "kind": "relational",
        "axis": AXIS,
        "subject_type": "pair",
        "relation": edge["relation"],
        "entity": None,
        "entities": [edge["a"], edge["b"]],
        "pair_label": f"{a} × {b}",
        "lenses": [],
        "is_alert": False,
        "is_inference": True,
        "threat_score": round(min(0.35, 0.1 + 0.03 * edge["weight"]), 3),
        "threat_factors": {"relation": edge["relation"], "weight": edge["weight"],
                           "n_dates": edge["n_dates"]},
        "threat_score_note": "estimated_v1_relational",
        "n_developments": edge["weight"],
        "narrative": head,
        "citations": cites[:6],
        "source_ids": [],
        "mode": "derived",
        "run_date": run_date_today(),
        "run_at": run_at_now(),
        "as_of": run_date_today(),
        "data_as_of": {"last_seen": edge["last_seen"]},
    }


def build_proposal(edge: dict[str, Any], *, run_date: str) -> dict[str, Any]:
    """Review-queue entry for an INTERPRETIVE edge (convergence/dispute) — never a card."""
    a, b = edge["a_label"], edge["b_label"]
    rel = edge["relation"]
    if rel == "convergence":
        themes = ", ".join(edge["themes"]) or "arena de nicho"
        text = (f"{a} e {b} atuam persistentemente na mesma arena de nicho "
                f"({themes}) em {edge['n_dates']} dias — possível convergência competitiva.")
    else:  # dispute
        text = (f"Sinal jurídico (litígio/investigação) menciona {a} e {b} — "
                f"possível disputa. NÃO confirmada; requer curadoria.")
    return {
        "id": f"{rel}:{edge['a']}:{edge['b']}",
        "kind": rel,
        "a": edge["a"], "b": edge["b"],
        "a_label": a, "b_label": b,
        "relation": rel,
        "themes": edge["themes"],
        "text": text,
        "weight": edge["weight"],
        "n_dates": edge["n_dates"],
        "evidence_ids": [e.get("id") for e in edge["evidence"] if e.get("id")][:12],
        "status": "pending",
        "created": run_date,
        "last_seen": edge["last_seen"],
    }


# --- idempotent proposal store (mirrors swot_reconcile) ----------------------
def merge_proposals(existing: list[dict[str, Any]], fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {p["id"]: p for p in (existing or []) if p.get("id")}
    for p in fresh:
        prior = by_id.get(p["id"])
        if prior:
            by_id[p["id"]] = {**p, "status": prior.get("status", "pending"),
                              "created": prior.get("created", p.get("created"))}
        else:
            by_id[p["id"]] = p
    return sorted(by_id.values(),
                  key=lambda p: (p.get("last_seen") or "", p.get("id")), reverse=True)


def _serializable_edges(edges: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(edges, key=lambda e: (e["weight"], e["n_dates"]), reverse=True)


def _load_json(bucket: str, key: str, s3: Any) -> dict[str, Any]:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body.decode("utf-8"))
    except Exception:  # pragma: no cover - absent/unreadable
        return {}


def publish(edges: dict[tuple[str, str, str], dict[str, Any]], nominated: dict[str, list[dict[str, Any]]],
            bucket: str, *, s3: Any, run_date: str) -> dict[str, int]:
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    # 1) the full graph (every typed edge + evidence)
    s3.put_object(
        Bucket=bucket, Key=GRAPH_EDGES_KEY,
        Body=json.dumps({"generated_at": now, "as_of": run_date,
                         "edges": _serializable_edges(edges.values())},
                        ensure_ascii=False).encode("utf-8"),
        ContentType="application/json", CacheControl="no-cache",
    )
    # 2) factual co_mention cards for the feed
    cards = [build_card(e) for e in nominated["factual"]]
    s3.put_object(
        Bucket=bucket, Key=GRAPH_INDEX_KEY,
        Body=json.dumps({"generated_at": now, "as_of": run_date, "cards": cards},
                        ensure_ascii=False).encode("utf-8"),
        ContentType="application/json", CacheControl="no-cache",
    )
    # 3) interpretive edges -> idempotent review queue (never auto-published)
    prev = _load_json(bucket, RELATIONAL_PROPOSALS_KEY, s3).get("proposals", [])
    fresh = [build_proposal(e, run_date=run_date) for e in nominated["proposals"]]
    merged = merge_proposals(prev, fresh)
    s3.put_object(
        Bucket=bucket, Key=RELATIONAL_PROPOSALS_KEY,
        Body=json.dumps({"generated_at": now, "as_of": run_date, "proposals": merged},
                        ensure_ascii=False).encode("utf-8"),
        ContentType="application/json", CacheControl="no-cache",
    )
    return {"edges": len(edges), "cards": len(cards),
            "proposals": len(merged), "proposals_new": max(0, len(merged) - len(prev))}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Build the relationship graph; publish factual cards + review-gated proposals."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}

    window = int(_f("ONCA_RELATIONAL_WINDOW_DAYS", _f("ONCA_FEATURE_WINDOW_DAYS", 90)))
    s3 = boto3.client("s3")
    run_date = run_date_today()

    narratives = feature_store.load_history(digests_bucket, window, s3=s3)
    edges = build_graph(narratives)
    nominated = nominate(edges)

    counts = {"edges": 0, "cards": 0, "proposals": 0}
    try:
        counts = publish(edges, nominated, digests_bucket, s3=s3, run_date=run_date)
    except Exception as exc:  # pragma: no cover - publish best-effort
        print(f"Warning: relational publish failed: {exc}")

    # Honest gate status: no genuine dyadic signal beyond niche-theme convergence yet.
    status = "ok" if counts["edges"] else "data_gated"
    return {
        "statusCode": 200,
        "body": json.dumps({
            "status": status, "as_of": run_date, "window_days": window,
            "run_at": run_at_now(), **counts,
        }),
    }
