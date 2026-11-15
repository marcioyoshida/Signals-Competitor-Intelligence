"""Shared Context Pack (#323): the frozen, hashed fact set every officer in a board run reads.

Built from one feed.json for the client's own (focal) institution, the question's subject and
the focal institution's nearest competitors by proximity (#385, #386). Every fact carries a stable
``id`` (what an officer cites as ``pack_fact_id``), its ``source`` and its ``as_of``. The pack
is canonical JSON; its sha256 is the run's ``shared_context_id`` and the S3 key, so the same
inputs always give the same pack and a stored pack is never rewritten.

Entity ids come from the feed, which already folds merged twin ids (#377). Peers are only
ids that exist in ``feed.entities``, so a duplicate registry id cannot slip a stranger's facts
in (ADR 022 trap). Historic replay (M2) will key by the dated CNPJ map (#330) instead.

A pack has a size cap so it fits an officer's context window. When the cap bites, facts are
dropped lowest-priority first and every drop is listed in ``gaps``: never a silent omission.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping

PACK_SCHEMA = "board/v1/pack"
DEFAULT_MAX_CHARS = 60_000
MAX_PEERS = 5
MAX_NARRATIVES = 6
PEER_NARRATIVES = 2
MAX_EVENTS = 6
NARRATIVE_CHARS = 400

# Lower number = kept first when the size cap bites.
PRIORITY = {"proximity": 0, "km1": 0, "soundness": 0, "npl": 0, "fundamentals": 1, "results": 1,
            "distress": 0, "third_party": 1, "enforcement": 1, "macro": 1, "financials": 2, "reputation": 2,
            "sanctions": 0, "swot": 3, "sector_event": 3, "narrative": 4}

# feed narrative kinds that are bookkeeping, not evidence about the entity
_SKIP_NARRATIVE = {"entity_fusion", "silence"}


def _slug(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-") or "x"


def _yyyymm(v: Any) -> str | None:
    s = str(v or "")
    return f"{s[:4]}-{s[4:6]}" if re.fullmatch(r"\d{6}", s) else (s or None)


def _fmt(d: Mapping[str, Any], keys: Iterable[str]) -> str:
    return "; ".join(f"{k}={d[k]}" for k in keys if d.get(k) is not None)


def _fact(fid: str, kind: str, entity: str | None, source: str, as_of: Any, text: str,
          data: Any = None) -> dict[str, Any]:
    f = {"id": fid, "kind": kind, "entity": entity, "source": source,
         "as_of": str(as_of) if as_of else None, "text": text.strip()}
    if data is not None:
        f["data"] = data
    return f


def choose_peers(feed: Mapping[str, Any], target: str, n: int = MAX_PEERS) -> list[str]:
    """The target's largest same-industry peers that exist in ``feed.entities``."""
    ents = {e["entity"]: e for e in feed.get("entities") or [] if e.get("entity")}
    me = ents.get(target)
    if not me:
        return []
    inds = me.get("industries") or []
    ms = (feed.get("market_structure") or {}).get(inds[0]) if inds else None
    ranked = [c.get("entity") for c in (ms or {}).get("constituents") or []]
    if not ranked:
        same = [e for e in ents.values() if set(e.get("industries") or []) & set(inds)]
        ranked = [e["entity"] for e in sorted(same, key=lambda e: -(e.get("market_share_pct") or 0))]
    out: list[str] = []
    for eid in ranked:
        if eid and eid != target and eid in ents and eid not in out:
            out.append(eid)
        if len(out) >= n:
            break
    return out


def _entity_facts(e: Mapping[str, Any]) -> list[dict[str, Any]]:
    eid, label = e["entity"], e.get("label") or e["entity"]
    out = []
    km1 = e.get("pilar3_km1")
    if km1:
        out.append(_fact(f"f-km1-{_slug(eid)}", "km1", eid, "BCB Pilar 3 (KM1)", km1.get("trimestre"),
                         f"{label}: Basileia {km1.get('basileia_pct')}%, alavancagem "
                         f"{km1.get('leverage_pct')}%, LCR {km1.get('lcr_pct')}%, NSFR "
                         f"{km1.get('nsfr_pct')}% (trimestre {km1.get('trimestre')}).", km1))
    s = e.get("soundness")
    if s:
        out.append(_fact(f"f-soundness-{_slug(eid)}", "soundness", eid, "BCB IF.data",
                         _yyyymm(s.get("base_date")),
                         f"{label}: {_fmt(s, ('indice_basileia', 'capital_principal', 'capital_nivel_i', 'razao_alavancagem', 'band'))}.", s))
    npl = e.get("inadimplencia")
    if npl:
        out.append(_fact(f"f-npl-{_slug(eid)}", "npl", eid, "BCB IF.data (inadimplência)",
                         _yyyymm(npl.get("base_date")),
                         f"{label}: inadimplência {_fmt(npl, ('npl_total', 'npl_pf', 'npl_pj', 'band', 'carteira_bi'))}.", npl))
    fu = e.get("fundamentals")
    if fu:
        out.append(_fact(f"f-fundamentals-{_slug(eid)}", "fundamentals", eid, "BCB IF.data",
                         _yyyymm(fu.get("base_date")),
                         f"{label}: {_fmt(fu, ('roe_pct', 'roa_pct', 'basileia_headroom_pp', 'credito_captacoes_pct', 'carteira_share_pct', 'lucro_share_pct', 'ativo_bi'))}.", fu))
    r = e.get("resultados")
    if r:
        out.append(_fact(f"f-results-{_slug(eid)}", "results", eid, "BCB COSIF", _yyyymm(r.get("month")),
                         f"{label}: {_fmt(r, ('custo_credito_pct', 'opex_ativo_pct'))}.", r))
    return out


