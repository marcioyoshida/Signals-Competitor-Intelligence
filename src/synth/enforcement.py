"""Sanctions / enforcement register for the CCO (#193, audit R10 + false-coverage #9).

**Why this exists.** The CCO ("compliance") officer's risk register was complaint rankings plus
internal data-integrity findings. No supervisor ever reached it: the BCB decreed the liquidação
extrajudicial of Trustee DTVM and Banvox DTVM (Atos do Presidente 1.389/1.390, 03/09) and the
acts came out ``low``, fused into an FII-dividend card; the CVM fined Banco Master, Vorcaro and
others R$ 203 mi (PAS, 08/09) and the story stopped at a ``news_corroborated`` narrative; the
CEIS/CNEP sanctions lens was ingested and shown nowhere. This module keeps ONE durable register
of enforcement *actions* — a supervisor acting against one operator — built only from what is
already ingested:

- **Official acts** (every digest section, items + context): BCB Atos do Presidente /
  Comunicados and DOU acts that :func:`federal_acts.enforcement_of` reads as a liquidação
  extrajudicial, intervenção, RAET or cassação; and sanction proceedings published by a
  sanctioning organ (SPA editais de citação of its Subsecretaria de Ação Sancionadora, COAF /
  CVM / SUSEP / PREVIC decisões in a processo administrativo sancionador).
- **Federal sanctions registries**: CEIS / CNEP rows from ``sanctions/index.json`` (already
  CNPJ-root bound to a tracked entity by ``ceis_cnep``), grouped per entity + cadastro.
- **News** (``reported`` / ``corroborated``): a headline that names an authority (CVM, BC,
  COAF, MPF, PF…) together with an enforcement verb (multa, condena, liquidação, inquérito,
  operação…) AND names the tracked company. This is how the CVM PAS against Banco Master and
  the MPF operation against Betnacional's owner reach the officer until a CVM sanctions source
  exists (audit fix #7). Hypotheticals ("pode ser multado", "pede convocação", CPI) are not.

Content honesty: every action carries the sources it was built from (official first); the
title is a verbatim act subject / headline, never a synthesised claim. A target institution is
the name the act itself prints — it is bound to a tracked entity only through the resolver
(aliases / CNPJ), never by fuzzy name matching (defamation/LGPD guard, same rule as CEIS/CNEP).
DOU ``company`` is the search term that found the act (audit R13), so it is NOT used to bind.

Evidence accumulates across runs in ``enforcement/latest.json`` (digests bucket), like
``sector_events``. Pure core; S3 / registry I/O are thin adapters.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unicodedata
from typing import Any, Callable, Iterable

STORE_KEY = "enforcement/latest.json"

#: Actions stay in the store this long after their last evidence.
RETENTION_DAYS = 400
#: The feed shows actions with evidence in this window.
FEED_WINDOW_DAYS = 180
#: A later source (a Comunicado after the Ato, a 2nd outlet) joins an action with the same
#: (kind, authority, subject) when it is dated within this many days of the action.
MERGE_WINDOW_DAYS = 30
MIN_OUTLETS = 2

SEVERITIES = ("low", "medium", "high", "critical")
_SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}

KIND_LABEL = {
    "liquidacao": "liquidação extrajudicial",
    "raet": "RAET",
    "intervencao": "intervenção",
    "cassacao": "cassação de autorização",
    "sancao": "sanção / multa",
    "processo_sancionador": "processo sancionador",
    "inquerito": "inquérito / operação",
    "inidoneidade": "inidoneidade (CEIS)",
    "impedimento": "impedimento de contratar (CEIS)",
    "anticorrupcao": "punição Lei Anticorrupção (CNEP)",
}
#: Base severity per kind for an OFFICIAL source; news caps at ``high`` until corroborated.
KIND_SEVERITY = {
    "liquidacao": "critical", "raet": "critical", "intervencao": "critical", "cassacao": "critical",
    "sancao": "high", "anticorrupcao": "high", "inidoneidade": "high",
    "processo_sancionador": "medium", "inquerito": "medium", "impedimento": "medium",
}

Resolver = Callable[[dict[str, Any]], list[str]]


def _fold(text: Any) -> str:
    t = unicodedata.normalize("NFKD", str(text or ""))
    return re.sub(r"\s+", " ", "".join(c for c in t if not unicodedata.combining(c)).lower()).strip()


def _strip_html(text: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(text or ""))).strip()


def _d(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _max_sev(*vals: Any) -> str:
    ok = [str(v) for v in vals if str(v) in _SEV_RANK]
    return max(ok, key=lambda s: _SEV_RANK[s]) if ok else "low"


_SUFFIX = re.compile(r"\b(?:ltda|s\.?\s?a|s/a|cia|companhia|eireli|me|epp|em liquidacao(?: extrajudicial)?)\b\.?")


def subject_key(name: Any) -> str:
    """A merge key for a target name: folded, corporate suffixes and punctuation dropped."""
    s = _SUFFIX.sub(" ", _fold(name))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# --- official acts --------------------------------------------------------------------------
_AUTHORITY_ORGAN: list[tuple[str, re.Pattern]] = [
    ("BCB", re.compile(r"banco central")),
    ("SPA/MF", re.compile(r"premios e apostas")),
    ("CVM", re.compile(r"valores mobiliarios")),
    ("COAF", re.compile(r"controle de atividades financeiras|\bcoaf\b")),
    ("SUSEP", re.compile(r"seguros privados|\bsusep\b")),
    ("PREVIC", re.compile(r"previdencia complementar|\bprevic\b")),
]
#: Organs whose own sanction proceedings belong in the register (CADE is merger control here).
SANCTIONING = ("SPA/MF", "CVM", "COAF", "SUSEP", "PREVIC", "BCB")
_INDIVIDUAL_DOC = re.compile(r"^(?:edita(?:l|is)|citac|intimac|decis|despacho|acordao|julgamento|pauta)")
_PROCEEDING = re.compile(r"sancionador\w*|acao sancionadora|\bimputad[ao]s?\b|auto de infracao"
                         r"|processo administrativo")
_PENALTY = re.compile(r"\bmultas?\b|\bpenalidades?\b|\binabilitac\w*|\badvertencia\b|\bcondena\w*"
                      r"|\bpena de\b|\bcassac\w*")
_RULING_DOC = re.compile(r"^(?:decis|acordao|julgamento|despacho decisorio)")
# "…foi imputada à empresa PHD BRASIL CURSOS…", "Interessado: XPTO Ltda.", "Acusados: …"
_PARTY = re.compile(
    r"(?:imputad[ao]s?\s+(?:a|à)\s+(?:empresa|pessoa jur[ií]dica|sociedade)\s+"
    r"|(?:interessad|acusad|autuad|indiciad|recorrent)[ao]s?\s*:\s*)"
    r"(?P<name>[A-ZÀ-Ý0-9][^,;:\n]{2,120}?)"
    r"(?=\s*(?:,|;|\.\.\.|…|\(|\s-\s|\s+inscrit|\s+CNPJ|\s+a\s+pr[aá]tica|\s+pela\s|$|\.\s)|EDITAL\s+DE)")


def authority_of(item: dict[str, Any]) -> str | None:
    if _fold(item.get("source")) == "bcb":
        return "BCB"
    organ = _fold(item.get("organ"))
    for auth, rx in _AUTHORITY_ORGAN:
        if rx.search(organ):
            return auth
    return None


# A party is shown only when it reads as a LEGAL person. Editais also cite natural persons
# (influencers promoting unlicensed bets); like CEIS/CNEP (pessoa jurídica only) they are out of
# scope — a person's name in an officer register is an LGPD exposure with no tracked entity.
_LEGAL_PERSON = re.compile(
    r"\b(?:ltda|s\.?\s?a|s/a|eireli|epp|me|cia|companhia|comercio|servicos?|servico|marketing|tecnolog\w*"
    r"|comunicac\w*|intermedia\w*|atacad\w*|cursos|digital|bets?|apostas|participac\w*|holding|group"
    r"|grupo|gaming|games|entretenimento|instituic\w*|banco|corretora|dtvm|distribuidora|pagamentos?"
    r"|investimentos?|administradora|seguradora|capitaliza\w*|consorcios?|fundacao|associacao|editora"
    r"|publicidade|midia|agencia|promoc\w*|eventos|solucoes|sistemas|consultoria|limitada|sociedade)\b")


def is_legal_person(name: Any) -> bool:
    return bool(_LEGAL_PERSON.search(_fold(name)))


def _party(text: str) -> str | None:
    m = _PARTY.search(text)
    return m.group("name").strip(" ,.-") if m else None


def official_action(item: dict[str, Any]) -> dict[str, Any] | None:
    """One official act → ``{kind, authority, target, severity, reason}`` or None."""
    from src.ingest import federal_acts

    if not isinstance(item, dict) or item.get("kind") not in ("regulatory", None):
        return None
    auth = authority_of(item)
    enf = federal_acts.enforcement_of(item)
    if enf:
        return {"kind": enf["kind"], "authority": auth or "—", "target": enf.get("target"),
                "severity": enf["severity"], "reason": enf["reason"]}
    if auth not in SANCTIONING or auth == "BCB":
        return None  # a BCB act that is not a resolution regime is a rule, not enforcement
    doc = _fold(item.get("doc_type"))
    title = _fold(_strip_html(item.get("title")))
    if not (_INDIVIDUAL_DOC.search(doc) or _INDIVIDUAL_DOC.search(title)):
        return None
    raw = _strip_html(" ".join(str(item.get(k) or "") for k in ("subject", "text")))
    text = _fold(raw)
    if not (_PROCEEDING.search(text) or _PENALTY.search(text)):
        return None
    ruling = bool(_RULING_DOC.search(doc) or _RULING_DOC.search(title)) and bool(_PENALTY.search(text))
    kind = "sancao" if ruling else "processo_sancionador"
    party = _party(raw)
    if party and not is_legal_person(party):
        return None  # a natural person: out of scope (see _LEGAL_PERSON)
    sev = _max_sev(KIND_SEVERITY[kind], item.get("severity") if ruling else None)
    return {"kind": kind, "authority": auth, "target": party, "severity": sev,
            "reason": "sanction ruling" if ruling else "sanction proceeding"}


# --- news --------------------------------------------------------------------------------------
_NEWS_AUTHORITY: list[tuple[str, re.Pattern]] = [
    ("CVM", re.compile(r"\bcvm\b")),
    ("BCB", re.compile(r"\bbc\b|\bbanco central\b|\bbacen\b")),
    ("COAF", re.compile(r"\bcoaf\b")),
    ("Procon", re.compile(r"\bprocon\b")),
    ("MPF", re.compile(r"\bmpf\b|\bministerio publico\b|\bmp-?[a-z]{2}\b")),
    ("PF", re.compile(r"\bpf\b|\bpolicia federal\b")),
    ("SPA/MF", re.compile(r"\bsecretaria de premios\b|\bspa\b")),
    ("SUSEP", re.compile(r"\bsusep\b")),
    ("PREVIC", re.compile(r"\bprevic\b")),
    ("Receita", re.compile(r"\breceita federal\b|\breceita\b")),
]
_NEWS_KIND: list[tuple[str, re.Pattern]] = [
    ("liquidacao", re.compile(r"\bliquidacao extrajudicial\b|\bdecret\w+ (?:a )?liquidacao\b|\bliquida (?:o|a)\b")),
    ("intervencao", re.compile(r"\bdecret\w+ (?:a )?intervencao\b|\bsob intervencao\b")),
    ("raet", re.compile(r"\braet\b")),
    ("cassacao", re.compile(r"\bcass(?:a|ou|acao)\b")),
    ("sancao", re.compile(r"\bmulta(?:s|do|da|dos|das)?\b|\bmultou\b|\bcondena(?:do|da|dos|das|cao)?\b"
                          r"|\bcondenou\b|\bpune\b|\bpuniu\b|\binabilit\w*|\bsanciona\w*|\bsancao\b")),
    ("inquerito", re.compile(r"\binquerito\b|\bdenuncia(?:do|da|dos|das)?\b|\bdenunciou\b|\bindici\w*"
                             r"|\bbusca e apreensao\b|\boperacao contra\b|\bfazem operacao\b|\bdeflagra\w*"
                             r"|\bmiram?\b")),
]
# Hypothetical / demand / political framing: not an enforcement act.
_NEWS_REJECT = re.compile(
    r"\bpode(?:m|ria|riam)?\b|\bpede(?:m)?\b|\bcobra(?:m)?\b|\bdefende(?:m)?\b|\bprojeto\b|\bpropoe\b"
    r"|\bestuda(?:m)?\b|\bconvoca\w*|\bdepor\b|\bdepoimento\b|\bcpi\b|\?|\babsolv\w*|\bnega(?:m)?\b"
    r"|\barquiva\w*|\bameaca\w*|\bpossivel\b|\brisco de\b")
# "operação"/"miram" only count when the actor is a police/prosecution/tax authority.
_POLICE = ("MPF", "PF", "Receita")


def news_action(item: dict[str, Any]) -> dict[str, Any] | None:
    """One news item → ``{kind, authority, target, severity, reason}`` or None. The headline
    must name the item's company (the operator acted against)."""
    if not isinstance(item, dict):
        return None
    title = _strip_html(item.get("title") or item.get("subject"))
    company = str(item.get("company") or item.get("name") or "").strip()
    t = _fold(title)
    if not title or not company or _fold(company) not in t or _NEWS_REJECT.search(t):
        return None
    auths = [a for a, rx in _NEWS_AUTHORITY if rx.search(t)]
    if not auths:
        return None
    for kind, rx in _NEWS_KIND:
        hit = rx.search(t)
        if not hit:
            continue
        if kind == "inquerito" and not any(a in _POLICE for a in auths) and "inquerito" not in hit.group(0):
            continue
        return {"kind": kind, "authority": auths[0], "target": company,
                "severity": _max_sev(KIND_SEVERITY[kind]), "reason": f"headline: {hit.group(0)!r}"}
    return None


