"""Wave 1 (ADR 003) — the Regulatory-lifecycle / deadline axis: what's due, who's hit.

The subject here is a **policy instrument** (an Instrução Normativa, a Resolução, the
Regulamento do Pix), not a competitor — the sector-wide rulebook that acts on every
entity at once. Where thematic narrates what competitors are *doing*, this narrates
what the *regulator* is imposing and when it comes due.

Design (per ADR 003 — "instrument thread + deadline extraction"; deterministic floor,
LLM extraction deferred to v2):
- **Instrument thread, no LLM.** Regexes lift instrument references (IN BCB, Resolução
  BCB/CMN/CVM, Circular, Regulamento/Manual do Pix) out of the regulatory narratives
  and thread them by a stable instrument key. Comunicados (backward-looking auction /
  rate notices) are **excluded by default** — precision-first (opt in with
  `ONCA_REG_INCLUDE_COMUNICADOS`).
- **Best-effort deadline.** A future pt-BR date co-occurring with a deadline cue
  ("a partir de", "até", "entra em vigor", "vigência", "prazo") within the horizon is
  attached as the instrument's deadline; absent one, the card still tracks the change.
- **Affected domain, not per-entity compliance (yet).** A keyword taxonomy infers the
  affected domain/cohort (Pagamentos/PIX, Crédito & portabilidade, Câmbio, …). The
  per-entity "who hasn't complied" map is the harder enabler — deferred to v2.
- **Grounded inference.** Cites the regulator's `exibenormativo` links; the instrument
  ref + date are sourced, only the affected/urgency read is labeled inference
  (`is_inference` / `mode="derived"` / `axis="regulatory"`).
- **SWOT feeder (ADR 004).** A new/changed rule is an external **Threat** (compliance
  burden) on competitors → `swot_hint` dimension "T".
- **No feedback loop.** `regulatory` is a derived axis (excluded from feature-store
  activity).
- **Change-tuned emit-on-change.** An instrument re-fires only when its signature
  (version / deadline) changes or the cooldown lapses; a deadline that passes or an
  instrument that drops out of the window is retracted same-day.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import unicodedata
from typing import Any

import boto3

from src.synth import feature_store
from src.synth.synthesize import run_at_now, run_date_today

AXIS = "regulatory"

# --- Nomination gate (env-overridable) -------------------------------------
RECENCY_DAYS = 21        # an instrument referenced within this window is "current"
COOLDOWN_DAYS = 14       # unchanged instrument re-fires only after this
DEADLINE_HORIZON = 365   # only future deadlines within this many days are attached
ALERT_WITHIN = 30        # a deadline this near flips the card to an alert


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except (TypeError, ValueError):
        return float(default)


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.lower()


def _days_between(a: str, b: str) -> int | None:
    try:
        return (feature_store._parse(a) - feature_store._parse(b)).days
    except Exception:
        return None


# --- Instrument extraction --------------------------------------------------
# Numbers may carry a pt-BR thousands separator ("5.333"); _num strips it so
# "Resolução CMN 5.333" threads as one instrument, not "5333" AND "5".
_NUM = r"(\d{1,3}(?:\.\d{3})*|\d{1,5})"

_INSTRUMENTS = [
    ("in-bcb", "Instrução Normativa BCB {n}", re.compile(r"instrucao normativa\s*bcb[:\s]*" + _NUM)),
    ("res-bcb", "Resolução BCB {n}", re.compile(r"resolucao\s+bcb\s*(?:n[o]?\s*)?[:\s]*" + _NUM)),
    ("res-cmn", "Resolução CMN {n}", re.compile(r"resolucao\s+cmn\s*(?:n[o]?\s*)?[:\s]*" + _NUM)),
    ("res-cvm", "Resolução CVM {n}", re.compile(r"resolucao\s+cvm\s*(?:n[o]?\s*)?[:\s]*" + _NUM)),
    ("circ", "Circular BCB {n}", re.compile(r"circular\s+(?:bcb\s+)?" + _NUM)),
]
_PIX_REGULAMENTO = re.compile(r"(manual de padroes[^.]{0,40}pix|regulamento do pix)")
_PIX_VERSION = re.compile(r"vers[aã]o\s*([\d.]+)")
_COMUNICADO = re.compile(r"comunicado[s]?\s*(\d{4,6})")

# How many chars around an instrument mention bind its deadline/domain — so a date
# or topic elsewhere in a dense multi-instrument card does not attach to every ref.
_CONTEXT_WINDOW = 220


def _num(raw: str) -> str:
    return raw.replace(".", "").strip()

_DEADLINE_CUE = re.compile(
    r"(a partir de|ate\s+\d|ate\s+o dia|entra em vigor|entrar[aã] em vigor|"
    r"vig[êe]ncia|prazo|passa a valer|obrigatori)"
)
_DATE_NUM = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_MONTHS = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}
_DATE_TXT = re.compile(r"(\d{1,2})\s+de\s+(" + "|".join(_MONTHS) + r")\s+de\s+(\d{4})")

# Affected-domain taxonomy (accent-free keyword -> domain label).
_DOMAINS = [
    ("Pagamentos / PIX", [r"\bpix\b", r"iniciacao do pix", r"regulamento do pix", r"arranjo de pagament"]),
    ("Crédito & portabilidade", [r"\bcredito\b", r"emprestimo", r"portabilidade", r"consignad"]),
    ("Câmbio & mercado aberto", [r"\bcambio\b", r"\bswap\b", r"compromissad", r"oferta publica", r"\bofpub\b", r"leilao"]),
    ("Open Finance", [r"open finance", r"open banking", r"compartilhamento de dados"]),
    ("Autorizações & governança", [r"orgaos estatutarios", r"autorizada a funcionar", r"cancelamento da autorizacao", r"eleit"]),
]
_DOMAIN_PATS = [(label, [re.compile(p) for p in pats]) for label, pats in _DOMAINS]


def _mentions(text: str, *, include_comunicados: bool) -> list[tuple[str, str, int, int]]:
    """Every instrument mention as (key, display_label, start, end) on normalized text."""
    out: list[tuple[str, str, int, int]] = []
    for prefix, disp_t, rx in _INSTRUMENTS:
        for m in rx.finditer(text):
            num = _num(m.group(1))
            out.append((f"{prefix}-{num}", disp_t.format(n=num), m.start(), m.end()))
    for m in _PIX_REGULAMENTO.finditer(text):
        vm = _PIX_VERSION.search(text)
        ver = vm.group(1) if vm else None
        disp = "Regulamento do Pix" + (f" (v{ver})" if ver else "")
        out.append(("regulamento-pix", disp, m.start(), m.end()))
    if include_comunicados:
        for m in _COMUNICADO.finditer(text):
            out.append((f"comunicado-{m.group(1)}", f"Comunicado BCB {m.group(1)}",
                        m.start(), m.end()))
    return out


def instruments_in(narrative: dict[str, Any], *, include_comunicados: bool = False) -> dict[str, str]:
    """{instrument_key: display_label} referenced by this narrative."""
    text = _norm(narrative.get("narrative") or "")
    found: dict[str, str] = {}
    for key, disp, _s, _e in _mentions(text, include_comunicados=include_comunicados):
        if len(disp) > len(found.get(key, "")):
            found[key] = disp
    return found


def _domain_of(text: str) -> str:
    t = _norm(text)
    for label, pats in _DOMAIN_PATS:
        if any(p.search(t) for p in pats):
            return label
    return "Setor financeiro"


def _future_deadline(text: str, as_of: str, horizon: int) -> str | None:
    """Nearest future date co-occurring with a deadline cue, within the horizon."""
    t = _norm(text)
    if not _DEADLINE_CUE.search(t):
        return None
    try:
        as_of_d = feature_store._parse(as_of)
    except Exception:
        return None
    cands: list[dt.date] = []
    for d, m, y in _DATE_NUM.findall(t):
        try:
            cands.append(dt.date(int(y), int(m), int(d)))
        except ValueError:
            continue
    for d, mon, y in _DATE_TXT.findall(t):
        try:
            cands.append(dt.date(int(y), _MONTHS[mon], int(d)))
        except (ValueError, KeyError):
            continue
    future = sorted(x for x in cands if 0 < (x - as_of_d).days <= horizon)
    return future[0].isoformat() if future else None


def prior_instruments(narratives: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Most-recent prior regulatory card per instrument: {key: {run_date, signature}}."""
    out: dict[str, dict[str, Any]] = {}
    for n in narratives:
        if not isinstance(n, dict) or n.get("axis") != AXIS:
            continue
        key = n.get("instrument")
        rd = feature_store._date_of(n)
        if not key or not rd:
            continue
        prev = out.get(key)
        if prev is None or rd > prev["run_date"]:
            out[key] = {"run_date": rd, "signature": n.get("signature")}
    return out


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def threads(narratives: list[dict[str, Any]], *, as_of: str, recency: int,
            include_comunicados: bool, horizon: int) -> dict[str, dict[str, Any]]:
    """Aggregate recent regulatory activity into per-instrument threads."""
    groups: dict[str, dict[str, Any]] = {}
    for n in narratives:
        if not feature_store.is_activity_narrative(n):
            continue
        d = feature_store._date_of(n)
        if not d:
            continue
        gap = _days_between(as_of, d)
        if gap is None or gap < 0 or gap > recency:
            continue
        raw = n.get("narrative") or ""
        norm = _norm(raw)
        mentions = _mentions(norm, include_comunicados=include_comunicados)
        seen_here: set[str] = set()
        for key, disp, start, end in mentions:
            # Bind deadline/domain to a window AROUND this mention, so a date or topic
            # elsewhere in a dense multi-instrument card can't attach to every ref.
            ctx = norm[max(0, start - _CONTEXT_WINDOW): end + _CONTEXT_WINDOW]
            g = groups.setdefault(
                key, {"label": disp, "cards": [], "latest": "", "deadline": None, "domain": None}
            )
            if len(disp) > len(g["label"]):
                g["label"] = disp
            if key not in seen_here:  # count a card once per instrument
                g["cards"].append(n)
                seen_here.add(key)
            local_domain = _domain_of(ctx)
            if d >= g["latest"] and (g["domain"] is None or local_domain != "Setor financeiro"):
                g["latest"] = d
                g["domain"] = local_domain
            dl = _future_deadline(ctx, as_of, horizon)
            if dl and (g["deadline"] is None or dl < g["deadline"]):
                g["deadline"] = dl
    for g in groups.values():
        if not g["latest"]:
            g["latest"] = max((feature_store._date_of(c) for c in g["cards"]), default="")
        if g["domain"] is None:
            g["domain"] = "Setor financeiro"
    return groups


