"""Wave 1 (ADR 003) — the Thematic / sector-current axis: what the sector is doing.

Where comparative/longitudinal/silence each narrate ONE entity, thematic narrates a
**cross-entity current**: a theme (crypto, apostas, resultados trimestrais, crédito &
inadimplência, …) that several distinct competitors are moving on at once. The subject
is a *theme*, not an entity — "what the sector is collectively doing about X."

Design (per ADR 003, mirrors the other Wave 1 detectors):
- **Deterministic, no LLM.** A keyword taxonomy tags each activity narrative with
  theme(s); a theme becomes a "current" when >= MIN_ENTITIES distinct competitors and
  >= MIN_MENTIONS activity cards touch it inside a recency window. The card is a
  heuristic pt-BR briefing. (Taxonomy-first per ADR 003's "keyword taxonomy → LLM";
  the LLM upgrade is deferred — the deterministic tagger is the shippable floor.)
- **Grounded inference.** Cites the driving activity narratives as evidence while
  labeled inference (`is_inference` / `mode="derived"` / `axis="thematic"`).
- **Cross-entity subject.** `subject_type="theme"`, `entity=None`, `entities=[…]` — the
  participating competitors. Generic no-entity source cards (e.g. raw BCB notices) count
  toward mentions but never toward the distinct-entity gate, so a purely-regulatory
  cluster is left to the regulatory-lifecycle axis, not mis-fired as a sector current.
- **SWOT feeder (ADR 004 note #7).** Themes are external market forces, so they map to
  **O/T** bullets: an expansionary current (crypto, FIIs, expansão) reads as an
  **Opportunity**, a pressure current (crédito & inadimplência) as a **Threat**. Pure
  data-cadence themes (resultados trimestrais, movimentação executiva) carry no hint.
- **No feedback loop.** `thematic` is a derived axis (excluded from feature-store
  activity), so a current card never re-shapes any baseline.
- **Current-tuned emit-on-change.** A standing current is suppressed within a cooldown
  unless its participant tier grows; a theme that falls below threshold is retracted
  same-day.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Any

import boto3

from src.synth import feature_store
from src.synth.synthesize import ENTITY_LABELS, run_at_now, run_date_today

AXIS = "thematic"

# --- Nomination gate (env-overridable) -------------------------------------
MIN_ENTITIES = 3       # a theme needs >= this many DISTINCT competitors to be a current
MIN_MENTIONS = 4       # ... and >= this many activity cards touching it
RECENCY_DAYS = 14      # window in which engagement counts toward a current
COOLDOWN_DAYS = 7      # re-emit suppression: only a grown participant tier re-fires
MAX_DRIVERS = 3        # sample activity cards cited as evidence per theme


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except (TypeError, ValueError):
        return float(default)


def _days_between(a: str, b: str) -> int | None:
    try:
        return (feature_store._parse(a) - feature_store._parse(b)).days
    except Exception:
        return None


def _norm(text: str) -> str:
    """Lowercase + strip accents so 'inadimplência' matches 'inadimplencia'."""
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.lower()


# --- Theme taxonomy ---------------------------------------------------------
# Each theme: display (pt-BR), accent-free regex patterns, and a SWOT dimension for
# ADR 004 (O = opportunity / T = threat / None = data-cadence, no hint). Patterns run
# against the accent-stripped lowercase narrative text; short tokens use \b anchors so
# "bet" does not match "debate".
THEMES: dict[str, dict[str, Any]] = {
    "crypto": {
        "display": "Cripto & stablecoins",
        "dim": "O",
        "patterns": [r"cripto", r"stablecoin", r"bitcoin", r"\bcrypto\b",
                     r"blockchain", r"\btoken", r"\bdrex\b", r"real digital"],
    },
    "betting": {
        "display": "Apostas & bets",
        "dim": "O",
        "patterns": [r"\bapostas?\b", r"\bbet\b", r"\bbets\b", r"cassino",
                     r"\bcasino\b", r"apostas? esportivas?", r"bookmaker", r"bettor"],
    },
    "fii": {
        "display": "Fundos imobiliários (FII)",
        "dim": "O",
        "patterns": [r"fundo imobiliario", r"\bfiis?\b", r"\bfiagro\b",
                     r"emissao de cotas", r"nova oferta.*cotas", r"\d+.?\s*emissao"],
    },
    "credit_risk": {
        "display": "Crédito & inadimplência",
        "dim": "T",
        "patterns": [r"inadimplenc", r"\bcredito\b", r"endividad", r"score .*credito",
                     r"\bcalote\b", r"busca por credito", r"corte no credito"],
    },
    "expansion": {
        "display": "Expansão & internacionalização",
        "dim": "O",
        "patterns": [r"banco multiplo", r"expandind", r"expansao", r"internacional",
                     r"\beua\b", r"nos estados unidos", r"lancament", r"nova parceria",
                     r"esta expandindo"],
    },
    "quarterly_results": {
        "display": "Resultados trimestrais",
        "dim": None,
        "patterns": [r"\d\s?t\s?2\d\b", r"resultados? do (primeiro|segundo|terceiro|quarto) trimestre",
                     r"lucro liquido", r"prejuizo", r"earnings release", r"resultados financeiros",
                     r"balanco financeiro", r"divulgou.*result"],
    },
    "leadership": {
        "display": "Movimentação executiva",
        "dim": None,
        "patterns": [r"\bceo\b", r"\bcfo\b", r"\bcoo\b", r"nomea", r"nomeacao",
                     r"novo presidente", r"novo diretor", r"assume o comando"],
    },
    "esg_climate": {
        "display": "ESG & risco climático",
        "dim": "O",
        "patterns": [r"\besg\b", r"sustentabil", r"risco climatic", r"climatic",
                     r"descarboniz", r"transicao energetica"],
    },
}


def _compiled() -> dict[str, list[re.Pattern]]:
    return {slug: [re.compile(p) for p in cfg["patterns"]] for slug, cfg in THEMES.items()}


_PATTERNS = _compiled()


def themes_of(narrative: dict[str, Any]) -> list[str]:
    """Every taxonomy theme whose pattern hits this narrative's text."""
    text = _norm(narrative.get("narrative") or "")
    if not text:
        return []
    return [slug for slug, pats in _PATTERNS.items() if any(p.search(text) for p in pats)]