def build_pack(feed: Mapping[str, Any], focal: str, *, subject: str | None = None,
               peers: list[str] | None = None, max_chars: int = DEFAULT_MAX_CHARS,
               proximity_config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The pack dict (without its hash; see ``seal``). Raises if focal or subject is unknown.

    ``focal`` is the client's own institution (whose board decides); ``subject`` is what the
    question is about (defaults to ``focal``). Peers are the focal institution's nearest
    competitors by proximity (#385), plus the subject when it is a competitor; each gets an
    ``f-proximity-<peer>`` fact so officers can weight its signals by distance."""
    from src.board import proximity as prox

    ents = {e["entity"]: e for e in feed.get("entities") or [] if e.get("entity")}
    if focal not in ents:
        raise ValueError(f"focal {focal!r} is not an entity in this feed")
    subject = subject or focal
    if subject not in ents:
        raise ValueError(f"subject {subject!r} is not an entity in this feed")
    target = focal
    pcfg = proximity_config or prox.load_config()
    ranked = prox.rank(feed, focal, candidates=peers, config=pcfg)
    peers = [r["peer"] for r in ranked][:MAX_PEERS]
    if subject != focal and subject not in peers:
        peers = [subject, *peers][:MAX_PEERS]
    if subject != focal and not any(r["peer"] == subject for r in ranked):
        ranked = ranked + prox.rank(feed, focal, candidates=[subject], config=pcfg)
    prox_by = {r["peer"]: r for r in ranked}
    scope = [focal, *[p for p in peers if p != focal]]
    scope_set = set(scope)
    facts: list[dict[str, Any]] = []
    gaps: list[str] = []

    focal_label = ents[focal].get("label") or focal
    proximity = []
    for eid in scope[1:]:
        r = prox_by.get(eid)
        if r:
            facts.append(prox.fact(r, focal_label, feed.get("as_of")))
            proximity.append({"peer": eid, "label": r["label"], "score": r["score"],
                              "coverage": r["coverage"], "low_coverage": r["low_coverage"],
                              "components": {d: c["sim"] for d, c in r["components"].items()},
                              "missing": r["missing"]})
    for eid in scope:
        ef = _entity_facts(ents[eid])
        if not ef:
            gaps.append(f"{eid}: sem dados prudenciais públicos no feed")
        facts += ef

    # Same trust gate as the Risco board: a single-outlet "reported" item never reaches a board
    # surface (#33 counterparty mis-attribution). A group/correspondent row is the entity's
    # THIRD-PARTY exposure, never its own insolvency, and is worded so (#356/#357).
    from src.synth.distress import TRUSTED_CONFIDENCE
    n_untrusted = 0
    for d in feed.get("distress") or []:
        if d.get("entity") not in scope_set:
            continue
        if d.get("confidence") not in TRUSTED_CONFIDENCE:
            n_untrusted += 1
            continue
        eid, role = d["entity"], d.get("role") or "self"
        src = ((d.get("sources") or [{}])[0] or {}).get("source") or "BCB"
        title = d.get("latest_title") or d.get("label") or ""
        if role == "self":
            fid, kind, text = f"f-distress-{_slug(eid)}-{_slug(d.get('kind'))}", "distress", \
                f"{eid}: {d.get('label')} — {title}"
        else:
            who = d.get("correspondent") or d.get("subject_name") or "terceiro"
            rel = "correspondente contratado" if role == "correspondent" else "empresa do grupo"
            fid, kind = f"f-third-party-{_slug(d.get('id') or eid)}", "third_party"
            text = (f"{eid}: exposição a terceiro ({rel}: {who}) em {d.get('label')} — {title}. "
                    f"Não é um regime sobre {eid}.")
        facts.append(_fact(fid, kind, eid, src, d.get("last_seen"), text))
    if n_untrusted:
        gaps.append(f"{n_untrusted} sinal(is) de insolvência com fonte única omitido(s) (não confirmados)")

    for x in feed.get("enforcement") or []:
        hit = scope_set & set([x.get("entity"), *(x.get("entities") or [])])
        if hit:
            facts.append(_fact(f"f-enforcement-{_slug(x['id'])}", "enforcement", sorted(hit)[0],
                               x.get("authority") or "oficial", x.get("date"), x.get("title") or ""))

    for m in (feed.get("sanctions") or {}).get("matches") or []:
        if m.get("entity") in scope_set:
            facts.append(_fact(f"f-sanctions-{_slug(m['entity'])}-{_slug(m.get('list') or m.get('id'))}",
                               "sanctions", m["entity"], m.get("list") or "listas de sanções",
                               (feed.get("sanctions") or {}).get("as_of"),
                               f"{m['entity']}: correspondência em lista de sanções ({m.get('list')})."))

    selic = (feed.get("macro") or {}).get("selic") or {}
    if selic.get("current") is not None:
        ld = selic.get("last_decision") or {}
        facts.append(_fact("f-macro-selic", "macro", None, "BCB Copom", selic.get("as_of"),
                           f"Selic {selic['current']}% (última decisão {ld.get('date')}: "
                           f"{ld.get('previous')}% → {ld.get('value')}%)."))
    for fo in (feed.get("macro") or {}).get("focus") or []:
        facts.append(_fact(f"f-macro-focus-{_slug(fo.get('indicator'))}-{fo.get('ref_year')}", "macro",
                           None, "BCB Focus", fo.get("date"),
                           f"Focus {fo.get('indicator')} {fo.get('ref_year')}: mediana {fo.get('median')} "
                           f"(anterior {fo.get('prev_median')})."))

    for fi in feed.get("financials") or []:
        if fi.get("entity_id") in scope_set:
            facts.append(_fact(f"f-financials-{_slug(fi['entity_id'])}", "financials", fi["entity_id"],
                               f"CVM {fi.get('doc')}", fi.get("period"),
                               f"{fi.get('name')}: {_fmt(fi, ('revenue', 'net_income', 'prior_net_income', 'net_margin', 'equity', 'assets'))} "
                               f"({fi.get('currency')}, período até {fi.get('period')})."))

    for rp in feed.get("reputation") or []:
        if rp.get("entity") in scope_set:
            facts.append(_fact(f"f-reputation-{_slug(rp['id'])}", "reputation", rp["entity"],
                               rp.get("source") or "BCB", rp.get("period") or rp.get("date"),
                               f"{rp.get('company')}: posição {rp.get('rank')} no ranking de reclamações "
                               f"({rp.get('category')}, índice {rp.get('index')}, {rp.get('period')})."))

    sw = (feed.get("swot") or {}).get(focal)
    if sw:
        lines = [f"{dim}: {b.get('text')}" for dim, bs in (sw.get("dimensions") or {}).items()
                 for b in bs if b.get("status") == "active"]
        if lines:
            facts.append(_fact(f"f-swot-{_slug(target)}", "swot", target, "Onça SWOT (inferência)",
                               max((b.get("latest") or "" for bs in sw["dimensions"].values() for b in bs),
                                   default=None), " | ".join(lines)))

    inds = set(ents[target].get("industries") or [])
    evs = [e for e in feed.get("sector_events") or []
           if (scope_set & set(e.get("entities") or [])) or e.get("industry") in inds]
    for ev in sorted(evs, key=lambda e: e.get("date") or "", reverse=True)[:MAX_EVENTS]:
        facts.append(_fact(f"f-sector-event-{_slug(ev['id'])}", "sector_event", None,
                           ((ev.get("sources") or [{}])[0] or {}).get("source") or "setorial",
                           ev.get("date"), ev.get("title") or ev.get("summary") or ""))

    # Narratives: the focal institution and the subject (up to MAX_NARRATIVES), plus each other
    # peer's latest PEER_NARRATIVES — a competitor's move must reach the officers, weighted by
    # its proximity fact (#386 dry run: Gigante's campaign was missing from the pack).
    def _nar_fact(x: Mapping[str, Any], ent: str) -> dict[str, Any]:
        text = x["narrative"]
        text = text if len(text) <= NARRATIVE_CHARS else text[:NARRATIVE_CHARS].rsplit(" ", 1)[0] + "…"
        return _fact(f"f-narrative-{_slug(x['id'])}", "narrative", ent,
                     f"Onça {x['kind']}" + (" (inferência)" if x.get("is_inference") else ""),
                     x.get("data_date") or x.get("date"), text)

    rows = sorted((x for x in feed.get("feed") or [] if x.get("kind") not in _SKIP_NARRATIVE
                   and x.get("narrative")), key=lambda x: x.get("date") or "", reverse=True)
    about = {focal, subject}
    for x in [x for x in rows if about & set(x.get("entities") or [])][:MAX_NARRATIVES]:
        facts.append(_nar_fact(x, subject if subject in (x.get("entities") or []) else focal))
    for peer in scope[1:]:
        if peer in about:
            continue
        mine = [x for x in rows if peer in (x.get("entities") or []) and not about & set(x.get("entities") or [])]
        facts += [_nar_fact(x, peer) for x in mine[:PEER_NARRATIVES]]

    # Stable order, unique ids (a repeated id would make a citation ambiguous).
    seen: set[str] = set()
    uniq = []
    for f in sorted(facts, key=lambda f: (PRIORITY.get(f["kind"], 9), f["id"])):
        if f["id"] not in seen:
            seen.add(f["id"])
            uniq.append(f)
    facts = uniq
    while facts and len(render(facts)) > max_chars:
        dropped = max(facts, key=lambda f: (PRIORITY.get(f["kind"], 9), f["id"]))
        facts.remove(dropped)
        gaps.append(f"omitido por limite de tamanho: {dropped['id']}")

    return {"schema": PACK_SCHEMA, "focal": focal, "subject": subject, "target": subject,
            "peers": [p for p in scope[1:]], "proximity": proximity,
            "feed_generated_at": feed.get("generated_at"), "feed_as_of": feed.get("as_of"),
            "facts": facts, "gaps": gaps}


def render(facts: list[dict[str, Any]]) -> str:
    """The text an officer reads (``converse(context=…)``): one line per fact, citable by id."""
    return "\n".join(f"[{f['id']}] ({f['source']}, {f['as_of'] or 's/d'}) {f['text']}" for f in facts)


def context_text(pack: Mapping[str, Any]) -> str:
    focal = pack.get("focal") or pack["target"]
    subject = pack.get("subject") or focal
    prox = {p["peer"]: p.get("score") for p in pack.get("proximity") or []}
    peers = ", ".join(f"{p} ({prox[p]:.2f})" if prox.get(p) is not None else p for p in pack["peers"])
    head = (f"PACOTE DE CONTEXTO {pack.get('sha256', '')} — instituição focal (o conselho que decide): "
            f"{focal}; assunto: {subject if subject != focal else 'a própria instituição focal'}; "
            f"concorrentes (proximidade à focal, 0 a 1): {peers or 'nenhum'}; feed de {pack.get('feed_as_of')}.")
    gaps = ("\nLACUNAS: " + "; ".join(pack["gaps"])) if pack.get("gaps") else ""
    return head + gaps + "\n" + render(pack["facts"])


def canonical(pack: Mapping[str, Any]) -> bytes:
    body = {k: v for k, v in pack.items() if k != "sha256"}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def seal(pack: dict[str, Any]) -> dict[str, Any]:
    """Return the pack with ``sha256`` set over its canonical form."""
    return {**pack, "sha256": hashlib.sha256(canonical(pack)).hexdigest()}


def fact_ids(pack: Mapping[str, Any]) -> set[str]:
    return {f["id"] for f in pack.get("facts") or []}


def proximity_of(pack: Mapping[str, Any]) -> dict[str, str]:
    """#386: every competitor fact id -> that competitor's ``f-proximity-*`` fact id. Facts
    about the focal institution, the market or no entity need no proximity."""
    prox = {f["entity"]: f["id"] for f in pack.get("facts") or [] if f.get("kind") == "proximity"}
    return {f["id"]: prox[f["entity"]] for f in pack.get("facts") or []
            if f.get("kind") != "proximity" and f.get("entity") in prox}


def pack_key(sha: str) -> str:
    return f"board/packs/{sha}.json"


def put_pack(s3: Any, bucket: str, pack: Mapping[str, Any]) -> str:
    """Store a sealed pack once (If-None-Match: a pack is immutable). Returns its key."""
    sha = pack.get("sha256")
    if not sha or hashlib.sha256(canonical(pack)).hexdigest() != sha:
        raise ValueError("pack is not sealed or its hash does not match")
    key = pack_key(sha)
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=canonical(pack), ContentType="application/json",
                      IfNoneMatch="*")
    except Exception as exc:  # an existing identical pack is fine; anything else is not
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code not in ("PreconditionFailed", "412"):
            raise
    return key


def get_pack(s3: Any, bucket: str, sha: str) -> dict[str, Any]:
    body = s3.get_object(Bucket=bucket, Key=pack_key(sha))["Body"].read()
    pack = json.loads(body)
    if hashlib.sha256(canonical(pack)).hexdigest() != sha:
        raise ValueError(f"stored pack {sha} fails its hash check")
    return {**pack, "sha256": sha}