def nominate(
    narratives: list[dict[str, Any]],
    *,
    as_of: str | None = None,
    cooldown_days: int | None = None,
) -> list[dict[str, Any]]:
    """Pure gate: recent narratives -> regulatory-instrument candidates (no I/O)."""
    as_of = as_of or run_date_today()
    cooldown_days = int(
        cooldown_days if cooldown_days is not None
        else _f("ONCA_REG_COOLDOWN_DAYS", COOLDOWN_DAYS)
    )
    recency = int(_f("ONCA_REG_RECENCY_DAYS", RECENCY_DAYS))
    horizon = int(_f("ONCA_REG_DEADLINE_HORIZON", DEADLINE_HORIZON))
    include_com = bool(int(_f("ONCA_REG_INCLUDE_COMUNICADOS", 0)))

    groups = threads(narratives, as_of=as_of, recency=recency,
                     include_comunicados=include_com, horizon=horizon)
    prior = prior_instruments(narratives)

    out: list[dict[str, Any]] = []
    for key, g in groups.items():
        signature = f"{g['label']}|{g['deadline'] or ''}"
        prev = prior.get(key)
        if prev is not None and prev.get("signature") == signature:
            gap = _days_between(as_of, prev["run_date"])
            if gap is not None and gap < cooldown_days:
                continue  # unchanged instrument within cooldown

        dtd = _days_between(g["deadline"], as_of) if g["deadline"] else None
        drivers = sorted(
            g["cards"],
            key=lambda n: (_score(n.get("threat_score")), feature_store._date_of(n)),
            reverse=True,
        )[:3]
        out.append(
            {
                "instrument": key,
                "label": g["label"],
                "domain": g["domain"],
                "deadline": g["deadline"],
                "days_to_deadline": dtd,
                "mentions": len(g["cards"]),
                "latest": g["latest"],
                "signature": signature,
                "drivers": drivers,
            }
        )
    # Nearest deadline first, then most-mentioned.
    out.sort(key=lambda c: (
        c["days_to_deadline"] if c["days_to_deadline"] is not None else 10**6,
        -c["mentions"],
    ))
    return out