# --- CEIS / CNEP ---------------------------------------------------------------------------------
def sanction_kind(row: dict[str, Any]) -> str:
    if str(row.get("cadastro") or "").upper() == "CNEP":
        return "anticorrupcao"
    return "inidoneidade" if "inidone" in _fold(row.get("category")) else "impedimento"


def sanction_actions(records: Iterable[dict[str, Any]], *, today: dt.date | None = None
                     ) -> list[dict[str, Any]]:
    """CEIS/CNEP rows (already CNPJ-bound by ``ceis_cnep``) → one action per (entity, kind),
    counting only sanctions in force today (no end date, or end ≥ today)."""
    today = today or dt.date.today()
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in records or []:
        if not isinstance(r, dict) or not r.get("entity"):
            continue
        end = _d(r.get("end"))
        if end and end < today:
            continue
        groups.setdefault((r["entity"], sanction_kind(r)), []).append(r)
    out: list[dict[str, Any]] = []
    for (eid, kind), rows in sorted(groups.items()):
        rows.sort(key=lambda r: str(r.get("start") or ""), reverse=True)
        cad = str(rows[0].get("cadastro") or "").upper() or "CEIS"
        company = rows[0].get("company") or eid
        cats = sorted({str(r.get("category") or "sanção") for r in rows})
        srcs = [{"kind": "registry", "title": f"{r.get('cadastro')}: {r.get('category') or 'sanção'}",
                 "label": r.get("orgao"), "url": r.get("url"), "date": r.get("start"),
                 "id": r.get("id"), "end": r.get("end")} for r in rows]
        act = _new_action(kind, f"CGU/{cad}", company, eid, run_date=today.isoformat(),
                          origin="sanctions_registry")
        act.update({
            "title": f"{company} — {len(rows)} sanção(ões) vigente(s) no {cad}: {'; '.join(cats)}",
            "summary": "Órgãos sancionadores: " + ", ".join(
                dict.fromkeys(str(r.get("orgao")) for r in rows if r.get("orgao"))),
            "date": rows[0].get("start") or today.isoformat(),  # the newest sanction
            "last_evidence": rows[0].get("start") or today.isoformat(),
            "sources": srcs, "n_sanctions": len(rows),
        })
        out.append(_refresh(act))
    return out


