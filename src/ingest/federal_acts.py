"""Federal normative acts → covered industries + severity (#175, part of incident #173).

MP 1.394 (2026-09-25) banned online betting and named no operator, so an entity-bound pipeline
had nowhere to put it. This module classifies every regulatory record leaving ingest (DOU acts
from Presidência / Atos do Poder Executivo / MF / SPA, and any other ``kind: "regulatory"``
item such as BCB normativos) into:

- ``industries: [slug, …]`` — from the per-industry vocabulary in ``registry.INDUSTRY_TOPICS``;
- ``severity: "critical" | "high" | "medium" | "low"``;
- ``severity_reason`` / ``industries_basis`` — why, so a curator can audit a call.

No entity is required. Pure (no I/O); ``dou.fetch_dou`` calls :func:`annotate` on its output.

**Industries.** An act's *lead* (title + ementa + the ``Art. 1º`` sentence) says what it is
about; the body of a long act mentions many things in passing (MP 1.394's body names banks and
payment institutions because balances are returned through them — it is not a banking act).
So: industries matched in the lead win; if the lead matches none, an industry needs ≥2
vocabulary hits in the body; if neither, the DOU topic phrase that found the act (step 1,
``topic_term``) is kept as the fallback.

**Severity** (the #173 rule of thumb, applied to the lead):

- ``critical`` — a ban / prohibition / suspension, or a revoked authorization or framework, in
  a sector-wide NORMATIVE act (MP, Lei, Decreto, Portaria, Resolução…);
- ``high`` — a new framework or new obligations (institui / regulamenta / dispõe sobre /
  estabelece / altera a Lei…); also a ban/suspension/revocation aimed at ONE operator
  (despacho, edital…), which is operator-level, not sector-wide;
- ``medium`` — a deadline change (prazo, prorroga, vigência) or a procedural act (citação,
  intimação, pauta de julgamento, encaminhamento de mensagem);
- ``low`` — anything else, and ALWAYS: personnel acts (DO2 / nomeia / exonera), budget acts
  (crédito suplementar…), and acts that match no covered industry and no entity (severity is
  relative to Onça's coverage — a diesel subsidy is not a CRO event).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

from src.ingest import registry

SEVERITIES = ("critical", "high", "medium", "low")
#: A full-text act whose lead names no industry keeps a topic-search industry only if that
#: industry's vocabulary recurs this often in the body.
BODY_MIN_HITS = 3


def fold(text: Any) -> str:
    """Accent-stripped lowercase; ``º``/``°`` → ``o`` so "Lei nº" and "Lei n°" both read "lei no"."""
    s = str(text or "").replace("°", "o").replace("º", "o")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", s)


def _term_regex(term: str) -> str:
    if term.startswith("re:"):
        return term[3:]
    t = fold(term).strip()
    stem = t.endswith("*")
    t = t.rstrip("*")
    # "instituic*" must also reach "instituicoes": a stem is a word-prefix INSIDE the phrase
    body = r"\w*".join(re.escape(p) for p in t.split("*"))
    return r"(?<![a-z0-9])" + body + (r"\w*" if stem else r"(?![a-z0-9])")


_VOCAB_CACHE: dict[tuple, dict[str, re.Pattern]] = {}


def vocabulary_patterns(vocab: dict[str, list[str]] | None = None) -> dict[str, re.Pattern]:
    """{industry: compiled alternation} for ``vocab`` (default: the registry's)."""
    vocab = vocab if vocab is not None else registry.industry_vocabulary()
    key = tuple((k, tuple(v)) for k, v in sorted(vocab.items()))
    if key not in _VOCAB_CACHE:
        _VOCAB_CACHE[key] = {ind: re.compile("|".join(_term_regex(t) for t in terms))
                             for ind, terms in vocab.items() if terms}
    return _VOCAB_CACHE[key]


def industries_in(text: Any, vocab: dict[str, list[str]] | None = None, *, min_hits: int = 1) -> list[str]:
    """Industries whose vocabulary occurs ≥ ``min_hits`` times in ``text`` (registry order)."""
    t = fold(text)
    return [ind for ind, rx in vocabulary_patterns(vocab).items()
            if len(rx.findall(t)) >= min_hits]


# --- act anatomy -----------------------------------------------------------------------------
# The enacting clause that ends an act's ementa ("O PRESIDENTE DA REPÚBLICA, no uso…",
# "A SECRETÁRIA DE PRÊMIOS E APOSTAS…, no uso das atribuições…", "… resolve:").
_ENACTING = re.compile(
    r"\b(?:o|a) (?:presidente|presidenta|vice-presidente|ministr[oa]|secretari[oa]|diretor[a]?|"
    r"superintendente|subsecretari[oa]|procurador[a]?|chefe|coordenador[a]?)\b"
    r"|\b(?:o|a) (?:conselho|diretoria|banco central|comissao|colegiado)\b"
    r"|\bno uso d[ae]s? (?:atribuic|competenc)|\bresolve:")
_ART1 = re.compile(r"\bart\. ?1o?\b\.?\s*(.{0,420})")

# Doc types / titles of acts addressed to ONE party or that only reference another act.
_INDIVIDUAL = re.compile(
    r"^\s*(?:despachos?|edital|editais|extratos?|aviso|avisos|atas?|pauta|ato declaratorio|atos declaratorios"
    r"|retificac\w*|decis(?:ao|oes)|mensage(?:m|ns)|comunicado de|termo|acordao|intimac\w*|citac\w*)\b")
# Contract / procurement / cooperation notices: never a regulatory change.
_ROUTINE = re.compile(
    r"^\s*(?:extratos? d[eo]|aviso d[eo] (?:licitac|homologac|adjudicac|dispensa|suspensao de licitac|registro de preco)"
    r"|resultado d[eo] (?:julgamento|licitac)|termo aditivo|ata de registro de precos)")
_PERSONNEL = re.compile(
    r"\b(?:nomea\w*|nomear|exonera\w*|exonerar|designa\w*|designar|dispensa\w*|dispensar|substitut\w*"
    r"|cargo em comissao|funcao comissionada|codigo (?:cce|fce)|ferias|aposentadoria|remove\w*|redistribu\w*"
    r"|cede\w* (?:o|a) servidor|lotac\w*)\b")
_BUDGET = re.compile(
    r"\b(?:credito (?:suplementar|especial|extraordinario)|abre ao orcamento|dotac\w*|remanejament\w*"
    r"|orcamento (?:fiscal|da seguridade)|programacao orcamentaria|limites? de (?:movimentacao|pagamento))\b")

# critical: bans / prohibitions / suspensions / revoked authorizations or frameworks
_BAN = re.compile(
    r"\bproib(?:e|em|ida|idas|ido|idos|icao|icoes|ir)\b"
    r"|\bveda(?:m|da|das|do|dos)?\b|\bvedacao\b"
    r"|\bsuspend(?:e|em)\b|\bsuspens(?:ao|a|as|o|os)\b"
    r"|\b(?:fica|ficam) (?:proibid|vedad|suspens|extint)\w*")
_REVOKED_AUTH = re.compile(
    r"\b(?:revog|cass|extin|extingu|cancel)\w*\W+(?:\w+\W+){0,8}?"
    r"(?:autorizac\w*|concess\w*|licenc\w*|permiss\w*|outorga\w*|credenciament\w*|registros?)\b")
_REVOKES_FRAMEWORK = re.compile(r"^\s*revoga\w*\b")   # an ementa whose PURPOSE is a revocation

# high: new framework / new obligations. "altera a Lei…" alone is weaker than "institui…": an
# ementa that only alters an act AND names a deadline is a deadline change (medium).
_FRAMEWORK = re.compile(
    r"\b(?:institu(?:i|ir|em|indo)|regulament(?:a|am|ar)|disciplin(?:a|am|ar)|dispoe sobre|cria|criar"
    r"|define|consolida|novo marco|novas regras|obrigatori\w*|devera|deverao"
    r"|estabelece(?:m|r)? (?:\w+ ){0,2}(?:regras|normas|procedimentos|requisitos|diretrizes|criterios"
    r"|condicoes|obrigac\w*|limites|vedac\w*))\b")
_ALTERS = re.compile(
    r"\baltera(?:m|r)? (?:a|o|as|os) (?:lei|leis|decreto|decretos|lei complementar|resolucao|resolucoes"
    r"|medida provisoria|portaria|instrucao normativa|circular)\b")
# medium: deadlines / procedure
_DEADLINE = re.compile(
    r"\b(?:prazos?|prorrog\w*|adia\w*|posterg\w*|vigencia|entrada em vigor|cronograma|calendario|data-base|"
    r"data limite)\b")
_PROCEDURAL = re.compile(
    r"\b(?:citac\w*|intimac\w*|notificac\w*|julgamento|pauta|sessao|recurso|audiencia|consulta publica"
    r"|encaminhamento|mensagem|processo administrativo|sancionador\w*|penalidade|advertencia|multa)\b")


def _lead(rec: dict[str, Any]) -> tuple[str, str, str]:
    """(title, ementa, art1) — folded. The ementa is the body up to the enacting clause."""
    # BCB normativos carry no title: "<doc_type> <number>" is the title, the subject the ementa
    raw_title = rec.get("title") or " ".join(str(x) for x in (rec.get("doc_type"), rec.get("number")) if x)
    title = fold(re.sub(r"<[^>]+>", "", str(raw_title or "")))
    subject = fold(rec.get("subject"))
    body = fold(rec.get("text"))
    if body.startswith(title) and title:
        body = body[len(title):].lstrip(" .-")
    if rec.get("full_text") or rec.get("source") != "DOU":
        m = _ENACTING.search(body[:1500])
        ementa = body[: m.start()] if m else body[:600]
        a1 = _ART1.search(body)
        art1 = a1.group(1) if a1 else ""
    else:  # a search snippet is a fragment around the hit, not the ementa: use its head only
        ementa, art1 = body[:400], ""
    if subject and subject != title and subject not in ementa:
        ementa = f"{subject} {ementa}"
    return title, ementa.strip(), art1.strip()


# --- citations: an act implementing a critical MP/law inherits its industries ----------------
_INSTRUMENT = re.compile(
    r"\b(medida provisoria|lei complementar|decreto-lei|decreto|lei)\s+n\s?o?\.?\s*(\d{1,2}(?:\.\d{3})+|\d{1,5})")
_INSTR_ABBR = {"medida provisoria": "mp", "lei complementar": "lc", "decreto-lei": "dl",
               "decreto": "decreto", "lei": "lei"}
_INSTR_PHRASE = {"mp": "Medida Provisória nº", "lc": "Lei Complementar nº", "dl": "Decreto-Lei nº",
                 "decreto": "Decreto nº", "lei": "Lei nº"}


def instrument_refs(text: Any) -> list[str]:
    """Normalized instrument keys cited in ``text``: "mp 1.394", "lei 14.790", "decreto 11.907"…"""
    out: list[str] = []
    for kind, num in _INSTRUMENT.findall(fold(text)):
        key = f"{_INSTR_ABBR[kind]} {num}"
        if key not in out:
            out.append(key)
    return out


def own_ref(rec: dict[str, Any]) -> str | None:
    """The instrument key an act IS (from its title: "MEDIDA PROVISÓRIA Nº 1.394, DE …")."""
    refs = instrument_refs(re.sub(r"<[^>]+>", "", str(rec.get("title") or "")))
    t = fold(rec.get("title")).lstrip()
    return refs[0] if refs and _INSTRUMENT.match(t) else None


def citation_phrase(ref: str) -> str:
    """The DOU search phrase for an instrument key ("mp 1.394" → "Medida Provisória nº 1.394").
    The in.gov.br search needs the "nº": "Medida Provisória 1.394" returns nothing (live)."""
    kind, _, num = ref.partition(" ")
    return f"{_INSTR_PHRASE.get(kind, kind)} {num}"


def critical_anchors(records: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """{instrument key: industries} for the critical PRIMARY acts (MP/Lei/Decreto) in
    ``records`` — the acts whose implementing / referencing acts should inherit their sector."""
    out: dict[str, list[str]] = {}
    for r in records or []:
        ref = own_ref(r) if isinstance(r, dict) else None
        if ref and r.get("severity") == "critical" and r.get("industries"):
            out.setdefault(ref, [])
            out[ref] += [i for i in r["industries"] if i not in out[ref]]
    return out


def classify(rec: dict[str, Any], *, vocab: dict[str, list[str]] | None = None,
             known_instruments: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Classify one regulatory record. Returns ``{"industries", "industries_basis", "severity",
    "severity_reason", "cites"}`` (``industries`` may be empty). Never mutates ``rec``.

    ``known_instruments`` ({"mp 1.394": ["betting"], …}, see :func:`critical_anchors`): an act
    that CITES one of them inherits its industries (union with its own) — the implementing
    Portaria of a ban is a betting act even if its ementa never says "apostas"."""
    title, ementa, art1 = _lead(rec)
    # the issuing organ is part of the lead for INDUSTRY matching only: an SPA act is a betting
    # act even when its snippet is cut mid-name ("SECRETARIA ... DE PRÊMIOS E APOSTAS")
    lead = f"{fold(rec.get('organ'))} {title} {ementa} {art1}"
    body = fold(rec.get("text"))

    inds = industries_in(lead, vocab)
    basis = "lead" if inds else None
    topic = [str(i) for i in (rec.get("topic_industries") or rec.get("industries") or [])]
    if not inds and topic:
        if rec.get("full_text"):
            # full text: the lead is authoritative, so a topic phrase met in passing (an MF
            # institutional-goals annex listing SUSEP; "Sistema Financeiro Nacional" in a
            # boilerplate clause) only counts when its industry recurs through the body
            dense = industries_in(body, vocab, min_hits=BODY_MIN_HITS)
            inds, basis = [i for i in topic if i in dense], "body"
        else:  # a search snippet is not a reliable lead: keep what the topic search found
            inds, basis = topic, "topic"
    cites: list[str] = []
    if known_instruments:
        mine = own_ref(rec)
        for ref in instrument_refs(f"{title} {ementa} {art1} {body}"):
            if ref != mine and ref in known_instruments:
                cites.append(ref)
                for ind in known_instruments[ref]:
                    if ind not in inds:
                        inds = inds + [ind]
        if cites and basis in (None, "topic", "body"):
            basis = "cites " + ", ".join(cites)
    if not inds:
        basis = None

    sev, why = _severity(rec, title, ementa, art1, has_scope=bool(inds or rec.get("company")))
    return {"industries": inds, "industries_basis": basis, "severity": sev, "severity_reason": why,
            "cites": cites}


def _severity(rec: dict[str, Any], title: str, ementa: str, art1: str, *, has_scope: bool) -> tuple[str, str]:
    doc_type = fold(rec.get("doc_type"))
    head = f"{title} {ementa}"
    if not has_scope:
        return "low", "no covered industry or entity"
    if str(rec.get("section") or "").upper().startswith("DO2") or _PERSONNEL.search(head[:500]):
        return "low", "personnel act"
    if _BUDGET.search(head):
        return "low", "budget act"
    if _ROUTINE.search(title) or _ROUTINE.search(doc_type):
        return "low", "routine administrative notice"
    individual = bool(_INDIVIDUAL.search(title) or _INDIVIDUAL.search(doc_type))
    ban_zone = f"{head} {art1}"
    banned = _BAN.search(ban_zone) or _REVOKED_AUTH.search(ban_zone) or _REVOKES_FRAMEWORK.search(ementa)
    if banned and not individual:
        return "critical", f"ban/suspension/revocation: {banned.group(0).strip()!r}"
    if banned and individual:
        return "high", f"operator-level ban/suspension/revocation: {banned.group(0).strip()!r}"
    fw = _FRAMEWORK.search(ementa) if not individual else None
    alters = _ALTERS.search(ementa) if not individual else None
    dl = _DEADLINE.search(ementa)
    if fw:
        return "high", f"new framework/obligation: {fw.group(0)!r}"
    if dl:
        return "medium", f"deadline change: {dl.group(0)!r}"
    if alters:
        return "high", f"amends a framework: {alters.group(0)!r}"
    proc = _PROCEDURAL.search(head)
    if proc:
        return "medium", f"procedural: {proc.group(0)!r}"
    return "low", "routine act"


def annotate(records: Iterable[dict[str, Any]], *, vocab: dict[str, list[str]] | None = None,
             known_instruments: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
    """Add ``industries`` (when any apply) + ``severity`` to each regulatory record, IN PLACE.

    A record's ``industries`` from step 1's topic search are replaced by the classifier's call
    when the classifier finds any (a topic phrase can sit in an act's body in passing), kept as
    the fallback otherwise. Two passes: the second lets an act inherit the industries of a
    critical MP/Lei/Decreto it cites (from this batch, plus ``known_instruments``). Returns the
    list; non-regulatory records are left untouched."""
    recs = list(records or [])
    regs = [r for r in recs if isinstance(r, dict) and r.get("kind") == "regulatory"]
    first = {id(r): classify(r, vocab=vocab) for r in regs}
    known = dict(known_instruments or {})
    for ref, inds in critical_anchors(
            [{**r, "industries": first[id(r)]["industries"] or r.get("industries"),
              "severity": first[id(r)]["severity"]} for r in regs]).items():
        known[ref] = list(dict.fromkeys((known.get(ref) or []) + inds))
    for rec in regs:
        c = classify(rec, vocab=vocab, known_instruments=known) if known else first[id(rec)]
        if rec.get("topic_term") and rec.get("industries") and "topic_industries" not in rec:
            rec["topic_industries"] = list(rec["industries"])  # what the search said, for audit
        if c["industries"]:
            rec["industries"] = c["industries"]
            rec["industries_basis"] = c["industries_basis"]
        else:  # the topic tags did not survive classification: the act is not sector-tagged
            rec.pop("industries", None)
            rec.pop("industries_basis", None)
        if c.get("cites"):
            rec["cites"] = c["cites"]
        rec["severity"] = c["severity"]
        rec["severity_reason"] = c["severity_reason"]
    return recs
