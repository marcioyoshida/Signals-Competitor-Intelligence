"""Distress watch — RJ / liquidação beyond the tracked registry (#355–#358).

Why this exists (the Euro17 miss, 2026-10): Grupo Euro17 filed for recuperação judicial
(R$ 1,72 bi, eight companies) and Onça stayed silent. Its BCB-authorised SCD had been
parked in the review queue by the prominence gate since 2026-09-03, so the headline named
no TRACKED entity and `distress.detect_distress_events` dropped it. Two of the group's
companies are BCB-registered correspondents of tracked banks (Votorantim, Daycoval) —
a third-party risk for their compliance officers that no surface showed.

This module takes the headlines that classified as distress and passed the defamation
guards but resolved to NO tracked entity, and resolves them against a WIDER index:

  - the BCB "em funcionamento" register snapshot (``reference/bcb_institutions.json``,
    written by the ingest Lambda, #358);
  - pending ``discovery`` reviews (the parked candidates, #359);
  - tracked entities whose bare brand is not news-searchable (``news_search=False`` —
    the standard resolver never sees them).

Precision-first, like the rest of the distress store:

  - **legal-name match** (the headline carries the institution's legal name minus its
    legal-form suffix) → the institution ITSELF (``role: self``), any brand length;
  - **brand match** (a distinctive brand head, >= 5 chars or alphanumeric, not generic,
    owned by <= ``MAX_BRAND_OWNERS`` CNPJ roots) → ``role: group``: the headline is about
    the brand's economic group, never stated as the institution's own RJ (#356);
  - the name must be the clause SUBJECT: it appears BEFORE the distress phrase.

Outputs: untracked records (#355), tracked records for promoted / non-news entities,
group records for siblings sharing a controller (#356), correspondent links (#357) and
BCB register exits (#358). Every network/registry call is injected so the core is pure.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Callable, Iterable

from src.synth import distress

BCB_SNAPSHOT_KEY = "reference/bcb_institutions.json"
EXITS_KEY = "distress/registry_exits.json"
QSA_KEY = "graph/watchlist_qsa.json"

MAX_BRAND_OWNERS = 3
MIN_HEAD = 5
# Words that are brand heads of real institutions but also everyday Portuguese/business
# words — never enough on their own to bind a headline to an institution.
GENERIC_HEADS = frozenset({
    "banco", "brasil", "brasileiro", "brasileira", "nacional", "credito", "creditos",
    "cooperativa", "capital", "invest", "investimentos", "financeira", "financas",
    "digital", "global", "grupo", "nova", "novo", "futuro", "primeira", "primeiro",
    "mais", "simples", "facil", "plano", "valor", "valores", "rede", "centro", "uniao",
    "alianca", "confianca", "seguro", "seguros", "prime", "super", "real", "ouro",
    "forte", "popular", "master", "pagamentos", "pagamento", "servicos", "gestao",
    "negocios", "empresarial", "comercial", "industrial", "rural", "agro", "sicoob",
    "sicredi", "unicred", "cresol", "ailos", "credisis", "uniprime", "economia",
    "mercado", "empresa", "empresas", "fundo", "fundos", "ativos", "recursos",
    "parana", "paulista", "mineira", "goias", "bahia", "sul", "norte", "leste", "oeste",
    "regional", "estado", "cidade", "familia", "povo", "trabalho", "energia",
})
# Legal-form tails stripped before a legal-name match.
_LEGAL_TAIL = re.compile(r"\s+(?:ltda|s\s*/?\s*a|sa|eireli|me|epp)\.?$")
# BCB register segment words that, right after a brand, make the headline about THAT
# institution ("Euro17 SCD", "MW Instituição de Pagamento").
_SEGMENT_CUES = ("scd", "sociedade de credito direto", "instituicao de pagamento", "financeira",
                 "banco", "corretora", "distribuidora", "dtvm", "cooperativa", "sep",
                 "scfi", "administradora de consorcio")

GROUP_LABEL = "Grupo econômico em {kind}"


def _fold(s: Any) -> str:
    return distress._norm(s)  # accent-folded lowercase


def _root8(v: Any) -> str | None:
    d = re.sub(r"\D", "", str(v or ""))
    return d[:8] if len(d) >= 8 else None


def _legal_name_key(name: str) -> str:
    t = re.sub(r"[^a-z0-9 ]+", " ", _fold(name))
    t = re.sub(r"\s+", " ", t).strip()
    prev = None
    while prev != t:
        prev, t = t, _LEGAL_TAIL.sub("", t).strip()
    return t


def _brand_of(name: str) -> str | None:
    from src.synth.entity_discovery import _clean_institution_brand

    return _clean_institution_brand(name)


def _distinctive(head: str) -> bool:
    if not head or head in GENERIC_HEADS:
        return False
    return len(head) >= MIN_HEAD or (len(head) >= 4 and any(c.isdigit() for c in head))


# --- the wide index ------------------------------------------------------------------------

def build_wide_index(
    bcb_rows: Iterable[dict[str, Any]] = (),
    reviews: Iterable[dict[str, Any]] = (),
    entities: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """``{"legal": {legal_key: [cand]}, "heads": {head: [cand]}}``.

    A candidate: ``{via, name, brand, head, cnpj, segment, review_id, profile, entity_id}``.
    Tracked entities only enter when they are NOT news-searchable (the standard resolver
    already covers the others) — they resolve to tracked events, not untracked ones."""
    legal: dict[str, list[dict[str, Any]]] = {}
    heads: dict[str, list[dict[str, Any]]] = {}

    def _add(c: dict[str, Any]) -> None:
        lk = _legal_name_key(c.get("name") or "")
        if lk and len(lk) >= 8:
            legal.setdefault(lk, []).append(c)
        head = c.get("head")
        if head:
            heads.setdefault(head, []).append(c)

    for r in bcb_rows or []:
        name = str(r.get("name") or "")
        brand = _brand_of(name)
        _add({"via": "bcb_register", "name": name, "brand": brand,
              "head": (brand or "").split(" ")[0] or None, "cnpj": _root8(r.get("cnpj")),
              "segment": r.get("license_class") or r.get("entity_type"),
              "registry": r.get("registry")})
    for rv in reviews or []:
        if rv.get("kind") != "discovery" or rv.get("status", "pending") != "pending":
            continue
        payload = rv.get("payload") or {}
        prof = payload.get("profile") or {}
        name = str(prof.get("raw_name") or rv.get("proposed") or payload.get("surface") or "")
        brand = _brand_of(name) or _fold(rv.get("proposed") or "").strip() or None
        # Live 2026-11-06: a Receita-CNAE candidate ("NORDESTE SAUDE EMPRESARIAL") head-matched
        # "Fábrica de pás eólicas do Nordeste". Only a REGULATED-register profile may match by
        # brand head (and be auto-promoted); the rest match by full legal name only.
        _add({"via": "review_queue", "name": name, "brand": brand,
              "head": ((brand or "").split(" ")[0] or None) if _regulated_profile(payload) else None,
              "cnpj": _root8(payload.get("cnpj") or (prof.get("cnpj_roots") or [None])[0]),
              "segment": prof.get("license_class"), "review_id": rv.get("review_id"),
              "profile": prof or None})
    for e in entities or []:
        if e.get("news_search", True) or not e.get("entity_id"):
            continue
        name = str(e.get("display_name") or "")
        brand = _fold(name).strip() or None
        roots = e.get("cnpj_roots") or []
        vehicle = bool(set(e.get("industries") or []) & _VEHICLE_INDUSTRIES)
        _add({"via": "tracked", "name": name, "brand": brand,
              "head": None if vehicle else ((brand or "").split(" ")[0] or None),
              "cnpj": _root8(roots[0]) if roots else None, "entity_id": e["entity_id"]})
    return {"legal": legal, "heads": heads}


# Fund vehicles carry descriptive names ("Energy Resort FII") — never a brand-head match.
_VEHICLE_INDUSTRIES = frozenset({"agri-funds", "real-estate-funds"})
# Registers whose discovery profiles are structured institutions (auto-promotable).
_REGULATED_SOURCES = frozenset({"bcb_autorizacoes", "susep", "susep_entidades", "spa_apostas"})


def _regulated_profile(payload: dict[str, Any]) -> bool:
    prof = payload.get("profile") or {}
    src = str(prof.get("source") or payload.get("source") or "")
    return src in _REGULATED_SOURCES and bool(prof.get("entity_id")) and bool(prof.get("license_class"))


def _owners(cands: list[dict[str, Any]]) -> set[str]:
    return {c.get("cnpj") or c.get("entity_id") or c.get("review_id") or c.get("name")
            for c in cands}


def match_headline(title: str, index: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """``[(candidate, role)]`` for one distress headline. Empty when nothing binds safely."""
    span = distress.distress_span(title)
    if not span:
        return []
    _kind, start, end = span
    t = _fold(title)
    # The clause subject: what precedes the distress phrase, plus a genitive object right after
    # it ("liquidação extrajudicial DA X", "falência DO X") up to the next clause break.
    before = t[:start]
    m = re.match(r"\s*(?:judicial\s+|extrajudicial\s+)?d[aeo]s?\s+([^,;:()|\-–—]{2,80})", t[end:])
    after = m.group(1) if m else ""
    subject = re.sub(r"[^a-z0-9 ]+", " ", before + " | " + after)
    subject = " " + re.sub(r"\s+", " ", subject).strip() + " "
    # Where a brand HEAD may sit (live 2026-11-06: "Aeris ENERGY recorre…" and "fábrica … do
    # NORDESTE pede…" bound to unrelated names): at the start of the clause that carries the
    # distress (after the last , ; : or an exclusivo-style tag), right after "grupo", or as the
    # first word of the genitive object after the phrase. Never mid-noun-phrase.
    clause = re.split(r"[,;:|]", before)[-1]
    clause = re.sub(r"^\s*(?:exclusivo|urgente|analise|opiniao)\s*", "", re.sub(r"[^a-z0-9 ,;:|]+", " ", clause))
    head_slots = {w for w in [(clause.split() or [""])[0], (after.split() or [""])[0]] if w}
    head_slots |= set(re.findall(r"\bgrupo\s+([a-z0-9]+)", re.sub(r"[^a-z0-9 ]+", " ", before + " " + after)))
    out: list[tuple[dict[str, Any], str]] = []
    seen: set[int] = set()
    # 1) legal name → the institution itself
    for lk, cands in (index.get("legal") or {}).items():
        if f" {lk} " in subject:
            for c in cands:
                if id(c) not in seen:
                    seen.add(id(c))
                    out.append((c, "self"))
    if out:
        return out
    # 2) distinctive brand head → its economic group (or itself, with a segment cue)
    for head, cands in (index.get("heads") or {}).items():
        if not _distinctive(head) or head not in head_slots:
            continue
        if len(_owners(cands)) > MAX_BRAND_OWNERS:
            continue  # one brand, many institutions — ambiguous, attribute to no one
        cued = any(f" {head} {cue} " in subject for cue in _SEGMENT_CUES)
        for c in cands:
            if id(c) not in seen:
                seen.add(id(c))
                out.append((c, "self" if cued and c.get("via") != "tracked" else "group"))
    return out


# --- event construction --------------------------------------------------------------------

def _label(kind: str, role: str) -> str:
    return GROUP_LABEL.format(kind=distress.phrase_for(kind)) if role == "group" else distress.label_for(kind)


def _event(item: dict[str, Any], kind: str, cand: dict[str, Any], role: str,
           today: dt.date) -> dict[str, Any]:
    src = item.get("source") or "News"
    return {
        "entity": cand.get("entity_id"), "kind": kind, "label": _label(kind, role),
        "date": str(item.get("date") or today.isoformat())[:10],
        "title": str(item.get("title") or "").strip(), "url": item.get("url"),
        "source": src, "source_kind": distress.source_kind(src),
        "publisher": distress._publisher(item.get("url")), "evidence_id": item.get("id"),
        "role": role, "subject_name": cand.get("name"), "cnpj": cand.get("cnpj"),
        "segment": cand.get("segment"), "matched_via": cand.get("via"),
        "brand": cand.get("brand"), "review_id": cand.get("review_id"),
    }


def resolve_untracked(
    unresolved: Iterable[dict[str, Any]],
    index: dict[str, Any],
    *,
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Distress events for headlines the tracked resolver missed. Each event carries
    ``entity`` (tracked / non-news entities) or ``entity: None`` (untracked) plus the
    matched candidate's identity."""
    today = today or dt.date.today()
    out: list[dict[str, Any]] = []
    for item in unresolved or []:
        title = item.get("title") or item.get("subject") or ""
        kind = distress.classify_distress(title)
        if not kind:
            continue
        # one institution can enter the index from several sources (register + review queue +
        # tracked) — keep ONE candidate per CNPJ root: tracked > review (it can be promoted) > register
        prio = {"tracked": 0, "review_queue": 1, "bcb_register": 2}
        best: dict[str, tuple[dict[str, Any], str]] = {}
        for cand, role in match_headline(title, index):
            k = cand.get("cnpj") or cand.get("entity_id") or cand.get("review_id") or cand.get("name")
            if k not in best or prio.get(cand.get("via"), 9) < prio.get(best[k][0].get("via"), 9):
                if k in best and not cand.get("segment"):
                    cand = {**cand, "segment": best[k][0].get("segment")}
                best[k] = (cand, role)
        for cand, role in best.values():
            ev = _event(item, kind, cand, role, today)
            ev["_profile"] = cand.get("profile")
            out.append(ev)
    return out


