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

# --- source trust tiers (option B: double-validation) ---------------------
# Map a raw ingestion source label to a trust "kind". A CVM Fato Relevante is
# self-reported to the regulator (regulatory-grade); news is single-outlet unless
# corroborated; a DataJud process is a court record (but party-scrubbed, so it only
# ever *corroborates* a name established elsewhere).
_REGULATORY_SOURCES = ("cvm", "fato", "ipe", "bcb", "cvm-fatorelevante")
_COURT_SOURCES = ("datajud", "cnj", "tribunal")


def source_kind(source: Any) -> str:
    s = _norm(source)
    if any(k in s for k in _REGULATORY_SOURCES):
        return "regulatory"
    if any(k in s for k in _COURT_SOURCES):
        return "court"
    return "news"


def _publisher(url: Any) -> str:
    """Coarse publisher key from a url (domain) for news-corroboration counting."""
    m = re.search(r"https?://([^/]+)/?", str(url or ""))
    host = (m.group(1) if m else "").lower().lstrip("www.")
    return host or "?"


def compute_confidence(sources: list[dict[str, Any]]) -> str:
    """Grade a record from its accumulated sources (most trusted wins):
      - ``regulatory`` — a CVM/regulator filing (single trusted source suffices);
      - ``curated``    — an analyst-seeded record (option C);
      - ``corroborated`` — >= 2 INDEPENDENT signals agree (a court record + news,
        or >= 2 distinct news publishers);
      - ``reported``   — a single news outlet (provisional).
    """
    kinds = {s.get("kind") for s in sources}
    if "regulatory" in kinds:
        return "regulatory"
    if "curated" in kinds:
        return "curated"
    news_pubs = {s.get("publisher") for s in sources if s.get("kind") == "news"}
    independent = len(news_pubs) + (1 if "court" in kinds else 0)
    if independent >= 2:
        return "corroborated"
    return "reported"


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
    # #354: the BCB resolution regimes (Lei 6.024/1974). Checked before RJ/falência — a
    # liquidation headline often mentions a later "falência" as an outcome. Deliberately NO
    # bare "liquidacao" (asset/financial-settlement sense) and NO bare "intervencao".
    "liquidacao_extrajudicial": {
        "label": "Liquidação Extrajudicial",
        "patterns": [
            r"liquidacao extrajudicial",
            r"\bdecret\w+\s+(?:a\s+)?liquidacao\b",
            r"\bem\s+liquidacao\b(?=.*\b(?:banco central|bc|bacen)\b)",
        ],
    },
    "raet": {
        "label": "Regime de Administração Especial Temporária (RAET)",
        "patterns": [r"regime de administracao especial temporaria", r"\braet\b"],
    },
    "intervencao": {
        "label": "Intervenção do Banco Central",
        "patterns": [
            r"\bdecret\w+\s+(?:a\s+)?intervencao\b",
            r"\bintervencao\b(?=.*\b(?:banco central|bc|bacen)\b)",
            r"\b(?:banco central|bacen)\b.*\bintervencao\b",
            r"\bsob\s+intervencao\b",
        ],
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
    # #358: not a headline kind — an institution that left the BCB "em funcionamento"
    # register between two snapshots. A FACT about the register, never an interpretation
    # (it may be a cancellation, a liquidation or a merger).
    "saida_cadastro_bcb": {
        "label": "Deixou de constar como em funcionamento no BCB",
        "patterns": [],
    },
}

# Headline classification order: most specific first.
_CLASSIFY_ORDER = ("recuperacao_extrajudicial", "liquidacao_extrajudicial", "raet",
                   "intervencao", "recuperacao_judicial", "falencia")

# Severity for entity_status (higher = worse). Group-level records (#356) and correspondent
# rows (#357) never outrank an entity's OWN record.
SEVERITY = {"falencia": 6, "liquidacao_extrajudicial": 6, "raet": 5, "intervencao": 5,
            "recuperacao_judicial": 4, "recuperacao_extrajudicial": 3, "saida_cadastro_bcb": 2}

_COMPILED = {
    kind: [re.compile(p) for p in spec["patterns"]]
    for kind, spec in DISTRESS_KINDS.items()
}


