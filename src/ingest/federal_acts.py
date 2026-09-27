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


def compliance_in(text: Any) -> list[str]:
    """Cross-industry compliance tags (#195: "aml") whose vocabulary occurs in ``text`` — a
    tag, never an industry (``registry.COMPLIANCE_TOPICS``)."""
    return industries_in(text, registry.compliance_vocabulary())


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


# --- operator-level enforcement (#193, audit R8/R10) ------------------------------------------
# A BCB/SUSEP/PREVIC resolution regime imposed on ONE institution — liquidação extrajudicial,
# intervenção, RAET — or a cassação of its authorization is the strongest thing a supervisor
# does to an operator. None of it is in _BAN / _REVOKED_AUTH, and these acts name no covered
# industry ("Trustee DTVM"), so Atos do Presidente 1.389/1.390 (03/09) came out ``low``.
# Checked FIRST in _severity: "a nomeação do liquidante" must not read as a personnel act.
_ENF_KINDS: tuple[tuple[str, str, re.Pattern], ...] = (
    ("liquidacao", "liquidação extrajudicial", re.compile(r"\bliquidacao extrajudicial\b")),
    ("raet", "RAET", re.compile(r"\braet\b|\bregime de administracao especial temporaria\b")),
    ("intervencao", "intervenção",
     re.compile(r"\bintervencao extrajudicial\b|\bregime de intervencao\b|\bsob intervencao\b"
                r"|\bintervencao (?:n[ao]s?|em)\b")),
    ("cassacao", "cassação de autorização",
     re.compile(r"\bcass(?:a|am|ar|ou|ada|ado|acao)\b\W+(?:\w+\W+){0,6}?"
                r"(?:autorizac\w*|registros?|licenc\w*|credenciament\w*|outorga\w*|concess\w*)")),
)
_DECREE = re.compile(r"\bdecret(?:a|am|ar|ou|ada|ado|acao)\b")
# "…liquidação extrajudicial da Trustee Distribuidora de Títulos e Valores Mobiliários Ltda., a
# nomeação…" → the institution. Runs on the UNFOLDED text: a name starts with a capital.
_ENF_TARGET = re.compile(
    r"(?i:liquida[çc][ãa]o extrajudicial|interven[çc][ãa]o(?: extrajudicial)?|RAET"
    r"|regime de administra[çc][ãa]o especial tempor[áa]ria"
    r"|cass(?:a[çc][ãa]o|a|ar|ou|ada|ado)\s+(?:d?[ao]s?\s+)?(?:autoriza[çc](?:[ãa]o|[õo]es)|registros?|licen[çc]as?)"
    r"(?: para (?:funcionar|funcionamento|operar))?)"
    r"(?:\s*\([^)]{1,12}\))?\s+(?i:d[aoe]s?|n[ao]s?|em)\s+"
    r"(?P<name>[A-ZÀ-Ý0-9][^;\n]{2,160}?)"
    r"(?=,\s+(?:a|o|as|os|e|com|conforme|nos|nas|que|cuj[ao])\s|;|\s+-\s|\s+e\s+(?:a\s+)?(?:nomea|nomeia|indispon|decret|determin)"
    r"|\.\s*$|(?<![A-Z]\.[A-Z])\.\s+[A-ZÀ-Ý]|\s*$)")


def enforcement_target(text: Any) -> str | None:
    """The institution an enforcement act names ("Trustee Distribuidora de Títulos e Valores
    Mobiliários Ltda."), verbatim from the act, or None."""
    s = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(text or ""))).strip()
    m = _ENF_TARGET.search(s)
    if not m:
        return None
    name = m.group("name").strip(" ,.-")
    if name.lower().startswith(("instituic", "instituiç", "entidade", "sociedade que", "empresa que")):
        return None  # "…liquidação extrajudicial de instituições…": a framework, not one operator
    return name + ("." if re.search(r"\b(?:ltda|s\.a|s/a|cia)$", name, re.I) else "")


def enforcement_of(rec: dict[str, Any], *, lead: str | None = None) -> dict[str, Any] | None:
    """Operator-level enforcement classification of one act, or None.

    ``{"kind", "kind_label", "target", "severity", "reason"}``:

    - a DECREE of liquidação extrajudicial / intervenção / RAET ("Decreta…", "Comunica a
      decretação…"), or a cassação of an authorization → ``critical``;
    - any other act about one named institution's liquidação / intervenção / RAET (prazo
      prorrogado, encerramento, conversão) → ``high``.

    A sector-wide framework ("dispõe sobre o regime de liquidação extrajudicial das
    instituições…") names no institution and is not a decree, so it is left to the normal rules.
    """
    if lead is None:
        t, e, a = _lead(rec)
        lead = f"{t} {e} {a}"
    raw = " ".join(str(x) for x in (rec.get("title"), rec.get("subject"), str(rec.get("text") or "")[:1500]) if x)
    for kind, label, rx in _ENF_KINDS:
        hit = rx.search(lead)
        if not hit:
            continue
        target = enforcement_target(raw)
        decree = bool(_DECREE.search(lead))
        if kind == "cassacao" or (decree and (target or kind != "intervencao")):
            return {"kind": kind, "kind_label": label, "target": target, "severity": "critical",
                    "reason": f"operator-level enforcement: {label}"
                              + (f" ({hit.group(0).strip()!r}, decretação)" if decree else f" ({hit.group(0).strip()!r})")}
        if target:
            return {"kind": kind, "kind_label": label, "target": target, "severity": "high",
                    "reason": f"operator-level enforcement: {label} ({hit.group(0).strip()!r})"}
    return None


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

    # #195: an AML/CFT rule binds every supervised institution — a compliance TAG (from the lead
    # + organ: a COAF/UIF act is AML by issuer), and coverage for severity like an industry
    compliance = compliance_in(lead)
    sev, why = _severity(rec, title, ementa, art1,
                         has_scope=bool(inds or compliance or rec.get("company")))
    return {"industries": inds, "industries_basis": basis, "severity": sev, "severity_reason": why,
            "cites": cites, "compliance": compliance}


def _severity(rec: dict[str, Any], title: str, ementa: str, art1: str, *, has_scope: bool) -> tuple[str, str]:
    doc_type = fold(rec.get("doc_type"))
    head = f"{title} {ementa}"
    # #193: operator-level enforcement first — it names its own subject (the institution), so
    # it needs no covered industry, and "nomeação do liquidante" is not a personnel act.
    enf = enforcement_of(rec, lead=f"{title} {ementa} {art1}")
    if enf:
        return enf["severity"], enf["reason"]
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
        if c.get("compliance"):  # #195: cross-industry tag (AML), never an industry
            rec["compliance_tags"] = c["compliance"]
        else:
            rec.pop("compliance_tags", None)
        rec["severity"] = c["severity"]
        rec["severity_reason"] = c["severity_reason"]
    return recs