# --- the register ------------------------------------------------------------------------------
def _action_id(kind: str, authority: str, subject: str, date: str) -> str:
    h = hashlib.sha1(f"{kind}|{authority}|{subject}|{date[:7]}".encode("utf-8")).hexdigest()[:12]
    return f"enf:{kind}:{h}"


def _new_action(kind: str, authority: str, target: str | None, entity: str | None, *,
                run_date: str, origin: str, date: str | None = None) -> dict[str, Any]:
    subj = entity or subject_key(target) or "?"
    date = date or run_date
    return {"id": _action_id(kind, authority, subj, date), "kind": kind,
            "kind_label": KIND_LABEL.get(kind, kind), "authority": authority, "target": target,
            "entity": entity, "entities": [entity] if entity else [], "subject_key": subj,
            "date": date, "last_evidence": date, "first_seen": run_date, "origin": origin,
            "severity": "low", "confidence": "reported", "title": None, "summary": None,
            "sources": []}


def _refresh(a: dict[str, Any]) -> dict[str, Any]:
    srcs = sorted(a.get("sources") or [], key=lambda s: ({"official": 0, "registry": 1}.get(s.get("kind"), 2),
                                                          str(s.get("date") or "")))
    a["sources"] = srcs
    official = [s for s in srcs if s.get("kind") in ("official", "registry")]
    news = [s for s in srcs if s.get("kind") == "news"]
    a["n_official"] = len(official)
    a["n_outlets"] = len({s.get("publisher_key") or s.get("label") for s in news})
    if official:
        a["confidence"] = "official"
    else:
        a["confidence"] = "corroborated" if a["n_outlets"] >= MIN_OUTLETS else "reported"
    sev = _max_sev(*(s.get("severity") for s in srcs), KIND_SEVERITY.get(a["kind"]))
    if a["confidence"] == "reported" and sev == "critical":
        sev = "high"  # one outlet never makes a critical enforcement fact
    a["severity"] = sev
    if srcs and not a.get("title"):
        a["title"] = srcs[0].get("title")
    dates = [str(s.get("date"))[:10] for s in srcs if s.get("date")]
    if dates and a.get("origin") != "sanctions_registry":  # registry dates are set by the caller
        a["date"] = min(dates)
        a["last_evidence"] = max(dates)
    return a


