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

from src.synth import feature_store, reg_change
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
# "Resolução CMN 5.333" threads as one instrument. Match the WHOLE digit run (with any
# internal dots) — an ordered `\d{1,3}` first alternative used to win on an UNdotted
# 4–5 digit number and truncate it ("5304" -> "530"); news text omits the dot, so that
# silently mis-threaded. `\d[\d.]*\d` takes the full run; `_num` strips the dots.
_NUM = r"(\d[\d.]*\d|\d)"

_INSTRUMENTS = [
    ("in-bcb", "Instrução Normativa BCB {n}", re.compile(r"instrucao normativa\s*bcb[:\s]*" + _NUM)),
    ("res-bcb", "Resolução BCB {n}", re.compile(r"resolucao\s+bcb\s*(?:n[o]?\s*)?[:\s]*" + _NUM)),
    ("res-cmn", "Resolução CMN {n}", re.compile(r"resolucao\s+cmn\s*(?:n[o]?\s*)?[:\s]*" + _NUM)),
    ("res-cvm", "Resolução CVM {n}", re.compile(r"resolucao\s+cvm\s*(?:n[o]?\s*)?[:\s]*" + _NUM)),
    # #71 — insurance regulator: CNSP resolutions + SUSEP circulars thread like BCB acts,
    # so insurance normativos (already fetched via the DOU organ filter) get the full
    # reg-lifecycle + change-record + "Mudança regulatória" treatment.
    ("res-cnsp", "Resolução CNSP {n}", re.compile(r"resolucao\s+cnsp\s*(?:n[o]?\s*)?[:\s]*" + _NUM)),
    ("circ-susep", "Circular SUSEP {n}", re.compile(r"circular\s+susep\s*(?:n[o]?\s*)?[:\s]*" + _NUM)),
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
    # Before the generic "Crédito" so securitização/recebíveis classify precisely.
    ("Securitização & crédito", [r"securitiz", r"\bfidc\b", r"\bcri\b", r"\bcra\b",
                                 r"recebiveis", r"registradora", r"direitos credit",
                                 r"cedula de credito"]),
    ("Crédito & portabilidade", [r"\bcredito\b", r"emprestimo", r"portabilidade", r"consignad"]),
    ("Câmbio & mercado aberto", [r"\bcambio\b", r"\bswap\b", r"compromissad", r"oferta publica", r"\bofpub\b", r"leilao"]),
    ("Open Finance", [r"open finance", r"open banking", r"compartilhamento de dados"]),
    # Before "Seguros & previdência" so CLOSED pension (EFPC/PREVIC) splits from open
    # (previdência aberta = SUSEP/insurance).
    ("Previdência complementar", [r"previdencia complementar", r"\befpc\b", r"\bprevic\b",
                                  r"entidade[s]? fechada", r"fundo de pensao", r"fundos de pensao"]),
    # #71 — insurance/pension rules classify precisely instead of the catch-all.
    ("Seguros & previdência", [r"\bseguro", r"seguradora", r"resseguro", r"previdenc",
                               r"capitalizac", r"\bsusep\b"]),
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


# --- Lifecycle stages (Wave 2: the full regulatory-lifecycle thread) --------
# An instrument moves proposal → publication → in-force → enforcement. Each mention
# is classified by cue precedence (enforcement > in-force > consultation > default
# publication). Data-gated today (our ingestion captures BCB notices sporadically,
# not each instrument's full pipeline), so lifecycles are few until richer regulatory
# ingestion lands — but stage classification adds a "where in the lifecycle" read now.
REG_LIFECYCLE_PREFIX = "reg_lifecycle/"
REG_LIFECYCLE_INDEX_KEY = "reg_lifecycle/index.json"
STAGE_ORDER = ["consulta", "publicacao", "vigencia", "fiscalizacao"]
STAGE_LABELS = {
    "consulta": "Consulta pública", "publicacao": "Publicação",
    "vigencia": "Vigência / prazo", "fiscalizacao": "Fiscalização",
}
_STAGE_CUES = [
    ("fiscalizacao", [r"fiscaliza", r"autuac", r"penalidade", r"descumprimento",
                      r"\bsancao\b", r"multa por", r"punic", r"irregularidad"]),
    ("vigencia", [r"entra em vigor", r"entrar[aã] em vigor", r"vig[eê]ncia",
                  r"a partir de", r"passa a valer", r"\bprazo\b", r"obrigatori"]),
    ("consulta", [r"consulta publica", r"audiencia publica", r"\bminuta\b",
                  r"proposta de", r"edital de consulta", r"tomada de subsidios",
                  r"em discussao", r"em consulta"]),
]
_STAGE_PATS = [(s, [re.compile(p) for p in pats]) for s, pats in _STAGE_CUES]


def stage_of(ctx: str) -> str:
    """Classify a mention's context window into a lifecycle stage (default publicação)."""
    t = _norm(ctx)
    for stage, pats in _STAGE_PATS:
        if any(p.search(t) for p in pats):
            return stage
    return "publicacao"


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
                key, {"label": disp, "cards": [], "latest": "", "deadline": None,
                      "domain": None, "mentions": []}
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
            # Lifecycle timeline material (Wave 2): stage of this mention + its evidence.
            g["mentions"].append({
                # wider than the 220-char display snippet so the amending clause (often
                # mid-text) survives for reg_change parsing (ADR-009 Phase A).
                "date": d, "stage": stage_of(ctx), "summary": raw.strip()[:600],
                "citations": [c for c in (n.get("citations") or []) if isinstance(c, dict)],
            })
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


# --- Instrument NEXUS (#51): a card's links + affected cohort must be ABOUT the
#     instrument it names. A single DOU/news item references many instruments, so
#     _agg_citations alone dumps a grab-bag (a "Resolução CMN 5304" card citing 5336,
#     5337, an unrelated Comunicado and CVM downloads). We (a) synthesize the
#     regulator's canonical page for the instrument as the authoritative checking
#     link, (b) keep only source citations that actually reference the instrument,
#     and (c) tag the affected FS industries so the card scopes and names its cohort.

# Instrument key prefix -> (regulator, BCB "exibenormativo" `tipo`). Confirmed live:
# .../exibenormativo?tipo=Resolução+CMN&numero=5337 is the canonical BCB norm page.
_INSTRUMENT_TIPO = {
    "in-bcb": ("bcb", "Instrução Normativa BCB"),
    "res-bcb": ("bcb", "Resolução BCB"),
    "res-cmn": ("bcb", "Resolução CMN"),
    "circ": ("bcb", "Circular"),
    "comunicado": ("bcb", "Comunicado"),
}

# Affected-domain -> FS industry taxonomy slugs (the cohort a sector rule acts on).
# Industry-level nexus is deterministic; per-ENTITY compliance blast-radius is #28.
_DOMAIN_INDUSTRIES = {
    "Pagamentos / PIX": ["acquiring", "fintech", "banking"],
    "Crédito & portabilidade": ["banking", "fintech", "consorcio"],
    "Câmbio & mercado aberto": ["banking", "investment-banking"],
    "Open Finance": ["banking", "fintech", "acquiring"],
    "Securitização & crédito": ["securitization", "fintech", "banking", "real-estate-funds"],
    "Previdência complementar": ["closed-pension", "asset-management"],
    "Seguros & previdência": ["insurance"],                                        # #71
    "Autorizações & governança": ["banking", "fintech", "insurance", "investment-banking", "consorcio"],
    "Setor financeiro": ["banking", "fintech", "insurance"],
}
_INDUSTRY_PT = {
    "acquiring": "Adquirência", "fintech": "Fintechs", "banking": "Bancos",
    "insurance": "Seguros", "investment-banking": "Banco de investimento",
    "consorcio": "Consórcios",
}


def _industries_for(domain: str) -> list[str]:
    """The PRECISE affected cohort for DISPLAY (narrative + chips) — the domain's mapped
    verticals; unknown → the core FS set."""
    return list(_DOMAIN_INDUSTRIES.get(domain) or _DOMAIN_INDUSTRIES["Setor financeiro"])


def industries_for_domain(domain: str, universe: Any = None) -> list[str]:
    """SCOPING industries for an entity-less regulatory card (#70). A specific domain scopes
    to its mapped verticals; the catch-all `Setor financeiro` / an unknown domain scopes to
    the WHOLE licensed universe — recall-first, so a sector-wide rule never vanishes from any
    tenant's Radar Regulatório. Intersected with the live universe so a stale map can't invent
    a slug. Scopes VISIBILITY only; the displayed cohort stays `affected_industries` (precise).
    """
    uni = None if universe is None else [str(u).strip().lower() for u in universe if u]
    mapped = _DOMAIN_INDUSTRIES.get(domain)
    if mapped is None or domain == "Setor financeiro":         # catch-all / unknown → all
        return list(uni) if uni else list(_DOMAIN_INDUSTRIES["Setor financeiro"])
    if uni is not None:
        keep = set(uni)
        return [i for i in mapped if i in keep]
    return list(mapped)


def _instrument_number(instrument_key: str) -> str | None:
    m = re.search(r"(\d+)$", instrument_key or "")
    return m.group(1) if m else None


def canonical_link(instrument_key: str, label: str) -> dict[str, Any] | None:
    """The regulator's own page for this instrument — built deterministically from
    (tipo, número), so it is ALWAYS about the named instrument. The checking-link
    nexus. Returns None for instrument types without a stable canonical URL."""
    from urllib.parse import quote

    num = _instrument_number(instrument_key)
    if not num:
        return None
    prefix = instrument_key.rsplit("-", 1)[0]
    info = _INSTRUMENT_TIPO.get(prefix)
    if not info:
        return None
    _reg, tipo = info
    url = ("https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?"
           f"tipo={quote(tipo)}&numero={num}")
    return {"url": url, "source": "BCB", "title": f"{label} — norma oficial (BCB)",
            "canonical": True}


def _cite_matches_instrument(c: dict[str, Any], num: str) -> bool:
    """True when a citation genuinely references the instrument number — an exact
    `numero=<n>` (strongest), else the number as a standalone token on a regulator
    host (gov.br). Opaque links (Google News RSS) carry no number and are dropped."""
    if not num:
        return False
    blob = " ".join(str(c.get(f, "")) for f in ("url", "title", "text", "id")).lower()
    m = re.search(r"numero=(\d+)", blob)
    if m:
        return m.group(1) == num
    if "gov.br" in blob and re.search(rf"\b{re.escape(num)}\b", blob):
        return True
    return False


def _instrument_citations(instrument_key: str, label: str,
                          drivers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Instrument-scoped citations: the canonical regulator page first, then only the
    source links that actually reference the instrument. Never the whole grab-bag."""
    num = _instrument_number(instrument_key)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Any two BCB `exibenormativo` links with the same `numero=` are the SAME norm page
    # regardless of `tipo`/encoding — so the canonical page and a source's own link to it
    # collapse. Other links dedup on the normalized URL.
    def _dedup_key(u: Any) -> str:
        s = str(u or "").lower()
        m = re.search(r"exibenormativo\?.*numero=(\d+)", s)
        return f"bcb-normativo:{m.group(1)}" if m else s.replace("+", "%20")
    canon = canonical_link(instrument_key, label)
    if canon:
        out.append(canon)
        seen.add(_dedup_key(canon["url"]))
    for c in _agg_citations(drivers):
        if not _cite_matches_instrument(c, num):
            continue
        u = c.get("url") or c.get("id")
        if _dedup_key(u) in seen:
            continue
        seen.add(_dedup_key(u))
        out.append(c)
    # No canonical page and nothing matched (e.g. a CVM instrument) — fall back to the
    # single strongest driver's citations, not the whole aggregate.
    if not out and drivers:
        out = _agg_citations(drivers[:1])
    return out


def build_narrative(cand: dict[str, Any], *, change_record: dict[str, Any] | None = None) -> dict[str, Any]:
    """Heuristic pt-BR regulatory-radar card — sourced instrument, labeled inference."""
    label = cand["label"]
    domain = cand["domain"]
    dtd = cand.get("days_to_deadline")
    deadline = cand.get("deadline")
    score = _reg_score(cand)
    is_alert = bool(deadline and dtd is not None and dtd <= ALERT_WITHIN)
    drivers = cand.get("drivers") or []
    citations = _instrument_citations(cand["instrument"], label, drivers)
    industries = _industries_for(domain)
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
    ind_pt = ", ".join(_INDUSTRY_PT.get(s, s) for s in industries)
    head += f" Afeta: {domain} (exposição provável: {ind_pt})."
    # ADR 009 Phase A: enumerate the act's own declared changes (amends/revokes/…).
    changes = reg_change.parse_changes(
        " ".join((d.get("narrative") or "") for d in drivers)[:3000],
        self_key=cand["instrument"])
    if changes:
        head += f" Mudanças: {reg_change.summarize_changes(changes)}."
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
        "industries": industries,
        # #70: precise cohort for DISPLAY (chips); `industries` is overridden recall-first at feed-build for scoping.
        "affected_industries": industries,
        "changes": changes,
        "n_changes": len(changes),
        # ADR 009 §3: the LLM change-record (rated), when drafted (radar cards too).
        "change_record": change_record,
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


# --- Full regulatory-lifecycle thread (Wave 2) ------------------------------
def build_lifecycles(
    narratives: list[dict[str, Any]], *, as_of: str | None = None, window: int = 90
) -> dict[str, dict[str, Any]]:
    """Per-instrument lifecycle thread: stage progression over time (deterministic)."""
    as_of = as_of or run_date_today()
    horizon = int(_f("ONCA_REG_DEADLINE_HORIZON", DEADLINE_HORIZON))
    include_com = bool(int(_f("ONCA_REG_INCLUDE_COMUNICADOS", 0)))
    min_dates = int(_f("ONCA_REG_LIFECYCLE_MIN_DATES", 2))

    groups = threads(narratives, as_of=as_of, recency=window,
                     include_comunicados=include_com, horizon=horizon)
    out: dict[str, dict[str, Any]] = {}
    for key, g in groups.items():
        # one timeline entry per (date, stage)
        seen: set[tuple[str, str]] = set()
        timeline: list[dict[str, Any]] = []
        for m in sorted(g.get("mentions") or [], key=lambda x: x["date"]):
            k = (m["date"], m["stage"])
            if k in seen:
                continue
            seen.add(k)
            timeline.append(m)
        dates = {m["date"] for m in timeline}
        if len(dates) < min_dates:
            continue  # a lifecycle needs a real multi-date arc (data-gated today)

        stages_seen = [s for s in STAGE_ORDER if any(m["stage"] == s for m in timeline)]
        current = stages_seen[-1] if stages_seen else "publicacao"
        deadline = g.get("deadline")
        dtd = _days_between(deadline, as_of) if deadline else None
        if (dtd is not None and dtd < 0) or current == "fiscalizacao":
            status = "resolved"
        elif len(stages_seen) >= 2:
            status = "developing"
        else:
            status = "open"
        out[key] = {
            "instrument": key, "label": g["label"], "domain": g["domain"],
            "deadline": deadline, "days_to_deadline": dtd,
            "stages_seen": stages_seen, "current_stage": current, "status": status,
            "first_seen": min(dates), "last_updated": max(dates),
            "n_dates": len(dates), "timeline": timeline,
        }
    return out


def regdoc_targets(narratives: list[dict[str, Any]], *, as_of: str | None = None,
                   window: int = 90) -> list[dict[str, Any]]:
    """ADR 009 Phase B input: [{instrument_key, label, url}] for tracked instruments,
    preferring an in.gov.br DOU citation (full text) among the instrument's cards."""
    as_of = as_of or run_date_today()
    horizon = int(_f("ONCA_REG_DEADLINE_HORIZON", DEADLINE_HORIZON))
    include_com = bool(int(_f("ONCA_REG_INCLUDE_COMUNICADOS", 0)))
    groups = threads(narratives, as_of=as_of, recency=window,
                     include_comunicados=include_com, horizon=horizon)
    out: list[dict[str, Any]] = []
    for key, g in groups.items():
        num = _instrument_number(key)
        # DOU slugs write the number with the pt-BR thousands dot ("5.336"), so match the
        # number with an optional separator, bounded by non-digits.
        num_pat = None
        if num:
            core = num if len(num) < 4 else num[:-3] + r"\.?" + num[-3:]
            num_pat = re.compile(rf"(?<!\d){core}(?!\d)")
        dou_url = None
        for m in g.get("mentions") or []:
            for c in m.get("citations") or []:
                u = c.get("url") if isinstance(c, dict) else None
                if not u or "in.gov.br" not in str(u):
                    continue
                # Scope to THIS instrument: a thread aggregates cards that may cite other
                # acts' DOU pages — require the instrument number in the URL slug, else we'd
                # store the wrong document (nexus, cf. #51).
                if num_pat and num_pat.search(str(u)):
                    dou_url = u
                    break
            if dou_url:
                break
        if dou_url:
            out.append({"instrument_key": key, "label": g.get("label"), "url": dou_url})
    return out


def build_lifecycle_card(lc: dict[str, Any]) -> dict[str, Any]:
    """A feed-ready card for one instrument's lifecycle thread (grounded, labeled)."""
    def _fmt(d: str) -> str:
        try:
            return feature_store._parse(d).strftime("%d/%m")
        except Exception:
            return d

    score = _reg_score({"days_to_deadline": lc["days_to_deadline"]})
    dtd, deadline = lc["days_to_deadline"], lc["deadline"]
    alert = bool(deadline and dtd is not None and 0 <= dtd <= ALERT_WITHIN)
    arc = " → ".join(
        f"{STAGE_LABELS[s]} ({_fmt(next(m['date'] for m in lc['timeline'] if m['stage'] == s))})"
        for s in lc["stages_seen"]
    )
    industries = _industries_for(lc["domain"])
    ind_pt = ", ".join(_INDUSTRY_PT.get(s, s) for s in industries)
    changes = reg_change.parse_changes(
        " ".join((m.get("summary") or "") for m in lc.get("timeline") or [])[:3000],
        self_key=lc["instrument"])
    head = f"Ciclo regulatório: {lc['label']} — estágio atual {STAGE_LABELS[lc['current_stage']]}."
    if arc:
        head += f" Progressão: {arc}."
    if deadline:
        head += f" Prazo: vence em {_fmt(deadline)}" + (f" (faltam {dtd} dias)." if dtd is not None else ".")
    head += f" Afeta: {lc['domain']} (exposição provável: {ind_pt})."
    if changes:
        head += f" Mudanças: {reg_change.summarize_changes(changes)}."
    tail = (" Ciclo derivado do encadeamento de menções ao instrumento na fonte "
            "reguladora — estágio e urgência são inferência; cada menção cita a fonte.")

    citations = _instrument_citations(lc["instrument"], lc["label"], list(reversed(lc["timeline"])))
    return {
        "id": f"reg-lifecycle-{lc['instrument']}",
        "kind": "regulatory_lifecycle",
        "axis": "regulatory_lifecycle",
        "subject_type": "instrument",
        "instrument": lc["instrument"],
        "instrument_label": lc["label"],
        "domain": lc["domain"],
        "industries": industries,
        # #70: precise cohort for DISPLAY (chips); `industries` is overridden recall-first at feed-build for scoping.
        "affected_industries": industries,
        "changes": changes,
        "n_changes": len(changes),
        # ADR 009 §3: the LLM change-record (rated impact/blast/difficulty), when drafted.
        "change_record": lc.get("change_record"),
        "deadline": deadline,
        "days_to_deadline": dtd,
        "status": lc["status"],
        "current_stage": lc["current_stage"],
        "stages_seen": lc["stages_seen"],
        "n_developments": lc["n_dates"],
        "entity": None,
        "entities": [],
        "lenses": ["regulatory"],
        "is_alert": alert,
        "is_inference": True,
        "threat_score": score,
        "threat_factors": {"current_stage": lc["current_stage"],
                           "stages": len(lc["stages_seen"]), "n_dates": lc["n_dates"]},
        "threat_score_note": "estimated_v1_reg_lifecycle",
        "swot_hint": {"dimension": "T", "sign": "-", "instrument": lc["instrument"],
                      "domain": lc["domain"]},
        "narrative": head + tail,
        "citations": citations,
        "source_ids": [],
        "mode": "derived",
        "run_date": lc["last_updated"],
        "run_at": run_at_now(),
        "as_of": lc["last_updated"],
        "data_as_of": {"first_seen": lc["first_seen"], "last_updated": lc["last_updated"]},
    }


def publish_lifecycles(lifecycles: dict[str, dict[str, Any]], bucket: str, *,
                       s3: Any | None = None, as_of: str | None = None) -> int:
    """Overwrite reg_lifecycle/{id}.json (full) + reg_lifecycle/index.json (feed cards)."""
    import datetime as _dt
    s3 = s3 or boto3.client("s3")
    as_of = as_of or run_date_today()
    cards = []
    for key, lc in lifecycles.items():
        s3.put_object(
            Bucket=bucket, Key=f"{REG_LIFECYCLE_PREFIX}{key}.json",
            Body=json.dumps(lc, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json", CacheControl="no-cache",
        )
        cards.append(build_lifecycle_card(lc))
    cards.sort(key=lambda c: (c["run_date"], c["threat_score"]), reverse=True)
    s3.put_object(
        Bucket=bucket, Key=REG_LIFECYCLE_INDEX_KEY,
        Body=json.dumps({
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "as_of": as_of, "cards": cards,
        }, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json", CacheControl="no-cache",
    )
    return len(cards)


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

    # ADR 009 §3: rate changes with a bounded LLM (labeled inference). Gated OFF by default
    # (Bedrock cost). One shared budget across radar cards + lifecycles; the concrete
    # blast-radius n_entities comes from the registry industry counts, not the model.
    llm_on = os.environ.get("ONCA_REG_LLM", "false").lower() in ("1", "true", "yes")
    llm_budget = int(_f("ONCA_REG_LLM_MAX", 20))
    ind_counts: dict[str, int] = {}
    n_records = 0
    if llm_on:
        try:
            from src.synth import feature_store as _fs

            for inds in _fs.load_industry_map().values():
                for i in inds:
                    ind_counts[i] = ind_counts.get(i, 0) + 1
        except Exception as exc:  # pragma: no cover
            print(f"Warning: industry counts unavailable, §3 off: {exc}")
            llm_on = False

    keys: list[str] = []
    fired = {c["instrument"] for c in cands}
    for cand in cands:
        rec = None
        if llm_on and n_records < llm_budget:
            try:
                from src.synth import reg_change_record

                changes = reg_change.parse_changes(
                    " ".join((d.get("narrative") or "") for d in cand.get("drivers") or [])[:3000],
                    self_key=cand["instrument"])
                rec = reg_change_record.record_for(
                    cand.get("label", ""), cand.get("domain", ""), changes, ind_counts,
                    effective_date=cand.get("deadline"))
                if rec:
                    n_records += 1
            except Exception as exc:  # pragma: no cover
                print(f"Warning: radar change-record failed: {exc}")
        key = _write(build_narrative(cand, change_record=rec), digests_bucket, s3)
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

    # Wave 2: the full regulatory-lifecycle thread — stage progression per instrument.
    n_lifecycles = 0
    try:
        lifecycles = build_lifecycles(recent, as_of=run_date, window=window_days)
        # §3 continues on the lifecycles with the REMAINING shared budget (radar cards drew
        # from it first).
        if llm_on and n_records < llm_budget:
            try:
                from src.synth import reg_change_record

                n_records += reg_change_record.enrich_lifecycles(
                    lifecycles, industry_counts=ind_counts,
                    max_records=llm_budget - n_records)
            except Exception as exc:  # pragma: no cover - best-effort
                print(f"Warning: reg change-record drafting failed: {exc}")
        if llm_on:
            print(f"reg change-records: drafted={n_records}")
        n_lifecycles = publish_lifecycles(lifecycles, digests_bucket, s3=s3, as_of=run_date)
    except Exception as exc:  # pragma: no cover - best-effort
        print(f"Warning: regulatory lifecycle publish failed: {exc}")

    # ADR 009 Phase B: fetch + version the full text of tracked instruments into the
    # regdocs/ store (the diff enabler). Gated OFF by default (network fetch of DOU pages);
    # content-hash-cached so steady-state runs only write changed/new docs.
    regdocs = None
    if os.environ.get("ONCA_REGDOCS", "false").lower() in ("1", "true", "yes"):
        try:
            from src.ingest import reg_documents

            bucket = os.environ.get("ONCA_RAW_BUCKET") or digests_bucket
            targets = regdoc_targets(recent, as_of=run_date, window=window_days)
            regdocs = reg_documents.sync_documents(targets, bucket, s3=s3, as_of=run_date)
            print(f"regdocs: targets={regdocs['targets']} stored={len(regdocs['stored'])} "
                  f"unchanged={regdocs['unchanged']} skipped={regdocs['skipped']} "
                  f"errors={len(regdocs['errors'])}")
        except Exception as exc:  # pragma: no cover - best-effort, never blocks
            print(f"Warning: regdocs sync failed: {exc}")

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "status": "ok",
                "as_of": run_date,
                "window_days": window_days,
                "nominated": len(cands),
                "emitted": len(keys),
                "lifecycles": n_lifecycles,
                "change_records": n_records,
                "regdocs": ({"stored": len(regdocs["stored"]), "unchanged": regdocs["unchanged"],
                             "skipped": regdocs["skipped"], "errors": len(regdocs["errors"])}
                            if regdocs else None),
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