def _reg_score(cand: dict[str, Any]) -> float:
    """Context-tier; a nearer deadline raises urgency (capped)."""
    dtd = cand.get("days_to_deadline")
    if dtd is None:
        return 0.25
    if dtd <= ALERT_WITHIN:
        return round(min(0.7, 0.7 - 0.3 * (dtd / ALERT_WITHIN)), 3)
    return round(min(0.5, 0.25 + 0.25 * max(0.0, 1 - dtd / 180)), 3)


def swot_hint(cand: dict[str, Any]) -> dict[str, Any]:
    """ADR 004: a new/changed rule is an external Threat (compliance burden)."""
    return {"dimension": "T", "sign": "-", "instrument": cand["instrument"],
            "domain": cand["domain"]}


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
    """Heuristic pt-BR regulatory-radar card — sourced instrument, labeled inference."""
    label = cand["label"]
    domain = cand["domain"]
    dtd = cand.get("days_to_deadline")
    deadline = cand.get("deadline")
    score = _reg_score(cand)
    is_alert = bool(deadline and dtd is not None and dtd <= ALERT_WITHIN)
    drivers = cand.get("drivers") or []
    citations = _agg_citations(drivers)
    source_ids: list[str] = []
    for d in drivers:
        source_ids.extend(d.get("source_ids") or [])

    head = f"Radar regulatório — {label}."
    lead = (drivers[0].get("narrative") or "").strip() if drivers else ""
    if lead:
        head += f" {lead[:220]}"
    if deadline:
        try:
            dd = feature_store._parse(deadline).strftime("%d/%m/%Y")
        except Exception:
            dd = deadline
        head += f" Prazo: vence em {dd}" + (f" (faltam {dtd} dias)." if dtd is not None else ".")
    head += f" Afeta: {domain}."
    tail = (
        " Instrumento e data extraídos da fonte reguladora; o domínio afetado e a "
        "urgência são inferência (não uma avaliação de conformidade por entidade)."
    )

    return {
        "id": f"regulatory-{cand['instrument']}",
        "kind": "regulatory_lifecycle",
        "axis": AXIS,
        "subject_type": "instrument",
        "instrument": cand["instrument"],
        "instrument_label": label,
        "domain": domain,
        "deadline": deadline,
        "days_to_deadline": dtd,
        "signature": cand["signature"],
        "entity": None,
        "entities": [],
        "lenses": ["regulatory"],
        "is_alert": is_alert,
        "is_inference": True,
        "threat_score": score,
        "threat_factors": {
            "days_to_deadline": dtd,
            "mentions": cand["mentions"],
            "has_deadline": bool(deadline),
        },
        "threat_score_note": "estimated_v1_regulatory",
        "swot_hint": swot_hint(cand),
        "narrative": head + tail,
        "citations": citations,
        "source_ids": source_ids,
        "mode": "derived",
        "run_date": run_date_today(),
        "run_at": run_at_now(),
        "as_of": run_date_today(),
        "data_as_of": {"latest_reference": cand["latest"], "deadline": deadline},
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Load recent history, emit regulatory-instrument radar narratives."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}

    window_days = int(_f("ONCA_REG_WINDOW_DAYS", _f("ONCA_FEATURE_WINDOW_DAYS", 90)))
    s3 = boto3.client("s3")
    run_date = run_date_today()

    recent = feature_store.load_history(digests_bucket, window_days, s3=s3)
    cands = nominate(recent, as_of=run_date)

    keys: list[str] = []
    fired = {c["instrument"] for c in cands}
    for cand in cands:
        key = _write(build_narrative(cand), digests_bucket, s3)
        if key:
            keys.append(key)

    # Same-day retraction: a regulatory card written today whose instrument no longer
    # clears the RAW gate (dropped out of the window) is invalidated.
    recency = int(_f("ONCA_REG_RECENCY_DAYS", RECENCY_DAYS))
    horizon = int(_f("ONCA_REG_DEADLINE_HORIZON", DEADLINE_HORIZON))
    include_com = bool(int(_f("ONCA_REG_INCLUDE_COMUNICADOS", 0)))
    qualifying = set(threads(recent, as_of=run_date, recency=recency,
                             include_comunicados=include_com, horizon=horizon).keys())
    retracted = _retract_same_day(digests_bucket, s3, run_date, qualifying)

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "status": "ok",
                "as_of": run_date,
                "window_days": window_days,
                "nominated": len(cands),
                "emitted": len(keys),
                "instruments": [
                    f"{c['instrument']}"
                    + (f"@{c['deadline']}" if c["deadline"] else "")
                    for c in cands
                ],
                "keys": keys,
                "retracted": sorted(retracted),
            }
        ),
    }


def _retract_same_day(bucket: str, s3: Any, run_date: str, qualifying: set[str]) -> list[str]:
    """Delete same-day regulatory cards for instruments that fell out of the window."""
    prefix = f"{feature_store.NARRATIVES_PREFIX}{run_date}/regulatory-"
    out: list[str] = []
    try:
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    except Exception as exc:  # pragma: no cover - best-effort
        print(f"Warning: list regulatory cards failed: {exc}")
        return out
    for obj in resp.get("Contents") or []:
        key = obj["Key"]
        inst = key[len(prefix):].removesuffix(".json")
        if inst in qualifying:
            continue
        try:
            s3.delete_object(Bucket=bucket, Key=key)
            out.append(inst)
        except Exception as exc:  # pragma: no cover - best-effort
            print(f"Warning: retract regulatory card {key} failed: {exc}")
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
        print(f"Warning: write regulatory narrative failed: {exc}")
        return None