def classify_distress(title: str) -> str | None:
    """Return the distress kind a headline signals, or None. Most-specific first."""
    t = _norm(title)
    if not t:
        return None
    for kind in _CLASSIFY_ORDER:
        for rx in _COMPILED[kind]:
            if rx.search(t):
                return kind
    return None


# #365: STATUS mentions — once a regime is decreed, the news stops saying "decreta liquidação"
# and names the standing status instead ("Liquidante do Banco Master diz…", "FGC paga credores
# do Will Bank", "Will Bank quebrou"). These never CREATE a record (a status phrase is weaker
# than an event and "quebra"/"credores" have everyday senses); they only refresh an entity's
# EXISTING trusted resolution / falência record.
_STATUS_PATTERNS = [re.compile(p) for p in (
    r"\bliquidante\s+d[oa]s?\b",
    r"\binterventor\s+d[oa]s?\b",
    r"\bmassa\s+(?:liquidanda|falida)\b",
    r"\bem\s+liquidacao\b",
    r"\bliquidad[oa]s?\s+pelo\s+(?:banco central|bc|bacen)\b",
    r"\bcredores\s+d[oa]s?\b(?=.*\b(?:fgc|liquid\w*)\b)|\b(?:fgc|liquid\w*)\b.*\bcredores\s+d[oa]s?\b",
    r"\b(?:quebra|colapso)\s+d[oa]s?\b",
    r"\b(?:quebrou|faliu)\b",
)]
STATUS_REFRESH_KINDS = frozenset({"liquidacao_extrajudicial", "intervencao", "raet", "falencia"})


def status_mention(title: str) -> bool:
    """True when a headline refers to a standing resolution / bankruptcy STATUS (#365)."""
    t = _norm(title)
    return bool(t) and any(rx.search(t) for rx in _STATUS_PATTERNS)


def refresh_status(index: dict[str, Any], mentions: list[dict[str, Any]]) -> int:
    """Fold status mentions into EXISTING own, trusted resolution / falência records only:
    ``status_mentions`` count (new evidence ids), ``last_status_*`` and ``last_seen``. Never
    creates a record and never adds a confidence source. Returns records touched."""
    touched = 0
    recs = index.get("records") or {}
    for m in mentions:
        for rec in recs.values():
            if (rec.get("entity") != m.get("entity") or (rec.get("role") or "self") != "self"
                    or rec.get("kind") not in STATUS_REFRESH_KINDS
                    or rec.get("confidence") not in TRUSTED_CONFIDENCE):
                continue
            ev_id = m.get("evidence_id") or m.get("url") or m.get("title")
            seen = list(rec.get("status_evidence") or [])
            if ev_id in seen:
                continue
            rec["status_evidence"] = (seen + [ev_id])[-5:]
            rec["status_mentions"] = int(rec.get("status_mentions") or 0) + 1
            if str(m.get("date") or "") >= str(rec.get("last_status_date") or ""):
                rec.update(last_status_date=m.get("date"), last_status_title=m.get("title"),
                           last_status_url=m.get("url"))
            if str(m.get("date") or "") > str(rec.get("last_seen") or ""):
                rec["last_seen"] = m["date"]
            touched += 1
    return touched


def distress_span(title: str) -> tuple[str, int, int] | None:
    """``(kind, start, end)`` of the first distress phrase in the accent-folded title, or None.
    #355 uses it as a subject-position guard: an untracked name counts only when it is the
    clause subject — named BEFORE the phrase ("Grupo X pede recuperação judicial") or as its
    genitive object right AFTER it ("decreta liquidação extrajudicial da X")."""
    t = _norm(title)
    kind = classify_distress(title)
    if not kind:
        return None
    ms = sorted((m for rx in _COMPILED[kind] for m in [rx.search(t)] if m), key=lambda m: m.start())
    return (kind, ms[0].start(), ms[0].end()) if ms else (kind, 0, 0)