def _publisher_key(item: dict[str, Any]) -> str:
    from src.synth.sector_events import publisher_key
    return publisher_key(item)


def _source_of(item: dict[str, Any], kind: str, cls: dict[str, Any]) -> dict[str, Any]:
    title = _strip_html(item.get("title") or "")
    if item.get("source") == "BCB" or not title:
        head = " ".join(str(x) for x in (item.get("doc_type"), item.get("number")) if x)
        title = f"{head} — {item.get('subject')}" if item.get("subject") else (title or head)
    src = {"kind": kind, "id": item.get("id"), "title": title, "url": item.get("url"),
           "date": str(item.get("date") or "")[:10] or None, "severity": cls.get("severity"),
           "reason": cls.get("reason")}
    if kind == "official":
        src.update({"label": item.get("organ") or item.get("source"), "source": item.get("source"),
                    "doc_type": item.get("doc_type"), "section": item.get("section")})
        if item.get("subject") and _fold(item.get("subject")) != _fold(title):
            src["subject"] = _strip_html(item.get("subject"))
    else:
        src.update({"label": item.get("publisher") or item.get("source"),
                    "publisher": item.get("publisher"), "publisher_key": _publisher_key(item)})
    return src


def _section_items(section: Any) -> list[dict[str, Any]]:
    if isinstance(section, dict):
        return [x for x in (section.get("items") or []) + (section.get("context") or []) if isinstance(x, dict)]
    if isinstance(section, list):
        return [x for x in section if isinstance(x, dict)]
    return []