def theme_tier(entity_count: int) -> int:
    if entity_count >= 9:
        return 3
    if entity_count >= 5:
        return 2
    return 1


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def prior_currents(narratives: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Most-recent prior thematic card per theme: {theme: {run_date, tier}}."""
    out: dict[str, dict[str, Any]] = {}
    for n in narratives:
        if not isinstance(n, dict) or n.get("axis") != AXIS:
            continue
        theme = n.get("theme")
        rd = feature_store._date_of(n)
        if not theme or not rd:
            continue
        prev = out.get(theme)
        if prev is None or rd > prev["run_date"]:
            out[theme] = {"run_date": rd, "tier": int(n.get("theme_tier") or 1)}
    return out


def _recent_activity(narratives: list[dict[str, Any]], as_of: str, recency: int) -> list[dict[str, Any]]:
    """Activity narratives (source-grounded, non-derived) within the recency window."""
    out = []
    for n in narratives:
        if not feature_store.is_activity_narrative(n):
            continue
        d = feature_store._date_of(n)
        if not d:
            continue
        gap = _days_between(as_of, d)
        if gap is not None and 0 <= gap <= recency:
            out.append(n)
    return out


def theme_groups(
    narratives: list[dict[str, Any]], *, as_of: str, recency: int
) -> dict[str, dict[str, Any]]:
    """Aggregate recent activity by theme: distinct entities, mentions, drivers."""
    groups: dict[str, dict[str, Any]] = {}
    for n in _recent_activity(narratives, as_of, recency):
        ent = n.get("entity")
        label = n.get("entity_label") or n.get("label")
        for theme in themes_of(n):
            g = groups.setdefault(
                theme, {"entities": {}, "mentions": 0, "cards": [], "lenses": set(), "latest": ""}
            )
            g["mentions"] += 1
            if ent:  # only real entities count toward the distinct-competitor gate
                g["entities"].setdefault(ent, label or ENTITY_LABELS.get(ent, str(ent).title()))
            for lens in n.get("lenses") or []:
                g["lenses"].add(lens)
            d = feature_store._date_of(n)
            if d > g["latest"]:
                g["latest"] = d
            g["cards"].append(n)
    return groups


def nominate(
    narratives: list[dict[str, Any]],
    *,
    as_of: str | None = None,
    cooldown_days: int | None = None,
) -> list[dict[str, Any]]:
    """Pure gate: recent narratives -> sector-current candidates (no I/O)."""
    as_of = as_of or run_date_today()
    cooldown_days = int(
        cooldown_days if cooldown_days is not None
        else _f("ONCA_THEMATIC_COOLDOWN_DAYS", COOLDOWN_DAYS)
    )
    min_entities = int(_f("ONCA_THEMATIC_MIN_ENTITIES", MIN_ENTITIES))
    min_mentions = int(_f("ONCA_THEMATIC_MIN_MENTIONS", MIN_MENTIONS))
    recency = int(_f("ONCA_THEMATIC_RECENCY_DAYS", RECENCY_DAYS))

    groups = theme_groups(narratives, as_of=as_of, recency=recency)
    prior = prior_currents(narratives)

    out: list[dict[str, Any]] = []
    for theme, g in groups.items():
        ents = g["entities"]
        if len(ents) < min_entities or g["mentions"] < min_mentions:
            continue
        tier = theme_tier(len(ents))

        prev = prior.get(theme)
        if prev is not None:
            gap = _days_between(as_of, prev["run_date"])
            if gap is not None and gap < cooldown_days and tier <= prev["tier"]:
                continue

        # Top drivers: highest-threat, most-recent activity cards (the cited evidence).
        drivers = sorted(
            g["cards"],
            key=lambda n: (_score(n.get("threat_score")), feature_store._date_of(n)),
            reverse=True,
        )[:MAX_DRIVERS]

        out.append(
            {
                "theme": theme,
                "display": THEMES[theme]["display"],
                "dim": THEMES[theme]["dim"],
                "entities": sorted(ents),
                "entity_labels": [ents[e] for e in sorted(ents)],
                "entity_count": len(ents),
                "mentions": g["mentions"],
                "theme_tier": tier,
                "lenses": sorted(g["lenses"]),
                "latest": g["latest"],
                "drivers": drivers,
            }
        )
    # Broadest, most-active currents first.
    out.sort(key=lambda c: (c["theme_tier"], c["entity_count"], c["mentions"]), reverse=True)
    return out


def _current_score(cand: dict[str, Any]) -> float:
    """Theme-level context (never a top alert): grows modestly with breadth."""
    return round(min(0.5, 0.2 + 0.03 * cand["entity_count"]), 3)


def swot_hint(cand: dict[str, Any]) -> dict[str, Any] | None:
    """ADR 004 note #7: a sector current maps to an O/T market force on competitors.
    Expansionary theme => Opportunity (+); pressure theme => Threat (−). Data-cadence
    themes (dim None) carry no hint."""
    dim = cand.get("dim")
    if dim not in ("O", "T"):
        return None
    return {
        "dimension": dim,
        "sign": "+" if dim == "O" else "-",
        "theme": cand["theme"],
        "entities": cand["entities"],
    }


def _agg_citations(drivers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for d in drivers:
        for c in d.get("citations") or []:
            if not isinstance(c, dict):
                continue
            key = c.get("url") or c.get("id") or json.dumps(c, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
    return out


def build_narrative(cand: dict[str, Any]) -> dict[str, Any]:
    """Heuristic pt-BR sector-current card — labeled inference, cites the drivers."""
    display = cand["display"]
    labels = cand["entity_labels"]
    n_ent = cand["entity_count"]
    recency = int(_f("ONCA_THEMATIC_RECENCY_DAYS", RECENCY_DAYS))
    score = _current_score(cand)
    drivers = cand.get("drivers") or []
    citations = _agg_citations(drivers)
    source_ids: list[str] = []
    for d in drivers:
        source_ids.extend(d.get("source_ids") or [])

    named = ", ".join(labels[:6]) + ("…" if len(labels) > 6 else "")
    head = (
        f"Corrente setorial — {display}: {n_ent} concorrentes se movimentaram neste "
        f"tema nos últimos {recency} dias ({named})."
    )
    lead = (drivers[0].get("narrative") or "").strip() if drivers else ""
    if lead:
        head += f" Destaque: {lead[:240]}"
    tail = (
        " Índice agregado a partir da atividade de vários concorrentes (inferência "
        "de corrente setorial, não um fato novo de uma entidade) — os sinais citados "
        "são a evidência."
    )

    return {
        "id": f"thematic-{cand['theme']}",
        "kind": "thematic",
        "axis": AXIS,
        "subject_type": "theme",
        "theme": cand["theme"],
        "theme_display": display,
        "entity": None,
        "entities": list(cand["entities"]),
        "entity_labels": labels,
        "lenses": list(cand["lenses"]),
        "is_alert": False,
        "is_inference": True,
        "threat_score": score,
        "threat_factors": {
            "entity_count": n_ent,
            "mentions": cand["mentions"],
            "theme_tier": cand["theme_tier"],
        },
        "threat_score_note": "estimated_v1_thematic",
        "theme_tier": cand["theme_tier"],
        "swot_hint": swot_hint(cand),
        "narrative": head + tail,
        "citations": citations,
        "source_ids": source_ids,
        "mode": "derived",
        "run_date": run_date_today(),
        "run_at": run_at_now(),
        "as_of": run_date_today(),
        "data_as_of": {"latest_activity": cand["latest"]},
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Load recent history, emit cross-entity sector-current narratives."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}

    window_days = int(_f("ONCA_THEMATIC_WINDOW_DAYS", _f("ONCA_FEATURE_WINDOW_DAYS", 90)))
    s3 = boto3.client("s3")
    run_date = run_date_today()

    recent = feature_store.load_history(digests_bucket, window_days, s3=s3)
    cands = nominate(recent, as_of=run_date)

    keys: list[str] = []
    fired = {c["theme"] for c in cands}
    for cand in cands:
        key = _write(build_narrative(cand), digests_bucket, s3)
        if key:
            keys.append(key)

    # Same-day retraction: a theme whose card was written earlier today but that no
    # longer clears the RAW threshold (ignoring cooldown) is invalidated. Cheap: one
    # head per taxonomy theme not firing today; misses (no card) are skipped.
    min_entities = int(_f("ONCA_THEMATIC_MIN_ENTITIES", MIN_ENTITIES))
    min_mentions = int(_f("ONCA_THEMATIC_MIN_MENTIONS", MIN_MENTIONS))
    recency = int(_f("ONCA_THEMATIC_RECENCY_DAYS", RECENCY_DAYS))
    groups = theme_groups(recent, as_of=run_date, recency=recency)
    qualifying = {
        t for t, g in groups.items()
        if len(g["entities"]) >= min_entities and g["mentions"] >= min_mentions
    }
    stale = [t for t in THEMES if t not in qualifying]
    retracted = _retract_same_day(digests_bucket, s3, run_date, stale)

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "status": "ok",
                "as_of": run_date,
                "window_days": window_days,
                "nominated": len(cands),
                "emitted": len(keys),
                "themes": [f"{c['theme']}({c['entity_count']})" for c in cands],
                "keys": keys,
                "retracted": sorted(retracted),
            }
        ),
    }


def _retract_same_day(bucket: str, s3: Any, run_date: str, stale: list[str]) -> list[str]:
    """Delete same-day current cards for themes that no longer qualify."""
    out: list[str] = []
    for theme in sorted(stale):
        key = f"{feature_store.NARRATIVES_PREFIX}{run_date}/thematic-{theme}.json"
        try:
            s3.head_object(Bucket=bucket, Key=key)
        except Exception:
            continue  # no same-day card for this theme — nothing to retract
        try:
            s3.delete_object(Bucket=bucket, Key=key)
            out.append(theme)
        except Exception as exc:  # pragma: no cover - best-effort
            print(f"Warning: retract thematic card {key} failed: {exc}")
    return out


def _write(narrative: dict[str, Any], bucket: str, s3: Any) -> str | None:
    date = str(narrative.get("run_date") or narrative.get("as_of") or "unknown")[:10]
    key = f"{feature_store.NARRATIVES_PREFIX}{date}/{narrative['id']}.json"
    try:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(narrative, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        return key
    except Exception as exc:  # pragma: no cover - write is best-effort
        print(f"Warning: write thematic narrative failed: {exc}")
        return None