# --- subject binding (defamation guard) -----------------------------------
# A distress record must attach to the *subject* of the distress clause, not to
# every entity merely co-mentioned in the headline. Three false-positive classes
# were poisoning the store (issues #33, #55):
#
# 1. Market operators / regulators ACT ON listed companies (delist, sanction,
#    supervise) — a news headline never makes THEM the distressed party. They can
#    still legitimately appear as the resolved entity, so they are dropped as a
#    distress *subject* specifically (denylist), not from the corpus.
NON_DISTRESS_SUBJECTS = frozenset({
    "b3", "cvm", "bcb", "bacen", "anbima", "susep", "previc", "coaf", "cade",
    "febraban", "abecs",
})

# 2. "<X> exclui/sanciona <Y> ... por/após recuperação" — the distress is a
#    *reason* for X's action and belongs to the object Y, not the acting subject.
#    We can't reliably tell actor from object by position in a bare RSS title, so
#    precision-first: when a headline is an action-taken-over-distress shape, we do
#    not attribute the distress to any co-mentioned entity (better a miss than a
#    false RJ). Genuine "<empresa> pede/entra em recuperação" headlines carry no
#    such actor verb and are unaffected.
_ACTOR_OVER_DISTRESS = re.compile(
    r"\b(exclui\w*|retir\w*|remov\w*|tir(a|ou)|cort(a|ou)|suspend\w*|rebaix\w*|"
    r"barr(a|ou)|notific\w*|process\w*|acion\w*|sancion\w*|puni\w*|mult\w*|"
    r"investig\w*|desenquadr\w*)\b.*\b(por|pela|apos|devido|em razao de|"
    r"por conta de|diante d)\w*\b"
)


def _actor_headline(norm_title: str) -> bool:
    """True when the headline describes an entity ACTING on another party in a
    clause whose reason is the distress event (issue #33: 'B3 exclui Braskem ...
    por recuperação extrajudicial') — subject is ambiguous, so don't attribute."""
    return bool(_ACTOR_OVER_DISTRESS.search(norm_title))


# 3. Creditor / exposure framing (issue #55) — the tracked entity is a CREDITOR
#    OF, or FINANCIALLY EXPOSED TO, a distressed debtor, not the distressed party
#    itself. E.g. "Cliente do Banco do Brasil entra em recuperação judicial",
#    "BB tem exposição a empresa em recuperação judicial", "Banco executa dívida
#    de X em RJ". The distress is the debtor's — usually a non-FS company, and
#    caught by its own direct headline elsewhere — never the bank's. Precision-
#    first, like guard 2: attribute to no one.
#
#    The cues are chosen so a bank's OWN distress headline is NOT suppressed:
#    - a POSSESSIVE creditor link ("cliente/devedor/tomador ... do/da/de <banco>")
#      requires the preposition, so "Empresa devedora pede RJ" (subject = empresa)
#      is untouched; only "devedora DO <banco>" (bank = creditor) is caught;
#    - EXPOSURE/loss language ("exposição", "provisão", "calote", "inadimplência")
#      a self-distress headline would not use about itself.
#    Bare "credores" is deliberately NOT a cue — a distressed company negotiating
#    with *its* creditors ("Banco Y em RJ negocia com credores") must still count.
_CREDITOR_FRAMING = re.compile(
    r"\b(?:client\w+|correntist\w+|devedor\w*|tomador\w+|mutuari\w+)\s+d[eoa]s?\b"
    r"|\b(?:exposic\w+|expost\w+|provis(?:ao|oes|ion\w*)|calote\w*|inadimplenc\w+)\b"
    r"|\bexecu\w+\b(?=.*\bdivida\b)|\bdivida\b(?=.*\bexecu\w+)"
    r"|\bcobranc\w+\s+d[eoa]s?\s+divida"
)


def _creditor_headline(norm_title: str) -> bool:
    """True when the headline frames the tracked entity as a CREDITOR of / EXPOSED
    to a distressed debtor rather than the distressed party itself (issue #55:
    'Cliente do Banco do Brasil ... recuperação judicial'). Precision-first: the
    distress belongs to the debtor, so attribute to no one."""
    return bool(_CREDITOR_FRAMING.search(norm_title))