def split_digest(digest: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(official acts, news items, CEIS/CNEP rows) from every digest section, de-duplicated."""
    official: dict[str, dict[str, Any]] = {}
    news: dict[str, dict[str, Any]] = {}
    sanctions: dict[str, dict[str, Any]] = {}
    for key, section in (digest or {}).items():
        for it in _section_items(section):
            iid = str(it.get("id") or it.get("url") or it.get("title") or "")
            if not iid:
                continue
            if key == "sanctions" or it.get("kind") == "sanction":
                sanctions.setdefault(iid, it)
            elif key == "news" or _fold(it.get("source")) == "news":
                news.setdefault(iid, it)
            elif it.get("kind") == "regulatory":
                official.setdefault(iid, it)
    return list(official.values()), list(news.values()), list(sanctions.values())


def _resolve(resolver: Resolver | None, name: str | None, headline: str | None = None) -> str | None:
    """One tracked entity, or None. Ambiguity (≠1 hit) stays unbound — precision first."""
    if not resolver or not (name or headline):
        return None
    try:
        hits = [e for e in (resolver({"title": headline or name, "text": name or headline}) or []) if e]
    except Exception:  # pragma: no cover - resolver best-effort
        return None
    hits = list(dict.fromkeys(hits))
    return hits[0] if len(hits) == 1 else None


def _find(actions: list[dict[str, Any]], kind: str, authority: str, subj: str, date: str
          ) -> dict[str, Any] | None:
    d = _d(date)
    for a in actions:
        if a.get("origin") == "sanctions_registry":
            continue
        if a["kind"] != kind or a["subject_key"] != subj:
            continue
        if authority != a["authority"] and "—" not in (authority, a["authority"]):
            continue
        ad = _d(a.get("date"))
        if d and ad and -MERGE_WINDOW_DAYS <= (d - ad).days <= MERGE_WINDOW_DAYS:
            return a
    return None


def build_register(
    store: dict[str, Any] | None,
    official: Iterable[dict[str, Any]],
    news: Iterable[dict[str, Any]],
    sanctions: Iterable[dict[str, Any]] | None = None,
    *,
    resolver: Resolver | None = None,
    today: dt.date | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fold one run's evidence into the durable store. Returns ``(store, report)``.

    ``sanctions`` (CEIS/CNEP rows) REPLACE the registry-derived actions when given — the
    ``sanctions/index.json`` store is itself durable, so the register mirrors it."""
    today = today or dt.date.today()
    run_date = today.isoformat()
    store = json.loads(json.dumps(store or {}))
    actions: list[dict[str, Any]] = list(store.get("actions") or [])
    report = {"official": 0, "news": 0, "created": [], "updated": [], "rejected_news": 0}

    def _fold_in(item: dict[str, Any], cls: dict[str, Any], kind: str) -> None:
        target = cls.get("target")
        headline = _strip_html(item.get("title")) if kind == "news" else None
        entity = _resolve(resolver, target, headline)
        subj = entity or subject_key(target) or ("item:" + str(item.get("id")))
        src = _source_of(item, kind, cls)
        date = src.get("date") or run_date
        a = _find(actions, cls["kind"], cls["authority"], subj, date)
        created = a is None
        if created:
            a = _new_action(cls["kind"], cls["authority"], target, entity, run_date=run_date,
                            origin=kind, date=date)
            a["subject_key"] = subj
            actions.append(a)
        if any(s.get("id") == src.get("id") for s in a["sources"] if s.get("id")):
            return
        a["sources"].append(src)
        if entity and not a.get("entity"):
            a["entity"], a["entities"] = entity, [entity]
        if target and not a.get("target"):
            a["target"] = target
        if kind == "official" and item.get("industries"):  # the act's own federal_acts call
            a["industries"] = list(dict.fromkeys((a.get("industries") or []) + list(item["industries"])))
        if a.get("authority") == "—" and cls["authority"] != "—":
            a["authority"] = cls["authority"]
        if kind == "official" and a.get("origin") == "news":
            a["title"] = src["title"]  # the act outranks the headline
            a["origin"] = "official"
        if kind == "official" and src.get("subject") and not a.get("summary"):
            a["summary"] = src["subject"]
        if created and kind == "official" and target and _fold(target) not in _fold(src["title"]):
            a["title"] = f"{src['title']} · {target}"  # a generic "EDITAL DE CITAÇÃO" names no one
        _refresh(a)
        (report["created"] if created else report["updated"]).append(a["id"])

    for it in official or []:
        cls = official_action(it)
        if cls:
            report["official"] += 1
            _fold_in(it, cls, "official")
    for it in news or []:
        cls = news_action(it)
        if cls:
            report["news"] += 1
            _fold_in(it, cls, "news")

    if sanctions is not None:
        actions = [a for a in actions if a.get("origin") != "sanctions_registry"]
        actions += sanction_actions(sanctions, today=today)

    cutoff = (today - dt.timedelta(days=RETENTION_DAYS)).isoformat()
    actions = [a for a in actions if str(a.get("last_evidence") or a.get("date") or "") >= cutoff]
    actions.sort(key=lambda a: str(a.get("last_evidence") or ""), reverse=True)
    actions.sort(key=lambda a: -_SEV_RANK.get(a.get("severity") or "low", 0))
    store.update({"as_of": run_date, "actions": actions})
    report["created"] = list(dict.fromkeys(report["created"]))
    report["updated"] = [i for i in dict.fromkeys(report["updated"]) if i not in report["created"]]
    return store, report


# --- feed projection -----------------------------------------------------------------------------
def for_feed(store: dict[str, Any] | None, *, entity_attrs: dict[str, Any] | None = None,
             window_days: int = FEED_WINDOW_DAYS, today: dt.date | None = None) -> list[dict[str, Any]]:
    """The feed-facing list: actions with evidence in the window, most severe then newest first.
    A bound entity's industries and label come from the feed's own ``entity_attrs``."""
    today = today or dt.date.today()
    cutoff = (today - dt.timedelta(days=window_days)).isoformat()
    attrs = entity_attrs or {}
    out: list[dict[str, Any]] = []
    for a in (store or {}).get("actions") or []:
        if str(a.get("last_evidence") or a.get("date") or "") < cutoff:
            continue
        a = dict(a)
        ea = attrs.get(a.get("entity") or "") or {}
        a["industries"] = list(ea.get("industries") or a.get("industries") or [])
        a["label"] = ea.get("label") or a.get("target") or a.get("entity")
        a.pop("subject_key", None)
        out.append(a)
    out.sort(key=lambda a: str(a.get("last_evidence") or ""), reverse=True)
    out.sort(key=lambda a: -_SEV_RANK.get(a.get("severity") or "low", 0))
    return out


def scope(actions: list[dict[str, Any]] | None, industries: Iterable[str],
          row_ok: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
    """Tenant / entry scoping. An entity-bound action follows its entity (``row_ok``); an
    unbound one is a public act about an untracked institution — kept when it carries no
    industry or a licensed one."""
    keep = {str(i).strip().lower() for i in (industries or [])}
    out = []
    for a in actions or []:
        if a.get("entity"):
            if row_ok(a):
                out.append(a)
        elif not a.get("industries") or {str(i).lower() for i in a["industries"]} & keep:
            out.append(a)
    return out


# --- S3 adapters + orchestrator -------------------------------------------------------------------
def load_store(bucket: str, *, s3: Any | None = None) -> dict[str, Any]:
    import boto3
    s3 = s3 or boto3.client("s3")
    try:
        data = json.loads(s3.get_object(Bucket=bucket, Key=STORE_KEY)["Body"].read())
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
    resolver: Resolver | None = None,
    s3: Any | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """One synth run: extract → fold into the durable store (+ mirror CEIS/CNEP) → publish."""
    if resolver is None:
        try:
            from src.synth.entities import resolve_entities as resolver  # lazy
        except Exception:  # pragma: no cover
            resolver = None
    official, news, _digest_sanctions = split_digest(digest)
    try:
        from src.ingest import ceis_cnep
        sanctions = ceis_cnep.list_records(ceis_cnep.load_index(bucket, s3=s3))
    except Exception as exc:  # pragma: no cover - best-effort
        print(f"Warning: sanctions index read skipped: {exc}")
        sanctions = None
    store = load_store(bucket, s3=s3)
    store, report = build_register(store, official, news, sanctions, resolver=resolver, today=today)
    publish(store, bucket, s3=s3)
    return {"actions": len(store["actions"]), "official_items": report["official"],
            "news_items": report["news"], "created": len(report["created"]),
            "updated": len(report["updated"])}