# --- #355 distress overrides the prominence gate --------------------------------------------

def promote(events: list[dict[str, Any]], *, promote_fn: Callable[[dict[str, Any], str], str | None],
            ) -> list[str]:
    """Promote each matched structured candidate (pending review with a BCB profile, or a
    bare BCB-register institution with a mappable licence) into a tracked entity, and
    re-point its events at the new entity. Returns the promoted entity ids."""
    done: dict[str, str | None] = {}
    for ev in events:
        if ev.get("entity"):
            continue
        key = ev.get("review_id") or (ev.get("cnpj") and f"cnpj:{ev['cnpj']}")
        if not key:
            continue
        if key not in done:
            try:
                done[key] = promote_fn(ev, key)
            except Exception as exc:  # pragma: no cover - best-effort
                print(f"Warning: distress promotion of {key} failed: {exc}")
                done[key] = None
        if done[key]:
            ev["entity"] = done[key]
            ev["promoted_to"] = done[key]
            ev["matched_via"] = f"{ev.get('matched_via')}+promoted"
    return [e for e in done.values() if e]


def registry_promote_fn(table: Any | None = None) -> Callable[[dict[str, Any], str], str | None]:
    """The live promotion: a pending discovery review is approved through the registry's own
    path (#359 — which now creates the entity); a register-only institution is created from
    its BCB profile. Coops and unmapped licences are never auto-created."""
    from src.synth import entity_discovery, entity_registry

    def _fn(ev: dict[str, Any], key: str) -> str | None:
        if ev.get("review_id"):
            # only a regulated-register profile is auto-promoted (live 2026-11-06: a Receita-CNAE
            # candidate was approved by a false match) — anything else stays a curator decision
            if not _regulated_profile({"profile": ev.get("_profile") or {}}):
                return None
            item = entity_registry.resolve_review(
                ev["review_id"], "approved", table=table,
                payload={"decided_by": "distress_override", "evidence": ev.get("url")})
            if item is None:
                return None
            return (((item.get("payload") or {}).get("profile") or {}).get("entity_id")
                    or item.get("entity_id"))
        if ev.get("matched_via") != "bcb_register" or not ev.get("cnpj"):
            return None
        prof = entity_discovery._profile_from_bcb_institution(
            {"name": ev.get("subject_name"), "cnpj": ev["cnpj"], "license_class": ev.get("segment")})
        if not prof.get("auto_ok") or not prof.get("industries"):
            return None
        return entity_registry.create_from_profile(prof, source="discovery",
                                                   reason="distress_override", table=table)

    return _fn