# 4. Counterparty framing (issue #73) — the distress SUBJECT is another (usually non-tracked)
#    party and the tracked entity is named only as a COUNTERPARTY-OBJECT of that party's action:
#    a beneficiary ("Casas Bahia aprova garantia ... AO Banco Digio"), a legal target ("Casas
#    Bahia desiste de pedidos CONTRA Banco do Brasil em recuperação judicial"), etc. The RJ
#    belongs to the acting party, never the bank named after the preposition. Precision-first:
#    a genuine self-distress headline ("Banco X pede recuperação judicial") never prefixes the
#    bank with contra/ao/perante/junto a/em favor de, so it is unaffected.
_COUNTERPARTY_FRAMING = re.compile(
    r"\b(?:contra|perante|junto\s+a[os]?|em\s+favor\s+d[eoa]s?|ao|aos)\s+"
    r"(?:[oa]s?\s+)?(?:banco|financeira|seguradora|corretora|fintech|instituicao|"
    r"administradora|gestora|cooperativa|consorcio)\w*"
)


def _counterparty_headline(norm_title: str) -> bool:
    """True when the tracked entity appears only as a prepositional COUNTERPARTY of another
    party's action in a distress headline (issue #73). Attribute to no one."""
    return bool(_COUNTERPARTY_FRAMING.search(norm_title))


def label_for(kind: str) -> str:
    return DISTRESS_KINDS.get(kind, {}).get("label", kind)


# Mid-sentence form ("Grupo econômico em …", "Correspondente em …").
_PHRASES = {"recuperacao_judicial": "recuperação judicial",
            "recuperacao_extrajudicial": "recuperação extrajudicial",
            "falencia": "falência", "liquidacao_extrajudicial": "liquidação extrajudicial",
            "raet": "RAET", "intervencao": "intervenção do Banco Central",
            "saida_cadastro_bcb": "saída do cadastro do BCB"}


def phrase_for(kind: str) -> str:
    return _PHRASES.get(kind, label_for(kind).lower())


