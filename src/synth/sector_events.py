"""Industry-level regulatory events (#177, part of incident #173).

**Why this exists.** Every narrative Onça synthesises is bound to an *entity*. A sector-wide
measure that names no operator — MP 1.394 banning online betting on 2026-09-25 — had nowhere
to go: it surfaced as one sentence inside B3's narrative, the CRO betting scope stayed at
``reg_threat 25.0`` and the Ask agent had nothing to cite. This module adds the missing object:
a durable **sector event** keyed by *industry*, not entity.

An event is created when either holds:

1. **≥1 official act** — a DOU / federal-act item (producers #174/#175) that carries
   ``industries: [slug, …]`` (and optionally ``severity``). A primary act (MP, Lei, Decreto,
   Portaria…) anchors the event; secondary acts that *reference* it (the Presidência despacho
   forwarding the MP to Congress) are attached as extra sources, not separate events.
2. **≥2 independent outlets** (distinct publishers) reporting the same sector-wide change —
   a headline that names the SECTOR (``bets``, ``casas de apostas``, ``criptoativos``…, or a
   sector-query news item tagged with ``industries``, #176) together with change vocabulary
   (``proíbe``/``proibição``/``suspende``/``revoga``/``medida provisória``/``novo marco``…).

Precision guards (tested on the real 09-23..09-26 headlines):

- an **opinion poll** ("75% defendem proibição das bets") is not an event;
- a **protest / demand** ("MTST … cobra proibição das apostas online") is not an event;
- a **hypothetical / proposal** ("possível proibição", "projeto de lei", "estuda proibir") is not;
- an **operator-specific** action ("Justiça manda suspender plataformas da Pixbet") is not
  *sector-wide* — no sector noun, so it stays on the entity.

Content honesty: every event carries the real sources it was built from (official act first)
and nothing else. A news-only event is labelled ``confidence: "reported"``; the headline shown
is a verbatim source headline, never a synthesised claim.

Evidence accumulates across runs in ``sector_events/latest.json`` (a single outlet on one run
and a second outlet two runs later still corroborate each other — the news digest synth reads
carries only the latest slice). Pure core; S3/registry I/O are thin adapters.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unicodedata
from collections import Counter
from typing import Any, Iterable

from src.synth.text_pt import pt_count

STORE_KEY = "sector_events/latest.json"

#: Pending single-outlet reports wait this long for a second, independent outlet.
PENDING_DAYS = 10
#: Events are kept in the store this long after their last evidence.
RETENTION_DAYS = 120
#: A news report / secondary act joins an existing event of the same industry when its date
#: falls within this many days after the event's date (or up to 2 days before it).
MERGE_WINDOW_DAYS = 10
MIN_OUTLETS = 2

SEVERITIES = ("low", "medium", "high", "critical")
_SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}

#: Sources whose items are official acts (the producers of #174/#175 emit these).
OFFICIAL_SOURCES = ("dou", "planalto", "federal", "federal_acts", "federal-acts", "presidencia",
                    "camara", "senado", "lexml")

CHANGE_LABEL = {
    "ban": "proibição",
    "suspension": "suspensão",
    "illegal": "ilegalidade",
    "revocation": "revogação",
    "new_framework": "novo marco regulatório",
    "court_ruling": "decisão judicial",
    "act": "ato normativo",
}


def _fold(text: Any) -> str:
    t = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def _strip_html(text: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(text or ""))).strip()


# --- sector vocabulary --------------------------------------------------------------
# Collective nouns that name a SECTOR (not one operator). Patterns run on accent-folded,
# lowercase text. Kept deliberately conservative: a false sector mention only matters when it
# also carries change vocabulary, but these same terms feed the #178 volume series.
INDUSTRY_TERMS: dict[str, list[str]] = {
    "betting": [
        r"\bbets\b", r"\bapostas\b", r"\bapostas? (?:online|esportiv\w*|de quota fixa|virtuais|digitais)\b",
        r"\bcasas? de apostas?\b", r"\b(?:setor|mercado|segmento|industria) de apostas\b",
        r"\bsites? de apostas?\b", r"\bplataformas? de apostas?\b", r"\bjogos? de azar\b",
        r"\bigaming\b", r"\bloterias? de apostas?\b", r"\bonline (?:betting|gambling)\b",
        r"\bsports? betting\b", r"\bbookmakers\b",
    ],
    "crypto": [
        r"\bcriptomoedas?\b", r"\bcriptoativos?\b", r"\bativos virtuais\b",
        r"\bexchanges? de cripto\w*", r"\bmercado cripto\b", r"\bstablecoins?\b",
    ],
    "consorcio": [r"\bconsorcios\b", r"\badministradoras de consorcio"],
    "insurance": [r"\bseguradoras\b", r"\b(?:setor|mercado) de seguros\b", r"\bresseguradoras\b"],
    "fintech": [r"\bfintechs\b", r"\binstituicoes de pagamento\b"],
    "banking": [r"\bbancos\b", r"\bsetor bancario\b", r"\binstituicoes financeiras\b"],
    "real-estate-funds": [r"\bfundos imobiliarios\b", r"\bfiis\b"],
    "agri-funds": [r"\bfiagros?\b"],
    "acquiring": [r"\bmaquininhas\b", r"\bcredenciadoras\b", r"\badquirentes\b"],
    "closed-pension": [r"\bfundos de pensao\b", r"\befpcs\b"],
    "securitization": [r"\bsecuritizadoras\b"],
}
_INDUSTRY_RE = {k: re.compile("|".join(v)) for k, v in INDUSTRY_TERMS.items()}

#: Plain words a reader uses for each sector — carried on the Ask card so "o setor de apostas"
#: / "as bets" retrieves the betting event (the slug "betting" is not how anyone asks).
INDUSTRY_WORDS: dict[str, str] = {
    "betting": "apostas, bets, apostas esportivas, casas de apostas, apostas de quota fixa, iGaming",
    "crypto": "cripto, criptoativos, criptomoedas, ativos virtuais",
    "consorcio": "consórcios",
    "insurance": "seguros, seguradoras",
    "fintech": "fintechs, instituições de pagamento",
    "banking": "bancos, setor bancário",
    "real-estate-funds": "fundos imobiliários, FIIs",
    "agri-funds": "Fiagro, fundos do agro",
    "acquiring": "adquirência, maquininhas",
    "closed-pension": "fundos de pensão, previdência fechada",
    "securitization": "securitização",
}

# Change vocabulary → change type, in priority order (first hit wins).
_STRONG: list[tuple[str, re.Pattern]] = [
    ("ban", re.compile(r"\bproib(?:e|em|iu|iram|ida|idas|ido|idos|icao|icoes|ir|i-las|i-los|indo)\b"
                       r"|\bbanimento\b|\bbanid[ao]s?\b|\bbaniu\b|\bbane\b"
                       r"|\bveda(?:m|da|das|do|dos|cao)?\b"
                       r"|\b(?:bans?|banned|prohibits?|prohibited|prohibition|outlaws?|outlawed)\b")),
    ("suspension", re.compile(r"\bsuspen(?:de|dem|deu|deram|sao|so|sa|sos|sas|dida|didas|dido|didos)\b"
                              r"|\bsuspend(?:s|ed)?\b|\bsuspension\b")),
    ("illegal", re.compile(r"\b(?:torna|tornam|tornou|tornaram|passa a ser|passam a ser)\s+(?:\w+\s+)?ileg")),
    ("revocation", re.compile(r"\brevog(?:a|am|ou|aram|ada|adas|ado|ados|acao)\b|\brevoke[sd]?\b")),
    ("new_framework", re.compile(r"\bmedida provisoria\b|\bmp\s*(?:n[o°.]*\s*)?\d"
                                 r"|\bnovo marco\b|\bnova regulamentacao\b|\bregulamenta\b"
                                 r"|\bnew (?:framework|rules?)\b")),
]

# Rejections — each names WHY a headline with change vocabulary is still not an event.
_POLL_MARK = re.compile(r"\bpesquisa\b|\benquete\b|\blevantamento\b|\bsondagem\b|\bdatafolha\b"
                        r"|\bquaest\b|\bipec\b|\batlas\b|\bpoll\b|\bsurvey\b")
_PCT_OPINION = re.compile(r"\d{1,3}\s?%.{0,40}\b(?:defend\w*|apoi\w*|a favor|aprov\w*|querem|preferem"
                          r"|acham|consideram|dizem|support\w*|favor)\b")
_DEMAND = re.compile(r"\bprotest\w*|\bcobra(?:m)?\b|\bpede(?:m)?\b|\bpedido de\b|\breivindic\w*"
                     r"|\bmanifestac\w*|\bmanifestantes\b|\babaixo-assinado\b|\bdefende(?:m)?\b"
                     r"|\bpression\w*|\bexige(?:m)?\b|\bcalls? for\b|\burges?\b|\bpetition\w*")
_HYPOTHETICAL = re.compile(r"\bpossivel\b|\beventual\b|\bestud(?:a|am|o)\b|\bavalia(?:m)?\b|\bdiscut\w*"
                           r"|\bdebat\w*|\bpropo(?:e|em|sta|stas)\b|\bprojeto de lei\b|\bpl\s?\d"
                           r"|\bpode(?:m|ria|riam)? (?:proibir|suspender|banir|vetar)\b"
                           r"|\bquer(?:em)? (?:proibir|suspender|banir)\b|\bameac\w*"
                           r"|\bvai (?:proibir|suspender|banir)\b|\bdeve(?:m)? (?:proibir|suspender)\b"
                           r"|\bsera(?:o)? proibid\w*|\bcould\b|\bmay (?:ban|suspend)\b|\bproposal\b"
                           r"|\bbill\b|\bconsiders?\b"
                           r"|\bcaso (?:\w+ ){0,3}(?:sejam|seja|forem|for)\b"
                           r"|\bse (?:\w+ ){0,3}(?:forem|for) (?:proibid|suspens|banid)\w*")
# A denied / overturned measure is the absence of an event ("Justiça … nega suspensão").
_NEGATED = re.compile(r"\bnega(?:m|do|da|ou|ram)?\b|\brejeit\w*|\barquiv\w*|\bderrub\w* (?:a |o )?(?:proib|suspens|veto)"
                      r"|\bafasta\w* (?:a )?(?:proib|suspens)|\bdenie[sd]\b|\boverturn\w*"
                      # #286: a lifted / reverted measure ("Revertida suspensão da oferta…")
                      r"|\brevertid\w*|\brevog\w* (?:a |o )?(?:proib|suspens)|\blevant\w* (?:a )?suspens"
                      r"|\brestabelec\w*")
# A public actor — the difference between "governo suspende 14 casas de apostas" (regulatory) and
# "Coinbase suspende oito pares" (an operator's own business decision).
_PUBLIC_ACTOR = re.compile(r"\b(?:governo|fazenda|spa|secretaria|ministerio|ministro|bcb|banco central|cvm|susep"
                           r"|justica|stf|stj|tribunal|juiz|congresso|senado|camara|presidente|lula|planalto"
                           r"|receita|anatel|procon|regulador\w*|medida provisoria|mp|lei|decreto|portaria"
                           r"|resolucao|government|regulator\w*|court)\b")

_INSTRUMENT_RE = re.compile(
    r"\b(medida provisoria|lei complementar|lei|decreto(?:-lei)?|portaria(?: [a-z]{2,6})?|resolucao(?: [a-z]{2,6})?"
    r"|instrucao normativa|mp)\s+n[o°.]*\s*([\d][\d.]*)")
_INSTRUMENT_ABBR = {"medida provisoria": "mp", "lei complementar": "lc", "instrucao normativa": "in"}
# Secondary official items: they reference an act rather than being one. They join the act's
# event; on their own they never create an event unless their severity is high/critical.
_SECONDARY_ACT = re.compile(r"\b(?:despachos?|mensage(?:m|ns)|edital|editais|extrato|aviso|retificac\w*"
                            r"|citacao|intimacao|ato declaratorio)\b")
# Top-level federal instruments — the ones that can reshape a whole sector on their own.
_TOP_LEVEL_ACT = re.compile(r"^(?:medida provisoria|lei complementar|lei|decreto)\b")


def industries_in_text(text: Any) -> list[str]:
    """Industries whose SECTOR noun appears in ``text`` (folded match)."""
    t = _fold(text)
    return [ind for ind, rx in _INDUSTRY_RE.items() if rx.search(t)]


def change_type_of(text: Any) -> str | None:
    t = _fold(text)
    for kind, rx in _STRONG:
        if rx.search(t):
            return kind
    return None


def _operator_specific(t: str, company: str) -> bool:
    """The change targets ONE operator, not the sector: the sector noun is qualified by the
    item's own company ("plataformas de apostas da Pixbet", "apostas na Pixbet"), or the company
    is the grammatical actor with no public authority in the headline ("Coinbase suspende…")."""
    c = _fold(company).strip()
    if len(c) < 3:
        return False
    cre = re.escape(c)
    if re.search(r"\b(?:d|n)(?:a|o|e|as|os)\s+" + cre + r"\b", t):
        noun_then_company = False
        for rx in _INDUSTRY_RE.values():
            for m in rx.finditer(t):
                if re.match(r"\s+(?:d|n)(?:a|o|e|as|os)\s+" + cre + r"\b", t[m.end():]):
                    noun_then_company = True
        if noun_then_company:
            return True
    pos_c = t.find(c)
    first_change = min((m.start() for _, rx in _STRONG for m in [rx.search(t)] if m), default=-1)
    return 0 <= pos_c < first_change and not _PUBLIC_ACTOR.search(t)


def rejection_reason(text: Any, *, company: str | None = None) -> str | None:
    """Why a change-vocabulary headline is NOT a sector regulatory event, or None if it may be.
    ``company`` — the entity term an entity-query news item was fetched for."""
    t = _fold(text)
    if _POLL_MARK.search(t) or _PCT_OPINION.search(t):
        return "poll"
    if _DEMAND.search(t):
        return "demand"
    if _HYPOTHETICAL.search(t):
        return "hypothetical"
    if _NEGATED.search(t):
        return "negated"
    if company and _operator_specific(t, company):
        return "operator_specific"
    return None


def instrument_refs(text: Any) -> list[str]:
    """Normalised act keys mentioned in ``text`` — ``mp-1394``, ``lei-14790``, ``portaria-mf-2946``."""
    out: list[str] = []
    for kind, num in _INSTRUMENT_RE.findall(_fold(text)):
        kind = _INSTRUMENT_ABBR.get(kind, kind)
        key = f"{re.sub(r'[^a-z]+', '-', kind).strip('-')}-{num.replace('.', '').rstrip('.')}"
        if key not in out:
            out.append(key)
    return out


# --- court rulings (11-08: the TRF-3 story split in two and labelled "proibição") ------------
# A court that lifts or reinstates a measure is a sector development of its own kind: its
# headline carries "proibição"/"suspensão" only as the OBJECT of the ruling ("autoriza retorno de
# três bets após proibição", "suspende liminar"). Labelling it a ban told agents the opposite.
_COURT = re.compile(r"\b(?:justica|tribunal|trf\d|tj[a-z]{2}|stf|stj|supremo|desembargador\w*|juiz\w*"
                    r"|liminar|decisao|fux|court|judge)\b")
_RULING_RESTORE = re.compile(
    r"\bvolta(?:m)? a (?:proibir|bloquear|suspender)\b|\bvoltam? a ser (?:bloquead|proibid|suspens)\w*"
    r"|\bsuspende\w* (?:a |o )?(?:liminar|decisao)\b|\bcass(?:a|am|ou)\b|\brestabelec\w*"
    r"|\bmodifica\w* (?:a )?decisao\b|\bmant(?:em|eve|ida|ido|er)\b|\bconfirm\w* (?:a )?proib"
    r"|\bsuspende\w* (?:a |o )?autorizac")
_RULING_RELEASE = re.compile(
    r"\bliber(?:a|am|ou|aram|ad[ao]s?)\b|\bautoriz(?:a|am|ou|aram|ad[ao]s?)\b|\bretorno\b|\bretomada\b"
    r"|\bvolt(?:a|am|ar) a (?:funcionar|operar)\b|\bderrub\w* (?:a |o )?(?:proib|suspens|mp|medida)")
RULING_LABEL = {"release": "liberação", "restore": "nova proibição / manutenção"}


def _norm(text: Any) -> str:
    """Folded text with TRF-3 / TRF 3 / TRF3 collapsed (a court name, not a number)."""
    return re.sub(r"\btrf[\s-]?(\d)\b", r"trf\1", _fold(text))


def court_ruling_of(text: Any) -> str | None:
    """``"restore"`` / ``"release"`` when the headline reports a court ruling ON A MEASURE — the
    measure must be named ("após proibição", "suspende liminar"); a ruling on contracts or fees
    ("STJ libera encerramento de contas") is not a regulatory change of the sector."""
    t = _norm(text)
    if not _COURT.search(t) or change_type_of(t) is None:
        return None
    if _RULING_RESTORE.search(t):
        return "restore"
    if _RULING_RELEASE.search(t):
        return "release"
    return None


# --- direct report vs background mention (11-08: three catch-all "ban" events) --------------
# Six weeks after MP 1.394 most betting headlines say "proibição" only as a backdrop ("Abras
# aponta alta do consumo após proibição das bets"). Such a headline proves the ban EXISTS — it
# can corroborate the ban while nothing else does (09-25/26) — but it is not a new change.
_BACKGROUND_LEAD = re.compile(
    r"(?:\bapos|\bdepois d[aeo]s?|\bdesde|\bcom|\bsem|\bcontra|\bsobre|\bdiante d[aeo]s?|\bem meio a"
    r"|\bpos|\bmant(?:er|em|eve)|\bcontesta\w*|\bpor|\bpara|\bconta d[ao]|\bfim d[ao]|\bcomo|\bque)"
    r"\s+(?:a |o |as |os |de |da |do )?$")
_ENACT_LEAD = re.compile(r"\b(?:anuncia\w*|decret\w*|determin\w*|oficializ\w*|public\w*|aprov\w*"
                         r"|sanciona\w*|assin\w*|edit\w*|confirma\w*)\s+(?:a |o |as |os )?$")
_STATE_AUX = re.compile(r"\b(?:estao|esta|sao|seguem|continuam|permanecem)\s+$")
# "após Lula proibir", "desde que as bets foram proibidas": the change as a time reference.
_SINCE = re.compile(r"\b(?:apos|depois d\w*|desde)\b(?:\s+\S+){0,4}\s*$")
_INFINITIVE = re.compile(r"^(?:proibir|suspender|revogar|banir|vedar|proibi-l\w+)$")
_EXPLAINER = re.compile(r"^\s*(?:como|entenda|o que|por que|porque|quem|qual|quais|veja|saiba)\b|\?")
_NOUN_FORM = re.compile(r"^(?:proibic|suspens[ao]|revogac|baniment|vedac|medida provisoria|novo marco"
                        r"|nova regulamentacao)")
_PARTICIPLE = re.compile(r"^(?:proibid|suspens[ao]s|suspens[ao]$|banid|vedad|revogad)")
# An industry acting on its own ("Bancos suspendem venda de títulos…", "Bets suspendem parceria")
# names the sector as the SUBJECT of the change verb — a business decision, not a regulator's.
_SELF_ACTION = re.compile(r"^\s*(?:os |as )?(?:" + "|".join(p for v in INDUSTRY_TERMS.values() for p in v)
                          + r")\s+(?:suspend\w*|proibem|vetam|banem|revogam|cancelam|encerram)\b")


def _self_action(t: str) -> bool:
    return bool(_SELF_ACTION.match(t))


def mention_of(text: Any) -> str:
    """``"direct"`` when the headline REPORTS a change (a verb — "proíbe", "ficam proibidos",
    "anuncia proibição" — or a court ruling); ``"background"`` when it only refers to one that
    already happened (a noun after "após/desde/com…", "proibidas por Lula", an explainer)."""
    t = _norm(text)
    if court_ruling_of(t):
        return "direct"
    if _EXPLAINER.search(t):
        return "background"
    hits = [m for _, rx in _STRONG for m in rx.finditer(t)]
    for m in hits:
        word, before = m.group(0), t[:m.start()]
        if _SINCE.search(before):
            continue  # "após Lula proibir", "desde que … foram proibidas"
        if _NOUN_FORM.match(word) or (_PARTICIPLE.match(word) and not re.search(
                r"\b(?:ficam|ficou|ficaram|fica|foram|foi|passam a ser|passa a ser|serao)\s+$", before)):
            if _ENACT_LEAD.search(before):
                return "direct"
            continue  # a noun / adjective: the change as backdrop
        if _INFINITIVE.match(word) or _BACKGROUND_LEAD.search(before) or _STATE_AUX.search(before):
            continue  # "Proibir bets é…", "depois de proibir", "por que devemos proibir"
        return "direct"
    return "background"


_TOPIC_STOP = set((
    "a o as os de da do das dos e em no na nos nas um uma uns umas para por pelo pela pelos pelas com "
    "sem sobre apos ate que se ao aos e ser sao foi seu sua seus suas mais menos ja nao sim ou como "
    "entre contra desde depois antes diante meio isso esse essa este esta ele ela eles elas lhe "
    "governo lula federal brasil brasileiro brasileira brasileiros pais setor mercado empresa empresas "
    "diz dizem afirma segundo entenda veja news hoje agora novo nova novos novas ainda pode podem vai "
    "sera serao ter tem tinha apos medida provisoria mp the of and in to for on").split())


def topic_tokens(text: Any) -> set[str]:
    """Distinctive content tokens of a headline: no sector noun, no change verb, no stop word,
    stemmed to 5 letters, plus ``@court``/``@release``/``@restore`` concept marks."""
    raw = str(text or "")
    if " - " in raw and len(raw.rsplit(" - ", 1)[1]) <= 40:   # Google News " - Publisher" suffix
        raw = raw.rsplit(" - ", 1)[0]
    t = _norm(raw)
    marks: set[str] = set()
    ruling = court_ruling_of(t)
    if ruling:
        marks |= {"@court", f"@{ruling}"}
    for rx in list(_INDUSTRY_RE.values()) + [r for _, r in _STRONG]:
        t = rx.sub(" ", t)
    words = re.findall(r"[a-z0-9]+", t)
    return marks | {w[:5] for w in words if (len(w) >= 3 or w.isdigit()) and w not in _TOPIC_STOP}


def topic_match(a: set[str], b: set[str]) -> bool:
    """Same story: two shared content tokens, or one plus a shared concept — two court rulings
    sharing "trf3" are one story even when one LIFTS and the other REINSTATES (11-06/07)."""
    lex = {x for x in a & b if not x.startswith("@")}
    concept = {x for x in a & b if x.startswith("@")}
    return len(lex) >= 2 or (len(lex) >= 1 and bool(concept))


def assess_headline(title: Any, *, industries: Iterable[str] | None = None,
                    company: str | None = None) -> dict[str, Any]:
    """Classify one news headline. Returns ``{"verdict": "report"|"rejected"|"irrelevant",
    "industries", "change_type", "reason", "mention", "ruling"}``. ``industries`` are the item's
    own tags (a #176 sector-query item); otherwise the sector must be NAMED in the headline."""
    tagged = [str(i) for i in (industries or []) if i]
    inds = list(dict.fromkeys(tagged + industries_in_text(title)))
    ruling = court_ruling_of(title)
    ctype = "court_ruling" if ruling else change_type_of(title)
    base = {"industries": inds, "change_type": ctype, "mention": None, "ruling": ruling}
    if not inds:
        return {**base, "verdict": "irrelevant", "industries": [],
                "reason": "no sector named" if ctype else "no change vocabulary"}
    if not ctype:
        return {**base, "verdict": "irrelevant", "reason": "no change vocabulary"}
    if ruling:
        # a ruling LIFTS or REINSTATES — the negation guard ("restabelece", "derruba") is its
        # very content, so only the poll / demand / hypothetical guards apply.
        t = _fold(title)
        why = ("poll" if _POLL_MARK.search(t) or _PCT_OPINION.search(t) else
               "demand" if _DEMAND.search(t) else "hypothetical" if _HYPOTHETICAL.search(t) else None)
    else:
        why = rejection_reason(title, company=company)
        if not why and _self_action(_fold(title)):
            why = "industry_action"
    if why:
        return {**base, "verdict": "rejected", "reason": why}
    return {**base, "verdict": "report", "reason": None, "mention": mention_of(title)}


def publisher_key(item: dict[str, Any]) -> str:
    """Coarse outlet identity for independence counting. Google-News URLs all share one host,
    so the item's ``publisher`` wins; "O Dia" and "odia.ig.com.br" collapse to ``odia``."""
    from src.synth.outlets import outlet_key

    return outlet_key(item.get("publisher"), item.get("url"))


# --- digest extraction ------------------------------------------------------------------
def _section_items(section: Any) -> list[dict[str, Any]]:
    if not isinstance(section, dict):
        return []
    rows = (section.get("items") or []) + (section.get("context") or [])
    return [r for r in rows if isinstance(r, dict)]


def _dedup(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out = []
    for it in items:
        key = str(it.get("id") or it.get("url") or it.get("title") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _is_official(item: dict[str, Any]) -> bool:
    if item.get("query_kind") == "sector":
        return False
    src = _fold(item.get("source"))
    return item.get("kind") == "regulatory" or any(s in src for s in OFFICIAL_SOURCES)


def split_digest(digest: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(official items WITH ``industries``, news items) from every digest section.

    Official acts may land in ``dou`` or a new federal-acts section (#174/#175) — scanning every
    section keeps this independent of the key the producer picks. News = the ``news`` slice plus
    any ``query_kind: "sector"`` item wherever it lands."""
    official: list[dict[str, Any]] = []
    news: list[dict[str, Any]] = []
    for key, section in (digest or {}).items():
        for it in _section_items(section):
            if key == "news" or it.get("query_kind") == "sector":
                news.append(it)
            elif _is_official(it) and it.get("industries"):
                official.append(it)
    return _dedup(official), _dedup(news)


# --- event construction -----------------------------------------------------------------
def _d(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _sev(value: Any) -> str:
    s = str(value or "").strip().lower()
    return s if s in _SEV_RANK else "medium"  # absent/unknown ⇒ medium (data contract)


def _max_sev(*vals: str) -> str:
    return max((v for v in vals if v in _SEV_RANK), key=lambda v: _SEV_RANK[v], default="medium")


def _unsplice(text: str) -> str:
    """The in.gov.br search snippet glues a head fragment to a highlight fragment that repeats
    it mid-word ("Proíbe a exploração, a oferta, a intermediaçãoíbe a exploração, a oferta, a
    intermediação e a publicidade…"). Keep the first word and resume at the repeat. Only ever
    DROPS the duplicated span — never adds text."""
    m = re.match(r"(\S+\s)", text)
    if not m:
        return text
    head = m.end()
    probe = text[head:head + 24]
    if len(probe) < 24:
        return text
    j = text.find(probe, head + len(probe))
    return text[:head] + text[j:] if j > 0 else text


def _ementa(item: dict[str, Any]) -> str:
    """The act's own summary sentence (ementa) from its text, after the title. Verbatim."""
    title = _strip_html(item.get("title"))
    text = _strip_html(item.get("text") or item.get("summary") or "")
    if title and text.upper().startswith(title.upper()):
        text = text[len(title):].strip()
    text = _unsplice(text)
    return _first_sentence(text)


# #233: legal ementas cite articles mid-sentence ("...estabelecida pelo art. 29 da Lei nº 14.790"),
# and ending at the first "." cut three critical BCB resolutions off at "art.". A period after
# one of these abbreviations (or a single letter / ordinal) is not a sentence end.
_EMENTA_ABBR = {"art", "arts", "inc", "incs", "al", "par", "cap", "n", "nº", "no", "nos", "núm",
                "res", "dec", "lc", "mp", "sr", "sra", "dr", "dra", "ltda", "s.a", "cia", "p", "pág",
                "fl", "fls", "obs", "ex", "etc"}
_EMENTA_MAX = 480


def _first_sentence(text: str) -> str:
    """The first full sentence of `text` (at least 20 chars), never ending on an abbreviation;
    clipped at a word boundary with "…" when no sentence end appears within _EMENTA_MAX.
    An amending act's ementa stops before the quoted new wording (': "Art. 3º ....')."""
    text = re.sub(r"\.{3,}", "…", text)              # dotted leaders / ellipses are not ends
    q = re.search(r':\s*["“]', text)
    if q and 20 <= q.start() < _EMENTA_MAX:
        text = text[:q.start() + 1]                   # keep the colon, drop the quoted article
    for m in re.finditer(r"[.;](?=\s|$)", text):
        end = m.end()
        if end < 20:
            continue
        if end > _EMENTA_MAX:
            break
        if m.group(0) == ".":
            prev = re.search(r"([\wº.]+)\.$", text[:end])
            word = (prev.group(1) if prev else "").lower()
            if word in _EMENTA_ABBR or len(word) == 1:
                continue
        return text[:end].strip()
    if len(text) <= _EMENTA_MAX:
        return text.strip()
    cut = text[:_EMENTA_MAX].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


def _act_name(item: dict[str, Any]) -> str:
    """A title for an act that arrives without one: the BCB normativos search gives
    ``doc_type`` + ``number`` (id ``bcb:<tipo>:<número>``) and no title. "Resolução BCB nº 597"
    is what the act is called, and it carries the instrument ref that joins the DOU copy."""
    doc_type, number = item.get("doc_type"), item.get("number")
    if not (doc_type and number):
        parts = str(item.get("id") or "").split(":")
        if len(parts) == 3 and parts[0] == "bcb":
            doc_type, number = doc_type or parts[1], number or parts[2]
    if not (doc_type and number) or str(number) == "None":
        return ""
    return f"{doc_type} nº {number}"


def _official_source(item: dict[str, Any]) -> dict[str, Any]:
    title = _strip_html(item.get("title")) or _act_name(item)
    summary = _ementa(item) if item.get("title") else ""
    return {"kind": "official", "id": item.get("id"), "title": title,
            "url": item.get("url"), "date": str(item.get("date") or "")[:10],
            "source": item.get("source"), "organ": item.get("organ"), "section": item.get("section"),
            "doc_type": item.get("doc_type"), "severity": _sev(item.get("severity")),
            "summary": summary or _strip_html(item.get("subject"))[:320]}


def _news_source(item: dict[str, Any], assessment: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "news", "id": item.get("id"), "title": _strip_html(item.get("title")),
            "url": item.get("url"), "date": str(item.get("date") or "")[:10],
            "publisher": item.get("publisher"), "publisher_key": publisher_key(item),
            "change_type": assessment.get("change_type"),
            "mention": assessment.get("mention"), "ruling": assessment.get("ruling"),
            "industries": assessment.get("industries") or [],
            "query_kind": item.get("query_kind") or "entity"}


def _event_id(industry: str, anchor: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _fold(anchor)).strip("-")[:60]
    if not slug:
        slug = hashlib.sha1(anchor.encode("utf-8")).hexdigest()[:10]
    return f"sector_event:{industry}:{slug}"


def _sources_sorted(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Official acts first (critical first), then news by date."""
    return sorted(sources, key=lambda s: (0 if s.get("kind") == "official" else 1,
                                          -_SEV_RANK.get(s.get("severity") or "", -1),
                                          s.get("date") or "", s.get("title") or ""))


def _refresh(ev: dict[str, Any]) -> dict[str, Any]:
    """Recompute the derived fields of an event from its sources."""
    srcs = _sources_sorted(_dedup(ev.get("sources") or []))
    ev["sources"] = srcs
    official = [s for s in srcs if s.get("kind") == "official"]
    news = [s for s in srcs if s.get("kind") == "news"]
    outlets = sorted({s.get("publisher_key") for s in news if s.get("publisher_key")})
    ev["n_outlets"] = len(outlets)
    ev["outlets"] = [next((s.get("publisher") for s in news if s.get("publisher_key") == k), k)
                     for k in outlets]
    ev["n_official"] = len(official)
    ev["confidence"] = "official" if official else ("corroborated" if len(outlets) >= MIN_OUTLETS
                                                    else "reported")
    ctypes = Counter(s.get("change_type") for s in news if s.get("change_type"))
    if official:
        # the DOU publication leads (full title + ementa), then any summarised act; the BCB
        # normativos copy of the same act is a bare "Resolução BCB nº N" + subject line
        lead = min(official, key=lambda s: (not s.get("summary"), str(s.get("id") or "").startswith("bcb:")))
        ev["title"] = lead["title"]
        # #233: a BCB Comunicado can arrive with no ementa at all — say so instead of a blank.
        ev["summary"] = lead.get("summary") or "Ementa não publicada pela fonte — consulte o ato oficial."
        ev["severity"] = _max_sev(*[s.get("severity") or "medium" for s in official])
        ev["change_type"] = change_type_of(f"{lead['title']} {lead.get('summary')}") or \
            (ctypes.most_common(1)[0][0] if ctypes else "act")
        ev["date"] = min(s.get("date") or "9999" for s in official)
    else:
        ctype = ctypes.most_common(1)[0][0] if ctypes else "new_framework"
        ev["change_type"] = ctype
        # A press-only event never claims more than the press did: its title is a verbatim
        # headline and its severity tops out at "high" (critical needs the act itself).
        ev["severity"] = "high" if ctype in ("ban", "suspension", "illegal") else "medium"
        # the lead is the earliest report that best states the shared story (not the
        # alphabetically first one — "AGU vai recorrer…" led the TRF-3 ruling)
        first = [s for s in news if s.get("date") == min(x.get("date") or "" for x in news)]
        toks = [topic_tokens(s.get("title")) for s in news]
        lead = max(first, key=lambda s: sum(len(topic_tokens(s.get("title")) & t) for t in toks),
                   default={})
        ev["title"] = lead.get("title") or ""
        ev["summary"] = (f"{pt_count(len(outlets), 'veículo independente relata', 'veículos independentes relatam')} "
                         f"{CHANGE_LABEL.get(ctype, 'mudança regulatória')} no setor — "
                         f"relato de imprensa, sem ato oficial ingerido.")
        ev["date"] = min((s.get("date") or "9999" for s in news), default=ev.get("date"))
        if ctype == "court_ruling":
            dirs = Counter(court_ruling_of(s.get("title")) for s in news)
            ev["rulings"] = {k: dirs[k] for k in ("release", "restore") if dirs[k]}
            if len(ev["rulings"]) > 1:
                # 11-08 TRF-3: "libera três casas" and "volta a proibir" on the same day. Both
                # are real reports of successive rulings; say so instead of picking one.
                ev["summary"] += (" Relatos divergentes: " + " × ".join(
                    f"{RULING_LABEL[k]} ({v})" for k, v in ev["rulings"].items())
                    + " — decisão em disputa; confirme nas fontes.")
        else:
            ev.pop("rulings", None)
        ev["contested"] = len(ev.get("rulings") or {}) > 1
    ev["change_label"] = CHANGE_LABEL.get(ev["change_type"], "mudança regulatória")
    ev["last_evidence"] = max((s.get("date") or "" for s in srcs), default=ev.get("date"))
    ev["industries"] = [ev["industry"]]
    return ev


def _new_event(industry: str, anchor: str, sources: list[dict[str, Any]], run_date: str,
               anchor_refs: list[str] | None = None) -> dict[str, Any]:
    ev = {"id": _event_id(industry, anchor), "industry": industry, "kind": "sector_event",
          "anchor_refs": list(anchor_refs or []), "sources": list(sources),
          "first_seen": run_date, "last_seen": run_date}
    return _refresh(ev)


def _in_window(ev: dict[str, Any], date: str) -> bool:
    e, d = _d(ev.get("date")), _d(date)
    if not e or not d:
        return False
    return -2 <= (d - e).days <= MERGE_WINDOW_DAYS


def _is_generic(srcs: Iterable[dict[str, Any]]) -> bool:
    """Every news source only REFERS to the change (background mention, nothing specific)."""
    return all(mention_of(s.get("title")) == "background" for s in srcs if s.get("kind") == "news")


def _story_match(title: Any, srcs: list[dict[str, Any]]) -> bool:
    """Does a headline tell the SAME story as a press-born event / cluster? A background mention
    matches only a generic one ("the ban"); a direct report must share its topic with one of the
    cluster's direct reports (11-08: INSS portabilidade ≠ a Texas data-center bond sale)."""
    news = [s for s in srcs if s.get("kind") == "news"]
    if mention_of(title) == "background":
        return _is_generic(news)
    mine = topic_tokens(title)
    direct = [s for s in news if mention_of(s.get("title")) == "direct"]
    return any(topic_match(mine, topic_tokens(s.get("title"))) for s in direct)


def _find_event(events: list[dict[str, Any]], industry: str, *, date: str,
                refs: Iterable[str] = (), change_type: str | None = None,
                allow_window: bool = True, title: Any = None) -> dict[str, Any] | None:
    """An item joins an event by an explicit instrument reference; failing that (news only,
    ``allow_window``) by the date window, but ONLY an event of the same kind of change.
    Live dry run 2026-09-27: a bare industry+window fallback merged routine SPA editais de
    citação into the MP 1.394 ban (dating it 09-24) and a CMN FIDC rule into MP 1.393.

    ``title`` (news): a PRESS-BORN event must also tell the same story (``_story_match``) —
    11-08: industry+type+window alone let a bond-sale story corroborate an INSS rule and let
    one "ban" event absorb 123 unrelated betting headlines. Official events keep the window rule."""
    refs = set(refs)
    same = [e for e in events if e.get("industry") == industry]
    if refs:
        for e in same:
            if refs & set(e.get("anchor_refs") or []):
                return e
    if not allow_window or not change_type:
        return None
    cands = [e for e in same if _in_window(e, date) and e.get("change_type") == change_type
             and (title is None or e.get("n_official")
                  or _story_match(title, e.get("sources") or []))]
    return max(cands, key=lambda e: e.get("date") or "") if cands else None


#: One measure is reported as a ban, a suspension of operations or revoked licences ("saques
#: desde a suspensão pelo governo" is MP 1.394's aftermath too).
RESTRICTIONS = ("ban", "suspension", "illegal", "revocation")


def _aftermath_of(events: list[dict[str, Any]], industry: str, change_type: str,
                  date: str) -> dict[str, Any] | None:
    """The earlier event a background mention refers to: same sector and kind of change, dated
    on/before the mention. "após proibição das bets" six weeks on is MP 1.394's aftermath."""
    if change_type not in RESTRICTIONS:
        return None  # "novo marco" commentary has no single act it is the backdrop of
    d = _d(date)
    prior = [e for e in events if e.get("industry") == industry and e.get("change_type") in RESTRICTIONS
             and d and _d(e.get("date")) and _d(e.get("date")) <= d + dt.timedelta(days=2)]
    return min(prior, key=lambda e: e.get("date") or "") if prior else None


def cluster_pending(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group one industry's pending reports into stories: same kind of change, first report
    within MERGE_WINDOW_DAYS, and the same story (``_story_match``). Before 11-08 every pending
    row of an industry was promoted as ONE event once two outlets existed."""
    clusters: list[list[dict[str, Any]]] = []
    for r in sorted(rows, key=lambda r: (r.get("date") or "", str(r.get("id") or ""))):
        home = next((c for c in clusters
                     if c[0].get("change_type") == r.get("change_type")
                     and _in_window({"date": c[0].get("date")}, r.get("date") or "")
                     and _story_match(r.get("title"), [{**x, "kind": "news"} for x in c])), None)
        if home is None:
            clusters.append([r])
        else:
            home.append(r)
    return clusters


def _attach(ev: dict[str, Any], src: dict[str, Any], run_date: str) -> bool:
    same = next((s for s in ev.get("sources") or []
                 if (s.get("id") and s.get("id") == src.get("id"))
                 or (s.get("url") and s.get("url") == src.get("url"))), None)
    if same is not None:
        # already attached; a stored copy saved without title/summary takes them from this run
        if any(not same.get(k) and src.get(k) for k in ("title", "summary")):
            for k in ("title", "summary"):
                same[k] = same.get(k) or src.get(k)
            _refresh(ev)
        return False
    ev.setdefault("sources", []).append(src)
    ev["last_seen"] = run_date
    _refresh(ev)
    return True


def repair_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Undo two store defects (10-04: 30 of 46 live events): BCB acts saved without a title,
    and the same event id stored once per run. Names untitled acts, then merges events of one
    industry that share an id or an instrument ref (the DOU-titled one leads). Idempotent."""
    for ev in events:
        for s in ev.get("sources") or []:
            if s.get("kind") == "official" and not s.get("title"):
                s["title"] = _act_name(s)
        refs = [r for s in ev.get("sources") or [] if s.get("kind") == "official"
                for r in instrument_refs(s.get("title"))]
        ev["anchor_refs"] = list(dict.fromkeys((ev.get("anchor_refs") or []) + refs))
    # an event whose sources carry real ementas leads (DOU copy) — then the oldest
    order = sorted(events, key=lambda e: (not any(s.get("summary") for s in e.get("sources") or []),
                                          e.get("first_seen") or "9999"))
    kept: list[dict[str, Any]] = []
    for ev in order:
        host = next((k for k in kept if k.get("industry") == ev.get("industry") and (
            k.get("id") == ev.get("id")
            or set(k.get("anchor_refs") or []) & set(ev.get("anchor_refs") or []))), None)
        if host is None:
            kept.append(ev)
            continue
        for s in ev.get("sources") or []:
            if not any((x.get("id") and x.get("id") == s.get("id"))
                       or (x.get("url") and x.get("url") == s.get("url"))
                       for x in host.get("sources") or []):
                host["sources"].append(s)
        host["anchor_refs"] = list(dict.fromkeys(host["anchor_refs"] + ev["anchor_refs"]))
        host["first_seen"] = min(host.get("first_seen") or "9999", ev.get("first_seen") or "9999")
        host["last_seen"] = max(host.get("last_seen") or "", ev.get("last_seen") or "")
    return [_refresh(e) for e in kept]


def build_events(
    store: dict[str, Any] | None,
    official: list[dict[str, Any]],
    news: list[dict[str, Any]],
    *,
    today: dt.date | None = None,
    industries: Iterable[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fold one run's official + news items into the store. Returns (store, run_report).

    ``industries`` — the covered industry slugs (registry); an item tagged with an industry
    outside it is ignored rather than inventing an unknown sector."""
    today = today or dt.date.today()
    run_date = today.isoformat()
    covered = {str(i) for i in industries} if industries is not None else None
    store = json.loads(json.dumps(store or {}))  # never mutate the caller's copy
    events: list[dict[str, Any]] = repair_events(list(store.get("events") or []))
    pending: list[dict[str, Any]] = list(store.get("pending") or [])
    report: dict[str, Any] = {"official": 0, "reports": [], "rejected": [], "created": [],
                              "updated": [], "aftermath": []}

    def _ok(ind: str) -> bool:
        return covered is None or ind in covered

    def _mark(ev: dict[str, Any], created: bool) -> None:
        bucket = report["created"] if created else report["updated"]
        if ev["id"] not in bucket and ev["id"] not in report["created"]:
            bucket.append(ev["id"])

    # 1) official acts: primary acts anchor events; secondary acts attach by reference.
    #
    # Which acts may OPEN an event (#175 severity semantics): ``critical`` = a sector-wide ban /
    # suspension / revocation → always. ``high`` also covers every routine framework amendment
    # (most BCB/CVM resoluções) and operator-level bans, so it opens an event only for a
    # top-level federal instrument (MP / Lei / Decreto) that is not operator-level; otherwise it
    # stays in the regular regulatory-card pipeline and here only ATTACHES to an existing event.
    # Severity absent ⇒ unknown/medium: opens only for a top-level instrument. ``low`` ⇒ ignored.
    def _may_open(it: dict[str, Any], sev: str, secondary: bool) -> bool:
        if secondary:
            return False
        if sev == "critical":
            return True
        top = bool(_TOP_LEVEL_ACT.search(_fold(f"{it.get('doc_type') or ''}").strip())
                   or _TOP_LEVEL_ACT.search(_fold(_strip_html(it.get("title"))).strip()))
        operator_level = "operator" in _fold(it.get("severity_reason"))
        if sev == "high":
            return top and not operator_level
        return top and not it.get("severity")

    primaries, secondaries = [], []
    for it in official:
        blob = f"{_strip_html(it.get('title'))} {it.get('doc_type') or ''}"
        (secondaries if _SECONDARY_ACT.search(_fold(blob)) else primaries).append(it)
    for it in primaries + secondaries:
        src = _official_source(it)
        if src["severity"] == "low":
            continue  # low-severity acts are operator-level, not sector-wide
        title_refs = instrument_refs(src["title"])
        all_refs = instrument_refs(f"{src['title']} {_strip_html(it.get('text'))}")
        is_secondary = it in secondaries
        for ind in [str(i) for i in (it.get("industries") or []) if _ok(str(i))]:
            report["official"] += 1
            # join by ANY instrument the act cites (title or body): an implementing Portaria
            # that "regulamenta a MP nº 1.394" belongs to the ban event; an unrelated act in the
            # same sector and window does not.
            ev = _find_event(events, ind, date=src["date"], refs=all_refs, allow_window=False)
            if ev is None and not is_secondary:
                # a primary act whose industry already has a PRESS-born event of the SAME kind of
                # change in the window upgrades it (keeps its id) instead of opening a parallel one.
                act_txt = f"{src['title']} {src.get('summary') or ''}"
                # #286: a lifted measure ("Revertida suspensão…") is no change of that kind
                act_ctype = None if _NEGATED.search(_fold(act_txt)) else change_type_of(act_txt)
                ev = next((e for e in events if e.get("industry") == ind and not e.get("n_official")
                           and _in_window(e, src["date"]) and act_ctype
                           and e.get("change_type") == act_ctype), None)
            if ev is not None:
                if _attach(ev, src, run_date):
                    ev["anchor_refs"] = list(dict.fromkeys((ev.get("anchor_refs") or []) + title_refs))
                    _mark(ev, False)
                continue
            if not _may_open(it, src["severity"], is_secondary):
                continue
            anchor = title_refs[0] if title_refs else (it.get("id") or src["title"])
            ev = _new_event(ind, anchor, [src], run_date, anchor_refs=title_refs)
            stored = next((e for e in events if e["id"] == ev["id"]), None)
            if stored is not None:  # an act with no instrument ref (Comunicado) seen last run
                if _attach(stored, src, run_date):
                    _mark(stored, False)
                continue
            events.append(ev)
            _mark(ev, True)

    # 2) news: qualifying sector reports → attach to an event, else pending until corroborated.
    for it in news:
        # #176: a sector-query item is tagged; an entity item that duplicated one keeps
        # query_kind "entity" but gains the tag — honour the tag either way.
        tagged = it.get("industries") or None
        a = assess_headline(it.get("title"), industries=tagged,
                            company=None if it.get("query_kind") == "sector" else it.get("company"))
        if a["verdict"] == "rejected":
            report["rejected"].append({"title": it.get("title"), "reason": a["reason"],
                                       "industries": a["industries"], "publisher": it.get("publisher")})
            continue
        if a["verdict"] != "report":
            continue
        src = _news_source(it, a)
        report["reports"].append({"title": src["title"], "publisher": src["publisher"],
                                  "industries": a["industries"], "change_type": a["change_type"]})
        for ind in [i for i in a["industries"] if _ok(i)]:
            refs = instrument_refs(src["title"])
            ev = _find_event(events, ind, date=src["date"], refs=refs, change_type=a["change_type"],
                             title=src["title"])
            if ev is not None:
                if _attach(ev, src, run_date):
                    _mark(ev, False)
                continue
            if a.get("mention") == "background":
                prior = _aftermath_of(events, ind, a["change_type"], src["date"])
                if prior is not None:
                    # a backdrop to a change already on record — never a new event of its own
                    report["aftermath"].append({"title": src["title"], "industry": ind,
                                                "event": prior["id"], "publisher": src["publisher"]})
                    continue
            if not any(p.get("id") == src["id"] and p.get("industry") == ind for p in pending):
                pending.append({**src, "industry": ind})

    # 3) promote corroborated pending reports (≥ MIN_OUTLETS distinct outlets per industry).
    cutoff = (today - dt.timedelta(days=PENDING_DAYS)).isoformat()
    pending = [p for p in pending if (p.get("date") or "") >= cutoff]
    by_ind: dict[str, list[dict[str, Any]]] = {}
    for p in pending:
        by_ind.setdefault(p["industry"], []).append(p)
    promoted: set[tuple[str, str]] = set()
    for ind, all_rows in by_ind.items():
        for rows in cluster_pending(all_rows):
            if len({r.get("publisher_key") for r in rows}) < MIN_OUTLETS:
                continue
            srcs = [{k: v for k, v in r.items() if k != "industry"} for r in rows]
            ctype = rows[0].get("change_type")
            ev = _new_event(ind, f"{ctype}-{rows[0].get('date')}", srcs, run_date)
            existing = next((e for e in events if e["id"] == ev["id"]), None)
            if existing is not None and not existing.get("n_official") and \
                    _story_match(rows[0].get("title"), existing.get("sources") or []):
                for s in srcs:
                    _attach(existing, s, run_date)
                _mark(existing, False)
            else:
                if existing is not None:
                    # same type + date, different story: never merge by an id collision
                    ev["id"] = _event_id(ind, f"{ctype}-{rows[0].get('date')}-"
                                              + hashlib.sha1(str(rows[0].get("id")).encode()).hexdigest()[:6])
                events.append(ev)
                _mark(ev, True)
            promoted |= {(ind, r.get("id")) for r in rows}
    pending = [p for p in pending if (p["industry"], p.get("id")) not in promoted]

    # 4) retention.
    keep_after = (today - dt.timedelta(days=RETENTION_DAYS)).isoformat()
    events = [e for e in events if (e.get("last_evidence") or e.get("date") or "") >= keep_after]
    events.sort(key=lambda e: e.get("date") or "", reverse=True)
    # one name per outlet ("O GLOBO", not also "oglobo.globo.com"), learned from the store
    from src.synth import outlets
    outlets.name_in_place({"events": events, "pending": pending})
    events = [_refresh(e) for e in events]
    store = {"as_of": run_date, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
             "events": events, "pending": pending, "window_days": MERGE_WINDOW_DAYS}
    return store, report


def recluster(store: dict[str, Any] | None, *, today: dt.date | None = None
              ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Re-derive every PRESS-BORN event (and the pending queue) under the current join rules —
    one-shot repair for stores built before 11-08 (catch-all "ban" events, false corroboration,
    one story split in two). Official events are kept exactly as stored. Their news is replayed
    day by day (``today`` = the item's date), so pending expiry and the merge window behave as
    they would have live. Returns (new store, report{old→new mapping, dropped sources})."""
    today = today or dt.date.today()
    base = json.loads(json.dumps(store or {}))
    events = repair_events(list(base.get("events") or []))
    official = [e for e in events if e.get("n_official")]
    press = [e for e in events if not e.get("n_official")]
    items: dict[tuple[str, str], dict[str, Any]] = {}
    seen_in: dict[tuple[str, str], list[str]] = {}
    for ev in press:
        for s in ev.get("sources") or []:
            if s.get("kind") != "news":
                continue
            key = (ev["industry"], str(s.get("id") or s.get("url")))
            items.setdefault(key, {"id": s.get("id"), "title": s.get("title"), "url": s.get("url"),
                                   "date": s.get("date"), "publisher": s.get("publisher"),
                                   "query_kind": "sector", "industries": [ev["industry"]]})
            seen_in.setdefault(key, []).append(ev["id"])
    for p in base.get("pending") or []:
        key = (p.get("industry"), str(p.get("id") or p.get("url")))
        items.setdefault(key, {"id": p.get("id"), "title": p.get("title"), "url": p.get("url"),
                               "date": p.get("date"), "publisher": p.get("publisher"),
                               "query_kind": "sector", "industries": [p.get("industry")]})
    work: dict[str, Any] = {"events": official, "pending": []}
    by_day: dict[str, list[dict[str, Any]]] = {}
    for it in items.values():
        by_day.setdefault(str(it.get("date") or today.isoformat())[:10], []).append(it)
    rejected, aftermath = [], []
    for day in sorted(by_day):
        d = min(_d(day) or today, today)
        work, rep = build_events(work, [], sorted(by_day[day], key=lambda i: str(i.get("id"))), today=d)
        rejected += rep["rejected"]
        aftermath += rep["aftermath"]
    work, _ = build_events(work, [], [], today=today)
    old_meta = {e["id"]: e for e in press}
    mapping = []
    for ev in work["events"]:
        if ev.get("n_official"):
            continue
        olds = sorted({o for s in ev["sources"]
                       for o in seen_in.get((ev["industry"], str(s.get("id") or s.get("url"))), [])})
        if olds:  # keep when Onça first saw it, not the replay date
            ev["first_seen"] = min(old_meta[o].get("first_seen") or "9999" for o in olds)
            ev["last_seen"] = max(old_meta[o].get("last_seen") or "" for o in olds)
        mapping.append({"id": ev["id"], "from": olds})
    work["as_of"] = base.get("as_of") or today.isoformat()
    return work, {"official_kept": len(official), "press_before": len(press),
                  "press_after": len(mapping), "mapping": mapping,
                  "rejected": rejected, "aftermath": aftermath}


# --- affected entities + feed projection ---------------------------------------------------
def industry_rosters(entities: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    """{industry: [{entity, label}]} for ACTIVE entities — from registry rows (``entity_id``/
    ``display_name``) or feed ``entity_attrs`` rows (``entity``/``label``)."""
    out: dict[str, list[dict[str, str]]] = {}
    for e in entities or []:
        if not isinstance(e, dict) or e.get("active", True) is False:
            continue
        eid = e.get("entity_id") or e.get("entity")
        if not eid:
            continue
        label = e.get("display_name") or e.get("label") or eid
        for ind in e.get("industries") or []:
            out.setdefault(str(ind), []).append({"entity": eid, "label": label})
    for rows in out.values():
        rows.sort(key=lambda r: r["entity"])
    return out


def with_affected(events: list[dict[str, Any]], rosters: dict[str, list[dict[str, str]]]
                  ) -> list[dict[str, Any]]:
    """Attach ALL active entities of the event's industry — the measure is sector-wide, so
    every licensed operator is affected; this is not a guess about which one."""
    out = []
    for ev in events or []:
        roster = rosters.get(ev.get("industry") or "", [])
        out.append({**ev, "affected_entities": roster, "n_affected": len(roster),
                    "entities": [r["entity"] for r in roster]})
    return out


def entity_attr_rows(entity_attrs: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [{"entity": eid, "label": (a or {}).get("label") or eid,
             "industries": (a or {}).get("industries") or []}
            for eid, a in (entity_attrs or {}).items()]


def for_feed(store: dict[str, Any] | None, *, entity_attrs: dict[str, Any] | None = None,
             window_days: int = 45, today: dt.date | None = None) -> list[dict[str, Any]]:
    """The feed-facing list: events with evidence in the window, affected entities from the
    feed's own ``entity_attrs`` (the same active roster every other block uses)."""
    today = today or dt.date.today()
    cutoff = (today - dt.timedelta(days=window_days)).isoformat()
    events = [e for e in ((store or {}).get("events") or [])
              if (e.get("last_evidence") or e.get("date") or "") >= cutoff]
    rosters = industry_rosters(entity_attr_rows(entity_attrs)) if entity_attrs else {}
    events = with_affected(events, rosters) if entity_attrs else events
    # The roster itself is paid depth + large; the feed carries ids only (labels via entity_attrs).
    return [{k: v for k, v in e.items() if k != "affected_entities"} for e in events]


def scope(events: list[dict[str, Any]] | None, industries: Iterable[str]) -> list[dict[str, Any]]:
    """Tenant scoping: an event is visible only to tenants licensed for its industry."""
    keep = {str(i).strip().lower() for i in (industries or [])}
    return [e for e in (events or []) if str(e.get("industry") or "").lower() in keep]


# --- S3 adapters + orchestrator -------------------------------------------------------------
def load_store(bucket: str, *, s3: Any | None = None) -> dict[str, Any]:
    import boto3
    s3 = s3 or boto3.client("s3")
    try:
        body = s3.get_object(Bucket=bucket, Key=STORE_KEY)["Body"].read()
        data = json.loads(body)
        return data if isinstance(data, dict) else {}
    except Exception:  # first run / missing object
        return {}


def publish(store: dict[str, Any], bucket: str, *, s3: Any | None = None) -> str:
    import boto3
    s3 = s3 or boto3.client("s3")
    s3.put_object(Bucket=bucket, Key=STORE_KEY,
                  Body=json.dumps(store, ensure_ascii=False, indent=2).encode("utf-8"),
                  ContentType="application/json")
    return f"s3://{bucket}/{STORE_KEY}"


def update_from_digest(
    digest: dict[str, Any],
    bucket: str,
    *,
    entities: list[dict[str, Any]] | None = None,
    s3: Any | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """One synth run: extract → fold into the durable store → attach affected entities → publish.
    ``entities`` are registry rows (``entity_registry.list_entities()``)."""
    official, news = split_digest(digest)
    rosters = industry_rosters(entities or [])
    store = load_store(bucket, s3=s3)
    store, report = build_events(store, official, news, today=today,
                                 industries=set(rosters) or None)
    store["events"] = [{k: v for k, v in e.items() if k != "affected_entities"}
                       for e in with_affected(store["events"], rosters)]
    publish(store, bucket, s3=s3)
    return {"events": len(store["events"]), "pending": len(store["pending"]),
            "created": report["created"], "updated": report["updated"],
            "official_items": report["official"], "reports": len(report["reports"]),
            "rejected": len(report["rejected"]), "store": store}