# --- #357 correspondents ---------------------------------------------------------------------

def attach_correspondents(
    events: list[dict[str, Any]],
    cnpj_index: dict[str, str],
    *,
    lookup: Callable[[str], list[dict[str, Any]]],
    already: dict[str, Any] | None = None,
) -> int:
    """For each event with a distinctive brand, the TRACKED institutions that contract its
    brand's companies as correspondents (BCB Informes_Correspondentes). One lookup per brand
    per run; a record checked before (``already[key]["correspondent_checked"]``) is skipped."""
    cache: dict[str, list[dict[str, Any]]] = {}
    n = 0
    for ev in events:
        head = (ev.get("brand") or "").split(" ")[0]
        if not _distinctive(head):
            continue
        key = distress.untracked_key(ev) if not ev.get("entity") else distress.record_key(ev)
        prev = (already or {}).get(key) or {}
        if prev.get("correspondent_checked"):
            if prev.get("correspondent_of"):
                ev["correspondent_of"] = prev["correspondent_of"]
            continue
        if head not in cache:
            try:
                cache[head] = lookup(head.upper()) or []
            except Exception as exc:  # pragma: no cover - best-effort network
                print(f"Warning: correspondent lookup for {head} failed: {exc}")
                cache[head] = []
                continue
        links: dict[str, dict[str, Any]] = {}
        for row in cache[head]:
            # the correspondent's own name must carry the brand as a word (contains() is loose)
            if f" {head} " not in " " + re.sub(r"[^a-z0-9 ]+", " ", _fold(row.get("NomeCorrespondente"))) + " ":
                continue
            # #362: an official regime names one CNPJ — only THAT company's contracts count
            # ("Frente", "Trustee", "Pleno" are everyday words in other correspondents' names).
            if ev.get("matched_via") == "bcb_regime" and _root8(row.get("CnpjCorrespondente")) != ev.get("cnpj"):
                continue
            eid = cnpj_index.get(_root8(row.get("CnpjContratante")) or "")
            if not eid or eid == ev.get("entity"):
                continue
            links.setdefault(eid, {"entity": eid, "contratante": row.get("NomeContratante"),
                                   "correspondente": row.get("NomeCorrespondente"),
                                   "cnpj": _root8(row.get("CnpjCorrespondente")),
                                   "posicao": row.get("Posicao")})
        ev["correspondent_checked"] = dt.date.today().isoformat()
        if links:
            ev["correspondent_of"] = sorted(links.values(), key=lambda x: x["entity"])
            n += 1
    return n