def detect_distress_events(
    news_items: Iterable[dict[str, Any]],
    *,
    resolver: Callable[[dict[str, Any]], list[str]],
    today: dt.date | None = None,
    unresolved: list[dict[str, Any]] | None = None,
    status: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Scan news items for distress headlines that resolve to a tracked entity.

    Returns one event per (entity, item): a title must both classify as distress
    AND resolve to at least one registry entity — a bare mention never qualifies.
    #355: a headline that classifies, passes every guard, but resolves to NO tracked entity
    is appended to ``unresolved`` (when given) for the wider distress watch.
    """
    today = today or dt.date.today()
    out: list[dict[str, Any]] = []
    for item in news_items or []:
        title = item.get("title") or item.get("subject") or ""
        kind = classify_distress(title)
        if not kind:
            # #365: a status mention — passes the same guards, resolved, refresh-only.
            if status is not None and status_mention(title):
                nt = _norm(title)
                if not (_actor_headline(nt) or _creditor_headline(nt)
                        or _counterparty_headline(nt) or _transaction_headline(nt)):
                    try:
                        ents = [e for e in (resolver(item) or []) if e not in NON_DISTRESS_SUBJECTS]
                    except Exception:  # pragma: no cover
                        ents = []
                    for eid in ents:
                        status.append({"entity": eid, "title": title.strip(), "url": item.get("url"),
                                       "date": str(item.get("date") or today.isoformat())[:10],
                                       "evidence_id": item.get("id")})
            continue
        norm_title = _norm(title)
        # Guard 2: action-over-distress headlines have an ambiguous subject — the
        # distress belongs to the object of the action, not the (usually resolved)
        # actor. Precision-first: attribute to no one. (issue #33)
        # Guard 3: creditor/exposure framing — the resolved entity is a creditor
        # of / exposed to a distressed debtor, not the distressed party. (issue #55)
        # Guard 4: counterparty framing — the entity is only a prepositional counterparty
        # (ao/contra/perante/…) of another party's action; the RJ is that party's. (issue #73)
        # Guard 5: transaction / time-reference framing — the entity did business with a party
        # that filed; the RJ is that party's. (issue #296)
        if (_actor_headline(norm_title) or _creditor_headline(norm_title)
                or _counterparty_headline(norm_title) or _transaction_headline(norm_title)):
            continue
        try:
            entities = resolver(item) or []
        except Exception:  # pragma: no cover - resolver best-effort
            entities = []
        # Guard 1: market operators / regulators are never the distressed party.
        entities = [e for e in entities if e not in NON_DISTRESS_SUBJECTS]
        if not entities:
            if unresolved is not None:
                unresolved.append(item)
            continue
        date = str(item.get("date") or today.isoformat())[:10]
        src = item.get("source") or "News"
        for eid in entities:
            out.append({
                "entity": eid,
                "kind": kind,
                "label": label_for(kind),
                "date": date,
                "title": title.strip(),
                "url": item.get("url"),
                "source": src,
                "source_kind": source_kind(src),
                "publisher": _publisher(item.get("url")),
                "evidence_id": item.get("id"),
            })
    return out


# 5. Transaction / time-reference framing (issue #296) — the tracked entity DID BUSINESS WITH a
#    party that later filed, and the headline dates that business against the OTHER party's
#    filing: "Banco Daycoval financiou e comprou ativos da Tradener nos 90 dias antes do pedido
#    de recuperação judicial". The RJ is Tradener's. Two cues, either is enough:
#    - a TIME REFERENCE to a filing ("antes/depois do pedido de recuperação", "nos N dias antes
#      do pedido"): news dates events against someone else's filing, not a bank's own;
#    - a TRANSACTION with a POSSESSIVE object ("comprou/financiou/adquiriu ... ativos DA <X>").
#      Only da/do/das/dos, never "de", so "Banco Y em RJ vende carteira de créditos" (its own
#      book) still counts.
#    Precision-first, like guards 2–4: attribute to no one.
_TIME_REF_TO_FILING = re.compile(
    r"\b(?:antes|depois|apos)\s+d[oa]\s+(?:pedido|processo|ajuizamento)\s+de\s+"
    r"(?:recuperacao|falencia)"
    r"|\bnos\s+\d+\s+dias\s+(?:antes|anteriores)"
)
_TRANSACTION_WITH_OTHER = re.compile(
    r"\b(?:financi|compr|adquir|vend|empresto|cede|cedeu|assum|refinanci)\w*\b[^.]{0,40}?"
    r"\b(?:ativos?|carteiras?|creditos?|dividas?|participac\w+|recebiveis|acoes|imoveis)\s+d[ao]s?\b"
)


def _transaction_headline(norm_title: str) -> bool:
    """True when the tracked entity is a party that transacted with a distressed company, or
    the headline dates something against another party's filing (issue #296)."""
    return bool(_TIME_REF_TO_FILING.search(norm_title) or _TRANSACTION_WITH_OTHER.search(norm_title))


def _src_entry(ev: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": ev.get("source_kind") or source_kind(ev.get("source")),
        "source": ev.get("source"),
        "publisher": ev.get("publisher") or _publisher(ev.get("url")),
        "url": ev.get("url"),
        "date": ev["date"],
    }


# Optional per-event fields carried onto a record (set-once unless the event has a value).
_CARRY = ("role", "subject", "subject_name", "cnpj", "segment", "matched_via", "brand",
          "group_of", "correspondent_of", "correspondent_checked", "promoted_to", "review_id",
          # #362: official resolution-regime facts (BCB "Regimes de resolução")
          "regime_id", "regime_status", "regime_end", "act", "responsavel")


def _fold_event(records: dict[str, dict[str, Any]], key: str, ev: dict[str, Any]) -> None:
    date = ev["date"]
    rec = records.get(key)
    ev_id = ev.get("evidence_id")
    if rec is None:
        sources = [_src_entry(ev)]
        rec = records[key] = {
            "entity": ev.get("entity"),
            "kind": ev["kind"],
            "label": ev["label"],
            "first_seen": date,
            "last_seen": date,
            "latest_title": ev["title"],
            "latest_url": ev.get("url"),
            "mentions": 1,
            "sources": sources,
            "confidence": compute_confidence(sources),
            "evidence": [e for e in [ev_id] if e][:5],
        }
    else:
        # #354: the distress sweep re-reads the same headline every run within its lookback,
        # so a mention counts only when its evidence id is NEW (an id-less event still counts).
        if not ev_id or ev_id not in (rec.get("evidence") or []):
            rec["mentions"] = int(rec.get("mentions", 0)) + 1
        if date < rec.get("first_seen", date):
            rec["first_seen"] = date
        if date >= rec.get("last_seen", ""):
            rec["last_seen"] = date
            rec["latest_title"] = ev["title"]
            rec["latest_url"] = ev.get("url")
            rec["label"] = ev["label"]
        # Accumulate a NEW independent source (dedup by kind+publisher) and
        # re-grade confidence — this is the double-validation upgrade path.
        sources = list(rec.get("sources") or [])
        ent = _src_entry(ev)
        if not any(s.get("kind") == ent["kind"] and s.get("publisher") == ent["publisher"]
                   for s in sources):
            sources.append(ent)
        rec["sources"] = sources[:8]
        rec["confidence"] = compute_confidence(rec["sources"])
        if ev_id and ev_id not in (rec.get("evidence") or []):
            rec["evidence"] = ((rec.get("evidence") or []) + [ev_id])[-5:]
    for f in _CARRY:
        if ev.get(f) not in (None, "", []):
            rec[f] = ev[f]
    # #362: a standing official fact (an ACTIVE BCB regime) is re-observed every run — dated at
    # its decree, but alive while the regulator still lists it (no TTL expiry, no new mention).
    obs = ev.get("observed")
    if obs and str(obs) > str(rec.get("last_seen") or ""):
        rec["last_seen"] = str(obs)[:10]
        rec["observed"] = str(obs)[:10]


def record_key(ev: dict[str, Any]) -> str:
    """The index key of a TRACKED event: own records ``<entity>#<kind>``; a group (#356) or
    correspondent row ``<entity>#<role>:<kind>`` so it never merges into the entity's own."""
    role = ev.get("role") or "self"
    return f"{ev['entity']}#{ev['kind']}" if role == "self" else f"{ev['entity']}#{role}:{ev['kind']}"


def untracked_key(ev: dict[str, Any]) -> str:
    """#355: an untracked institution is keyed by its CNPJ root when known, else its brand."""
    subj = ev.get("cnpj") and f"cnpj:{ev['cnpj']}" or f"brand:{ev.get('brand') or ev.get('subject')}"
    return f"{subj}#{ev['kind']}"


def merge_distress(
    existing: dict[str, Any] | None,
    events: list[dict[str, Any]],
    *,
    today: dt.date | None = None,
    ttl_days: int = DEFAULT_TTL_DAYS,
    untracked: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Upsert distress events into the index, keyed by (entity, kind).

    Keeps first_seen/last_seen, latest title/url, an evidence sample and a mention
    count; prunes records untouched for `ttl_days`. Escalation to `falencia` is
    tracked as its own record (a company can appear under both).

    #355: ``untracked`` events (institutions not in the registry, resolved against the BCB
    register / review queue) fold into a separate ``untracked`` map, keyed by CNPJ root.
    A group-level event (#356, ``role: group``) is keyed ``<entity>#group:<kind>`` so it
    never merges into — or masquerades as — the entity's OWN record.
    """
    today = today or dt.date.today()
    idx = dict(existing or {})
    records: dict[str, dict[str, Any]] = dict(idx.get("records") or {})
    other: dict[str, dict[str, Any]] = dict(idx.get("untracked") or {})

    for ev in events:
        _fold_event(records, record_key(ev), ev)
    for ev in untracked or []:
        _fold_event(other, untracked_key(ev), ev)

    # Prune stale records (nothing referenced them within the TTL window).
    cutoff = (today - dt.timedelta(days=ttl_days)).isoformat()

    def _live(m: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {k: r for k, r in m.items() if str(r.get("last_seen") or "") >= cutoff}

    pruned, pruned_other = _live(records), _live(other)
    out = {
        "as_of": today.isoformat(),
        "count": len(pruned),
        "records": pruned,
    }
    if pruned_other or "untracked" in idx:
        out["untracked"] = pruned_other
        out["untracked_count"] = len(pruned_other)
    for k in ("exits_seen",):  # #358 bookkeeping survives a merge
        if k in idx:
            out[k] = idx[k]
    return out


# --- entity-facing accessor ----------------------------------------------

def _rank(rec: dict[str, Any]) -> tuple[int, int]:
    own = 1 if (rec.get("role") or "self") == "self" else 0
    return own, SEVERITY.get(rec.get("kind"), 0)


def entity_status(index: dict[str, Any], entity_id: str) -> dict[str, Any] | None:
    """The most severe active distress record for an entity, or None.

    Severity: falência / liquidação > RAET / intervenção > RJ > extrajudicial > register
    exit. An entity's OWN record always outranks a group (#356) or correspondent (#357) one.
    """
    best: dict[str, Any] | None = None
    for rec in (index.get("records") or {}).values():
        if rec.get("entity") != entity_id:
            continue
        if best is None or _rank(rec) > _rank(best):
            best = rec
    return best


# Confidence grades an executive / board surface may state as fact (the #33 guardrail):
# a single-outlet `reported` headline stays off them.
TRUSTED_CONFIDENCE = frozenset({"regulatory", "curated", "corroborated", "confirmed"})


def _with_ids(rec: dict[str, Any], key: str, tracked: bool) -> dict[str, Any]:
    r = dict(rec)
    # `date` + a stable `id` = the FIRST sighting: push (alert_events) fires once per event,
    # not again on every later mention. Before, records had no `date`, so push never fired.
    r.setdefault("date", r.get("first_seen"))
    r["id"] = f"distress:{key}:{r.get('first_seen')}"
    r["tracked"] = tracked
    r.setdefault("role", "self")
    return r


def list_records(index: dict[str, Any]) -> list[dict[str, Any]]:
    """Flat, sorted (most-recent first) list of distress records for the feed.

    #357: a record with ``correspondent_of`` links fans out one extra row per contracting
    TRACKED institution (``entity`` = that bank, ``role: correspondent``), so a tenant scoped
    to the bank sees its correspondent's distress. #355: untracked institutions carry
    ``entity: None`` and ``tracked: False`` — visible on the unscoped (owner/curator) feed only.
    """
    recs: list[dict[str, Any]] = []
    for src, tracked in (("records", True), ("untracked", False)):
        for k, r in (index.get(src) or {}).items():
            base = _with_ids(r, k, tracked)
            if not tracked:
                base["entity"] = None
            recs.append(base)
            for link in r.get("correspondent_of") or []:
                row = dict(base)
                row.update(entity=link.get("entity"), role="correspondent", tracked=True,
                           correspondent=link.get("correspondente"),
                           correspondent_cnpj=link.get("cnpj"),
                           subject_name=r.get("subject_name") or r.get("entity"),
                           label=f"Correspondente em {phrase_for(r.get('kind') or '')}",
                           id=f"{base['id']}:corr:{link.get('entity')}")
                recs.append(row)
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


def _digest_items(digest: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    news = digest.get("news") if isinstance(digest, dict) else None
    if isinstance(news, dict):
        # #354: distress_items = the sweep over EVERY fetched headline (incl. outlet items
        # naming no tracked term) — the Euro17 RJ headline was fetched and dropped before.
        items += ((news.get("items") or []) + (news.get("context") or [])
                  + (news.get("distress_items") or []))
    fatos = digest.get("fatos") if isinstance(digest, dict) else None
    if isinstance(fatos, dict):
        items += (fatos.get("items") or []) + (fatos.get("context") or [])
    seen: set[str] = set()
    out = []
    for it in items:
        k = str(it.get("id") or it.get("url") or it.get("title") or "")
        if k and k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def update_from_digest(
    digest: dict[str, Any],
    bucket: str,
    *,
    resolver: Callable[[dict[str, Any]], list[str]] | None = None,
    s3: Any | None = None,
    today: dt.date | None = None,
    watch: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """End-to-end distress update from a run's digest.

    Mines BOTH the **trusted CVM Fato Relevante** stream (option A — a listed
    issuer's own regulator-filed RJ, `regulatory` confidence) AND the **news**
    stream (which adds `reported`/`corroborated` coverage, incl. private companies
    CVM never sees). Merges into the durable index and persists. Best-effort.

    #355–#358: ``watch(unresolved, own_events, index)`` (default: ``distress_watch.run_live``
    when ``s3`` access is available) resolves the headlines no tracked entity matched, adds
    group / correspondent / register-exit events, and may promote parked candidates.
    """
    if resolver is None:
        from src.synth.entities import resolve_entities as resolver  # lazy
    items = _digest_items(digest)
    unresolved: list[dict[str, Any]] = []
    status: list[dict[str, Any]] = []
    events = detect_distress_events(items, resolver=resolver, today=today, unresolved=unresolved,
                                    status=status)
    index = load_index(bucket, s3=s3)
    watched: dict[str, Any] = {"tracked": [], "untracked": [], "summary": {}}
    import os

    if watch is None and os.environ.get("ONCA_ENTITIES_TABLE") and \
            os.environ.get("ONCA_DISTRESS_WATCH", "on") != "off":
        import boto3

        from src.synth import distress_watch

        _s3 = s3 or boto3.client("s3")

        def watch(u, own, idx):  # noqa: E306 - the live default
            return distress_watch.run_live(u, own, idx, bucket, s3=_s3, today=today)
    try:
        if watch is not None:
            watched = watch(unresolved, events, index) or watched
    except Exception as exc:  # pragma: no cover - the watch is additive, never fatal
        print(f"Warning: distress watch skipped: {exc}")
    merged = merge_distress(index, events + (watched.get("tracked") or []), today=today,
                            untracked=watched.get("untracked") or [])
    refreshed = refresh_status(merged, status)  # #365: after the merge, so a regime just seen counts
    publish(merged, bucket, s3=s3)
    by_kind: dict[str, int] = {}
    for e in events:
        by_kind[e["source_kind"]] = by_kind.get(e["source_kind"], 0) + 1
    return {"new_events": len(events), "records": merged.get("count", 0),
            "untracked": merged.get("untracked_count", 0), "by_source": by_kind,
            "status_mentions": len(status), "status_refreshed": refreshed,
            "watch": watched.get("summary") or {}}


# Back-compat: news-only entry (kept; callers/tests may still use it).
def update_from_news(
    news_items: Iterable[dict[str, Any]],
    bucket: str,
    *,
    resolver: Callable[[dict[str, Any]], list[str]] | None = None,
    s3: Any | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    if resolver is None:
        from src.synth.entities import resolve_entities as resolver  # lazy
    events = detect_distress_events(news_items, resolver=resolver, today=today)
    index = load_index(bucket, s3=s3)
    merged = merge_distress(index, events, today=today)
    publish(merged, bucket, s3=s3)
    return {"new_events": len(events), "records": merged.get("count", 0)}


# --- option C: one-time curated seed --------------------------------------
# Analyst-curated seed of tracked entities in a *known, public* distress process.
# Deliberately EMPTY by default: asserting that a real company is in RJ/falência is
# a defamation-sensitive, accuracy-critical claim, so records must come from a
# regulator filing (option A), corroborated press (option B), or a vetted analyst
# entry here — never a fabricated guess. Populate as
#   {"entity": "<registry_id>", "kind": "recuperacao_judicial",
#    "date": "YYYY-MM-DD", "title": "...", "url": "https://...vetted-source"}
SEED_DISTRESS: list[dict[str, Any]] = []


def seed_distress(
    bucket: str,
    seed: list[dict[str, Any]] | None = None,
    *,
    s3: Any | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Fold the curated seed into the durable index at `curated` confidence
    (idempotent). Records seeded here are marked source_kind=curated so a later
    regulator filing or corroborating press upgrades their confidence."""
    seed = SEED_DISTRESS if seed is None else seed
    events = []
    for s in seed:
        kind = s.get("kind") or "recuperacao_judicial"
        events.append({
            "entity": s["entity"], "kind": kind, "label": label_for(kind),
            "date": str(s.get("date") or (today or dt.date.today()).isoformat())[:10],
            "title": s.get("title") or label_for(kind), "url": s.get("url"),
            "source": s.get("source") or "curated", "source_kind": "curated",
            "publisher": _publisher(s.get("url")), "evidence_id": s.get("id"),
        })
    index = load_index(bucket, s3=s3)
    merged = merge_distress(index, events, today=today)
    publish(merged, bucket, s3=s3)
    return {"seeded": len(events), "records": merged.get("count", 0)}