MAX_COMMON_COMPANIES = 5


def drop_common_brands(events: list[dict[str, Any]], *,
                       lookup: Callable[[str], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Drop brand-matched (group) events whose head is an ordinary word: the BCB correspondent
    register is a broad corpus of Brazilian company names, and a head carried by more than
    ``MAX_COMMON_COMPANIES`` distinct correspondent companies is not a brand (live 2026-11-06:
    "energy", "nordeste"). Legal-name matches (role self, via legal key) are kept. A lookup
    failure keeps nothing for that head — precision first."""
    verdict: dict[str, bool] = {}
    out = []
    for ev in events:
        if ev.get("role") != "group" or ev.get("matched_via") == "bcb_register_exit":
            out.append(ev)
            continue
        head = (ev.get("brand") or "").split(" ")[0]
        if head not in verdict:
            try:
                rows = lookup(head.upper()) or []
                cos = {r.get("CnpjCorrespondente") for r in rows
                       if f" {head} " in " " + re.sub(r"[^a-z0-9 ]+", " ", _fold(r.get("NomeCorrespondente"))) + " "}
                verdict[head] = len(cos) <= MAX_COMMON_COMPANIES
            except Exception as exc:  # pragma: no cover - best-effort network
                print(f"Warning: brand commonness check for {head} failed: {exc}")
                verdict[head] = False
        if verdict[head]:
            out.append(ev)
        else:
            print(f"Distress watch: dropped common-word brand '{head}' ({ev.get('title', '')[:80]})")
    return out


# --- #356 shared-controller siblings ---------------------------------------------------------

def _controllers(qsa_entry: dict[str, Any]) -> set[tuple[str, str]]:
    out = set()
    for s in qsa_entry.get("socios") or []:
        q = _fold(s.get("qual"))
        if ("socio" in q or "titular" in q) and s.get("name") and s.get("doc_mask"):
            out.add((_fold(s["name"]).strip(), str(s["doc_mask"])))
    return out


GROUP_KINDS = frozenset({"recuperacao_judicial", "falencia", "liquidacao_extrajudicial",
                         "raet", "intervencao"})


def controller_siblings(
    events: list[dict[str, Any]],
    qsa: dict[str, Any],
) -> list[dict[str, Any]]:
    """Group events for tracked entities sharing a PF controller (sócio, same name + masked
    doc) with an entity whose OWN distress was just recorded."""
    ents = (qsa or {}).get("entities") or {}
    ctl = {eid: _controllers(v) for eid, v in ents.items() if isinstance(v, dict)}
    out: list[dict[str, Any]] = []
    for ev in events:
        eid = ev.get("entity")
        if not eid or (ev.get("role") or "self") != "self" or ev["kind"] not in GROUP_KINDS:
            continue
        mine = ctl.get(eid) or set()
        if not mine:
            continue
        for sib, theirs in ctl.items():
            if sib == eid or not (mine & theirs):
                continue
            g = dict(ev)
            g.update(entity=sib, role="group", group_of=eid,
                     label=_label(ev["kind"], "group"), matched_via="controller")
            out.append(g)
    return out


# --- #362 BCB resolution regimes --------------------------------------------------------------

REGIMES_KEY = "reference/bcb_regimes.json"
HISTORY_YEARS = 5
REGIME_PAGE = "https://www.bcb.gov.br/estabilidadefinanceira/resolucao"


def _regime_event(row: dict[str, Any], kind: str, cnpj_index: dict[str, str], *, date: str,
                  title: str, status: str, observed: str | None) -> dict[str, Any]:
    root = str(row.get("cnpj") or "")[:8]
    name = str(row.get("name") or root)
    ev = {
        "entity": cnpj_index.get(root), "kind": kind, "label": distress.label_for(kind),
        "date": date, "title": title, "url": row.get("act_url") or REGIME_PAGE,
        "source": "BCB-Regimes", "source_kind": "regulatory", "publisher": "bcb.gov.br",
        "evidence_id": f"bcb-regime:{row.get('id')}:{kind}", "role": "self",
        "subject_name": name, "cnpj": root, "segment": row.get("segment"),
        "matched_via": "bcb_regime", "brand": _brand_of(name),
        "regime_id": row.get("id"), "regime_status": status, "act": row.get("act"),
        "responsavel": row.get("responsavel"), "regime_end": row.get("end"),
    }
    if observed:
        ev["observed"] = observed
    return ev


def regime_events(doc: dict[str, Any] | None, cnpj_index: dict[str, str], *,
                  today: dt.date) -> list[dict[str, Any]]:
    """Regulatory distress events from the BCB resolution-regimes snapshot.

    - an ACTIVE regime → its kind (liquidação / intervenção / RAET), dated at the decree and
      ``observed`` today, so the record stays live while the BCB still lists it (Banco Rural
      has been in liquidação since 2013 — far past the store TTL);
    - a CLOSED regime → the same record marked with its closure reason and no longer
      refreshed; a closure by conversion into falência is also a ``falencia`` event.
    """
    from src.ingest import bcb_regimes

    out: list[dict[str, Any]] = []
    doc = doc or {}
    for row in (doc.get("active") or {}).values():
        kind = row.get("kind")
        if not kind or not row.get("start"):
            continue
        what = distress.label_for(kind)
        act = f" ({row['act']})" if row.get("act") else ""
        out.append(_regime_event(row, kind, cnpj_index, date=row["start"],
                                 title=f"{row.get('name')}: {what} decretada pelo Banco Central{act}",
                                 status="ativo", observed=today.isoformat()))
    # closed regimes we watched close + history details (#362 history: bound ONLY by an exact
    # CNPJ of a tracked entity, and only when recent — an old homonym never attaches)
    floor = (today - dt.timedelta(days=365 * HISTORY_YEARS)).isoformat()
    history = [r for r in (doc.get("history_detail") or {}).values()
               if str(r.get("cnpj") or "")[:8] in cnpj_index
               and max(str(r.get("end") or ""), str(r.get("start") or "")) >= floor]
    for row in list((doc.get("closed") or {}).values()) + history:
        kind = row.get("kind")
        if not kind or not row.get("start"):
            continue
        sit = str(row.get("situacao") or "Encerrado")
        end = row.get("end") or row.get("left_active_on") or today.isoformat()
        out.append(_regime_event(row, kind, cnpj_index, date=row["start"],
                                 title=f"{row.get('name')}: {distress.label_for(kind)} — {sit}",
                                 status=sit, observed=None))
        if bcb_regimes.is_falencia_closure(sit):
            out.append(_regime_event(row, "falencia", cnpj_index, date=str(end)[:10],
                                     title=f"{row.get('name')}: regime convertido em falência",
                                     status=sit, observed=None))
    return out


def regime_cnpj_matches(doc: dict[str, Any] | None,
                        entities: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """#363: ``(entity_id, cnpj_root, via)`` for tracked entities WITHOUT a CNPJ that a regime
    institution names exactly. Two precise rules, never a fuzzy brand:

    - legal name minus its legal-form tail == an alias / display name ("BANCO MASTER S/A" ↔
      "Banco Master");
    - the institution's own e-mail or site domain label == the alias with its spaces removed,
      >= 6 chars ("liquidacao@willbank.com.br" ↔ "Will Bank").

    A regime that matches more than one entity, or an entity matched by more than one regime,
    is dropped (ambiguous)."""
    rows = list(((doc or {}).get("active") or {}).values()) + list(((doc or {}).get("closed") or {}).values())
    by_name: dict[str, set[str]] = {}
    by_slug: dict[str, set[str]] = {}
    for e in entities:
        if e.get("cnpj_roots") or not e.get("entity_id"):
            continue
        for a in [e.get("display_name"), e.get("news_term"), *(e.get("aliases") or [])]:
            if not a:
                continue
            k = _legal_name_key(a)
            if k:
                by_name.setdefault(k, set()).add(e["entity_id"])
            slug = re.sub(r"[^a-z0-9]", "", _fold(a))
            if len(slug) >= 6:
                by_slug.setdefault(slug, set()).add(e["entity_id"])
    def _labels(row: dict[str, Any]) -> set[str]:
        out = set()
        for v in (row.get("emails"), row.get("site")):
            for dom in re.findall(r"(?:@|//|www\.)([a-z0-9-]+)\.(?:com|net|org|b)", _fold(v)):
                out.add(dom.replace("-", ""))
        return out

    # a domain shared by several institutions (a liquidator's mailbox serves the whole Master
    # group: bancomaster.com.br on 5 regimes) identifies none of them
    dom_use: dict[str, int] = {}
    for row in rows:
        for lab in _labels(row):
            dom_use[lab] = dom_use.get(lab, 0) + 1
    found: dict[str, dict[str, set[str]]] = {}
    for row in rows:
        root = str(row.get("cnpj") or "")[:8]
        if len(root) < 8:
            continue
        hits: set[tuple[str, str]] = set()
        for eid in by_name.get(_legal_name_key(row.get("name") or ""), set()):
            hits.add((eid, "legal_name"))
        for lab in _labels(row):
            if dom_use.get(lab) == 1:
                for eid in by_slug.get(lab, set()):
                    hits.add((eid, "domain"))
        if len({h[0] for h in hits}) == 1:
            for eid, via in hits:
                found.setdefault(eid, {}).setdefault(via, set()).add(root)
    out: list[tuple[str, str, str]] = []
    for eid, vias in sorted(found.items()):
        for via in ("legal_name", "domain"):  # an exact legal name outranks a domain
            roots = vias.get(via) or set()
            if roots:
                if len(roots) == 1:
                    out.append((eid, next(iter(roots)), via))
                break
    return out


# --- #366 controller propagation ---------------------------------------------------------------

def controlled_group_events(events: list[dict[str, Any]],
                            entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A tracked entity's OFFICIAL own distress (regulatory / curated source) → a ``group`` row
    for every tracked entity that lists it among its registry ``controllers`` (Will Bank ←
    Banco Master). Matched by exact alias / display name; never "itself insolvent"."""
    names: dict[str, set[str]] = {}
    for e in entities:
        for a in [e.get("display_name"), e.get("news_term"), e.get("entity_id"), *(e.get("aliases") or [])]:
            if a:
                names.setdefault(_legal_name_key(str(a).replace("_", " ")), set()).add(e["entity_id"])
    controlled: dict[str, set[str]] = {}
    for e in entities:
        for c in e.get("controllers") or []:
            ids = names.get(_legal_name_key(c)) or set()
            if len(ids) == 1:
                controlled.setdefault(next(iter(ids)), set()).add(e["entity_id"])
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for ev in events:
        eid = ev.get("entity")
        if (not eid or (ev.get("role") or "self") != "self" or ev.get("kind") not in GROUP_KINDS
                or ev.get("source_kind") not in ("regulatory", "curated")):
            continue
        for sub in sorted(controlled.get(eid) or ()):
            if sub == eid or (sub, ev["kind"]) in seen:
                continue
            seen.add((sub, ev["kind"]))
            g = {k: v for k, v in ev.items() if k not in ("correspondent_of", "correspondent_checked",
                                                          "observed")}
            g.update(entity=sub, role="group", group_of=eid, label=_label(ev["kind"], "group"),
                     matched_via="controller_registry")
            if ev.get("observed"):
                g["observed"] = ev["observed"]
            out.append(g)
    return out


# --- #358 BCB register exits -----------------------------------------------------------------
# #361: an exit is a FACT about the register, not distress — the first live exits (2026-11-06)
# were cooperative MERGERS (Acredicoop → Viacredi). Exits stay in distress/registry_exits.json;
# an exit caused by a resolution regime reaches the store through regime_events instead.

def exit_events(exits: Iterable[dict[str, Any]], cnpj_index: dict[str, str]) -> list[dict[str, Any]]:
    """One ``saida_cadastro_bcb`` event per institution that left the register. Regulatory
    source (the BCB register itself) — a fact about the listing, not a cause."""
    out: list[dict[str, Any]] = []
    for x in exits or []:
        root = _root8(x.get("cnpj"))
        if not root:
            continue
        name = str(x.get("name") or root)
        out.append({
            "entity": cnpj_index.get(root), "kind": "saida_cadastro_bcb",
            "label": distress.label_for("saida_cadastro_bcb"), "date": str(x.get("date"))[:10],
            "title": f"{name} deixou de constar no cadastro de instituições em funcionamento do BCB",
            "url": "https://www.bcb.gov.br/estabilidadefinanceira/encontreinstituicao",
            "source": "BCB-Autorizacoes", "source_kind": "regulatory", "publisher": "bcb.gov.br",
            "evidence_id": f"bcb-exit:{root}:{str(x.get('date'))[:10]}", "role": "self",
            "subject_name": name, "cnpj": root, "segment": x.get("segment"),
            "matched_via": "bcb_register_exit",
        })
    return out


# --- orchestration ---------------------------------------------------------------------------

def _s3_json(bucket: str, key: str, s3: Any) -> dict[str, Any]:
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception:
        return {}


def run(
    unresolved: list[dict[str, Any]],
    own_events: list[dict[str, Any]],
    index: dict[str, Any],
    *,
    entities: list[dict[str, Any]],
    bcb_rows: list[dict[str, Any]],
    reviews_fn: Callable[[], list[dict[str, Any]]],
    promote_fn: Callable[[dict[str, Any], str], str | None] | None,
    correspondent_lookup: Callable[[str], list[dict[str, Any]]] | None,
    qsa: dict[str, Any] | None,
    exits: list[dict[str, Any]],
    signal_fn: Callable[[str, dict[str, Any]], Any] | None = None,
    today: dt.date | None = None,
    regimes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure-ish orchestration (every side effect injected). Returns
    ``{"tracked": [...], "untracked": [...], "summary": {...}}``."""
    today = today or dt.date.today()
    cnpj_index = {str(r)[:8]: e["entity_id"] for e in entities if e.get("entity_id")
                  for r in (e.get("cnpj_roots") or [])}
    summary: dict[str, Any] = {"unresolved": len(unresolved)}
    wide: list[dict[str, Any]] = []
    if unresolved:
        reviews = reviews_fn() if reviews_fn else []
        widx = build_wide_index(bcb_rows, reviews, entities)
        wide = resolve_untracked(unresolved, widx, today=today)
        # a candidate matched by CNPJ to a tracked entity is that entity (index lag)
        for ev in wide:
            if not ev.get("entity") and ev.get("cnpj") in cnpj_index:
                ev["entity"] = cnpj_index[ev["cnpj"]]
        if correspondent_lookup:
            wide = drop_common_brands(wide, lookup=correspondent_lookup)
        elif any(e.get("role") == "group" for e in wide):
            wide = [e for e in wide if e.get("role") != "group"]  # can't vet a brand → no group claims
        if signal_fn:
            for ev in wide:
                if ev.get("review_id"):
                    try:
                        signal_fn(ev["review_id"], {"kind": "distress", "ref": ev.get("url") or ev.get("evidence_id"),
                                                    "date": ev["date"], "label": ev["label"]})
                    except Exception as exc:  # pragma: no cover
                        print(f"Warning: review signal failed: {exc}")
        summary["promoted"] = promote(wide, promote_fn=promote_fn) if promote_fn else []
    # #361: register exits are NOT distress (mergers); #362: official resolution regimes are.
    regime = regime_events(regimes, cnpj_index, today=today)
    wide += regime
    if correspondent_lookup:
        prev = {**(index.get("untracked") or {}), **(index.get("records") or {})}
        summary["correspondent_links"] = attach_correspondents(
            wide, cnpj_index, lookup=correspondent_lookup, already=prev)
    tracked = [e for e in wide if e.get("entity")]
    untracked = [e for e in wide if not e.get("entity")]
    siblings = controller_siblings(own_events + tracked, qsa or {})
    controlled = controlled_group_events(own_events + tracked, entities)  # #366
    for e in wide + siblings + controlled:
        e.pop("_profile", None)
    summary.update(tracked=len(tracked), untracked=len(untracked), group_siblings=len(siblings),
                   exits=len(exits or []), regimes=len(regime),
                   regimes_tracked=sum(1 for e in regime if e.get("entity")),
                   controlled_group=len(controlled))
    siblings += controlled
    return {"tracked": tracked + siblings, "untracked": untracked, "summary": summary}


def run_live(
    unresolved: list[dict[str, Any]],
    own_events: list[dict[str, Any]],
    index: dict[str, Any],
    bucket: str,
    *,
    s3: Any,
    entities: list[dict[str, Any]] | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """The Lambda wiring: S3 snapshots + registry + BCB correspondents API."""
    from src.synth import entity_registry

    if entities is None:
        try:
            entities = entity_registry.list_entities()
        except Exception as exc:  # pragma: no cover
            print(f"Warning: distress watch registry read failed: {exc}")
            entities = []
    snap = _s3_json(bucket, BCB_SNAPSHOT_KEY, s3)
    exits_doc = _s3_json(bucket, EXITS_KEY, s3)
    regimes = _s3_json(bucket, REGIMES_KEY, s3)
    # #363: a tracked entity with no CNPJ that a regime names exactly gets its CNPJ root
    # (never stolen from another entity — add_cnpj_roots refuses), so the regime binds to it.
    for eid, root, via in regime_cnpj_matches(regimes, entities):
        try:
            if entity_registry.add_cnpj_roots(eid, [root], source=f"bcb_regimes:{via}"):
                print(f"Distress watch: CNPJ {root} attached to {eid} ({via}, BCB resolution regime)")
                for e in entities:
                    if e.get("entity_id") == eid:
                        e["cnpj_roots"] = sorted(set(e.get("cnpj_roots") or []) | {root})
        except Exception as exc:  # pragma: no cover - registry best-effort
            print(f"Warning: CNPJ backfill for {eid} failed: {exc}")
    qsa = _s3_json(bucket, QSA_KEY, s3)
    from src.ingest import bcb_correspondentes

    return run(
        unresolved, own_events, index,
        entities=entities,
        bcb_rows=list((snap.get("institutions") or {}).values()),
        reviews_fn=lambda: entity_registry.list_reviews(status="pending"),
        promote_fn=registry_promote_fn(),
        correspondent_lookup=bcb_correspondentes.lookup_by_name,
        qsa=qsa,
        exits=exits_doc.get("exits") or [],
        signal_fn=entity_registry.record_review_signal,
        today=today,
        regimes=regimes,
    )
