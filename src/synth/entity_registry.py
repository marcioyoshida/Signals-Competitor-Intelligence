"""Entities registry (DynamoDB) — ADR docs/2026-08-17-adr-entities-registry.md.

Single-table lookup design (O(1) exact resolution, no GSI):
  pk = "ENT#<entity_id>"    -> entity record (display_name, aliases, sector, ...)
  pk = "ALIAS#<norm>"       -> {entity_id}   (accent-folded name index)
  pk = "CNPJ#<root8>"       -> {entity_id}   (exact join key)

This file grows with the ADR rollout: the table + curated seed and `put_entity`
write primitive (step 1); the read helpers `resolve_entities` uses (step 2);
`auto_create_from_entrant` — CNPJ-keyed auto-create from BCB entrants (step 3);
and `accumulate_aliases` — data-derived alias accumulation from structured CVM
signals (step 4). Steps 5–7 (review queue, per-tenant config, UI) build on these.

Module-level imports stay free of src.synth.* so ingest can reuse the write
primitives later without pulling synth; the curated seed imports lazily.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import unicodedata
from decimal import Decimal
from typing import Any, Iterable


def _ddb_safe(obj: Any) -> Any:
    """Recursively coerce values into DynamoDB-storable types.

    boto3's DynamoDB resource rejects Python ``float`` ("Float types are not
    supported"); convert to ``Decimal`` (via str, to avoid binary-float drift).
    Used for free-form blobs like a review ``payload`` that may carry a fund's
    PL / cotistas as floats.
    """
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _ddb_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_ddb_safe(v) for v in obj]
    return obj


def normalize_alias(value: str) -> str:
    """Accent-stripped, uppercased, whitespace-collapsed key for name matching."""
    nfkd = unicodedata.normalize("NFKD", str(value or ""))
    folded = "".join(c for c in nfkd if not unicodedata.combining(c)).upper()
    return " ".join(folded.split())


def _table(table: Any | None = None) -> Any:
    if table is not None:
        return table
    import boto3

    return boto3.resource("dynamodb").Table(
        os.environ.get("ONCA_ENTITIES_TABLE", "onca-entities")
    )


# ADR 018 Phase 1 — per-field curation provenance. Every registry write stamps who/what
# set a field, so Phase 2's write-precedence can tell a curated value from an automated
# one. Ranked weakest→strongest: inferred < enrich < discovery < structured < curated <
# fixture (a human/API `curated` write, and the code seed, are the strongest).
PROV_SOURCES = ("inferred", "enrich", "discovery", "structured", "curated", "fixture")
PROV_RANK = {s: i for i, s in enumerate(PROV_SOURCES)}
# ADR 018 Phase 2 — protection level for write-precedence. A human/API `curated` edit and
# the code `fixture` seed are BOTH top (curated must be able to override the seed), so an
# automated writer can never demote either. Higher wins; a lower-ranked write is rejected.
_PROV_PROTECT = {"inferred": 0, "enrich": 2, "discovery": 2, "structured": 3,
                 "curated": 4, "fixture": 4}  # enrich==discovery (enrich follows discovery)


def _may_write(ent: dict[str, Any], field: str, source: str) -> bool:
    """Phase 2: may a write from `source` set `field`? A field with no provenance is open;
    otherwise the write must be at least as strong as the field's current provenance —
    this is the GENERAL form of the #52/ADR-017 point-guards (discovery can't demote a
    curated/fixture institution's industries)."""
    cur = (ent.get("_prov") or {}).get(field)
    if not cur:
        return True
    return _PROV_PROTECT.get(source, 0) >= _PROV_PROTECT.get(cur.get("source"), 0)


def _prov_entry(source: str, confidence: str | None = None) -> dict[str, Any]:
    import datetime as _dt

    entry = {"source": str(source),
             "set_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
    if confidence:  # omit None — keep the DynamoDB map free of NULLs
        entry["confidence"] = str(confidence)
    return entry


def entity_provenance(entity_id: str, table: Any | None = None) -> dict[str, Any]:
    """ADR 018: the per-field provenance map {field: {source, confidence?, set_at}}."""
    return (get_entity(entity_id, table=table) or {}).get("_prov") or {}


# ADR 018 Phase 1b — append-only mutation journal (audit + rollback substrate). Written to
# a SEPARATE table (ONCA_CURATION_LOG_TABLE) so it never bloats the entity scans; a no-op
# when unset (tests, local). Best-effort: a log failure never blocks the registry write.
_LOG_TABLE_CACHE: Any | None = None


def _log_table() -> Any | None:
    global _LOG_TABLE_CACHE
    name = os.environ.get("ONCA_CURATION_LOG_TABLE")
    if not name:
        return None
    if _LOG_TABLE_CACHE is None:
        import boto3

        _LOG_TABLE_CACHE = boto3.resource("dynamodb").Table(name)
    return _LOG_TABLE_CACHE


def _log(entity_id: str, action: str, source: str, detail: dict[str, Any] | None = None) -> None:
    t = _log_table()
    if t is None:
        return
    try:
        import datetime as _dt
        import uuid

        ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")
        t.put_item(Item=_ddb_safe({
            "entity_id": str(entity_id), "ts": f"{ts}#{uuid.uuid4().hex[:8]}",
            "action": str(action), "source": str(source), "detail": detail or {},
        }))
    except Exception as exc:  # pragma: no cover - best-effort, never blocks a write
        print(f"Warning: curation-log write failed for {entity_id}: {exc}")


def entity_history(entity_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """ADR 018: the mutation journal for one entity, newest first. [] if no log table."""
    t = _log_table()
    if t is None:
        return []
    try:
        from boto3.dynamodb.conditions import Key

        r = t.query(KeyConditionExpression=Key("entity_id").eq(str(entity_id)),
                    ScanIndexForward=False, Limit=limit)
        return list(r.get("Items") or [])
    except Exception:  # pragma: no cover
        return []


# ADR 018 Phase 4 — rollback over the journal. The log records each write's NEW value, so a
# field's value "before time T" is the newest logged value strictly earlier than T. Only the
# ownership fields (industries/parent) are rollback-supported; aliases are additive.
_MISSING = object()
_ROLLBACK_FIELDS = ("industries", "parent")


def _field_from_log(entry: dict[str, Any], field: str) -> Any:
    d = entry.get("detail") or {}
    action = entry.get("action")
    if action == "set_industries" and field == "industries":
        return d.get("new")
    if action == "set_parent" and field == "parent":
        return d.get("parent")
    if action == "put" and field in d:
        return d.get(field)
    return _MISSING


def field_value_before(entity_id: str, field: str, before_ts: str,
                       *, history: list[dict[str, Any]] | None = None) -> Any:
    """The field's value as of the last write strictly earlier than ``before_ts`` (the
    journal ts sorts lexically), or ``_MISSING`` if there is no prior value."""
    hist = history if history is not None else entity_history(entity_id)
    for h in hist:  # newest first
        if str(h.get("ts", "")) < str(before_ts):
            v = _field_from_log(h, field)
            if v is not _MISSING:
                return v
    return _MISSING


def rollback_field(entity_id: str, field: str, before_ts: str, *,
                   history: list[dict[str, Any]] | None = None, table: Any | None = None) -> bool:
    """Restore ``field`` to its value just before ``before_ts`` (a curated write, so it wins
    precedence and re-stamps provenance). Returns True if a rollback was applied."""
    if field not in _ROLLBACK_FIELDS:
        raise ValueError(f"rollback unsupported for {field!r}; one of {_ROLLBACK_FIELDS}")
    v = field_value_before(entity_id, field, before_ts, history=history)
    if v is _MISSING:
        return False
    ok = (set_industries(entity_id, v or [], source="curated", table=table) if field == "industries"
          else set_parent(entity_id, v or None, source="curated", table=table))
    if ok:
        _log(entity_id, "rollback", "curated", {"field": field, "restored": v, "before": before_ts})
    return ok


def revert_entity_since(entity_id: str, since_ts: str, *, table: Any | None = None) -> list[str]:
    """Roll back every rollback-supported field an entity changed at/after ``since_ts`` to
    its state just before. Returns the fields reverted (undoes a bad run in one shot)."""
    hist = entity_history(entity_id)
    touched = {f for h in hist if str(h.get("ts", "")) >= str(since_ts)
               for f in _ROLLBACK_FIELDS if _field_from_log(h, f) is not _MISSING}
    reverted = [f for f in touched
                if rollback_field(entity_id, f, since_ts, history=hist, table=table)]
    return reverted


def _stamp(entity: dict[str, Any], fields: Iterable[str], source: str,
           confidence: str | None = None) -> None:
    prov = entity.setdefault("_prov", {})
    entry = _prov_entry(source, confidence)
    for f in fields:
        prov[f] = dict(entry)


def put_entity(
    entity_id: str,
    display_name: str,
    aliases: Iterable[str],
    *,
    alias_forms: Iterable[str] | None = None,
    sector: str | None = None,
    industries: Iterable[str] | None = None,
    license_class: str | None = None,
    cnpj_roots: Iterable[str] = (),
    ticker: str | None = None,
    controllers: list[str] | None = None,
    confidence: str = "cnpj",
    canonical_id: str | None = None,
    news_term: str | None = None,
    ambiguous_tokens: Iterable[str] | None = None,
    fatos_term: str | None = None,
    news_search: bool = True,
    ownership: str | None = None,
    certifications: Iterable[str] | None = None,
    attribution_role: str | None = None,
    parent: str | None = None,
    source: str = "curated",
    prov: dict[str, Any] | None = None,
    table: Any | None = None,
) -> dict[str, Any]:
    """Upsert an entity + its ALIAS#/CNPJ# lookup items. Returns the entity item.

    ``aliases`` seed the normalized ALIAS# index (exact lookup). ``alias_forms``
    (defaults to ``aliases``) are the *raw* strings used by resolve_entities'
    substring matching — preserving curated hacks like "STONE " / "TICKER:STNE".
    """
    t = _table(table)
    raw = [str(a) for a in (alias_forms if alias_forms is not None else aliases) if str(a).strip()]
    norm = sorted(
        {normalize_alias(a) for a in aliases if str(a).strip() and not str(a).upper().startswith("TICKER:")}
    )
    roots = sorted({str(r)[:8] for r in cnpj_roots if str(r).strip()})
    entity = {
        "pk": f"ENT#{entity_id}",
        "type": "entity",
        "entity_id": entity_id,
        "display_name": display_name,
        "aliases": norm,
        "alias_forms": raw,
        "cnpj_roots": roots,
        "controllers": controllers or [],
        "confidence": confidence,
        "active": True,
        "canonical_id": canonical_id or entity_id,
    }
    inds = sorted({str(i).strip().lower() for i in (industries or ()) if str(i).strip()})
    if inds:
        entity["industries"] = inds
    if sector:
        entity["sector"] = sector
    if license_class:
        entity["license_class"] = license_class
    if ticker:
        entity["ticker"] = ticker
    # Curation for SEARCH, held on the entity record so it is API-editable with no
    # code deploy (was hardcoded NEWS_TERM_OVERRIDES / AMBIGUOUS_TOKENS):
    #  - news_term: the Google-News query phrase (overrides the display_name default),
    #  - ambiguous_tokens: this entity's bare tokens that are common words, so they
    #    may resolve only from a structured identity source, never a free-text
    #    headline (e.g. "STONE"; but "STONECO" stays distinctive). `ambiguous` is a
    #    convenience flag = the list is non-empty.
    if news_term:
        entity["news_term"] = str(news_term)
    #  - fatos_term: the CVM issuer-name phrase used to pull this entity's material
    #    facts (Fato Relevante / Comunicado ao Mercado) — a STRUCTURED identity
    #    source, so a B3-listed entity resolves from its filing, not a fragile
    #    news headline. Only set for entities that actually file (listed issuers).
    #  - news_search: False makes the entity structured-only for news — it is not
    #    added to the Google-News query set (e.g. "Porto Seguro" collides with the
    #    Bahia city; its identity comes from the fatos_term instead).
    if fatos_term:
        entity["fatos_term"] = str(fatos_term)
    if not news_search:
        entity["news_search"] = False
    if ambiguous_tokens is not None:
        toks = sorted(
            {
                str(a).upper().strip()
                for a in ambiguous_tokens
                if str(a).strip() and " " not in str(a).strip()
            }
        )
        entity["ambiguous_tokens"] = toks
        entity["ambiguous"] = bool(toks)
    # Curated CLASSIFICATION attributes (queryable entity facts, API-editable):
    #  - ownership: legal/control nature — public (companhia aberta/listed),
    #    governmental (wholly state-owned), mixed (sociedade de economia mista —
    #    public control + private capital), or private.
    #  - certifications: compliance/certification labels (e.g. "ISO 27001",
    #    "PCI-DSS") — evidenced/curated, never assumed.
    if ownership:
        entity["ownership"] = str(ownership)
    if certifications is not None:
        entity["certifications"] = sorted(
            {str(c).strip() for c in certifications if str(c).strip()}
        )
    #  - attribution_role: how news naming this entity is attributed — `competitor`
    #    (default, lenient) vs `operator`/`data_provider`/`regulator` (bind to
    #    subject only; the entity is often the actor/source in others' news).
    if attribution_role:
        entity["attribution_role"] = str(attribution_role)
    #  - parent: the tier-1 conglomerate this entity is a line-of-business of (ADR 017).
    #    A sub-entity (e.g. a bank's FIAGRO/consórcio arm) links to its parent so the
    #    parent can stay tagged tier-1 only while the lower-industry activity lives here;
    #    the tier-1 opt-in toggle folds a parent's children back in on demand.
    if parent:
        entity["parent"] = str(parent)
    # ADR 018: stamp provenance. A full rebuild (seed/create) stamps every field it set
    # with `source`; a delegating setter (assign_ticker) passes a pre-built `prov` so the
    # other fields keep their existing provenance and only the changed one is re-stamped.
    if prov is not None:
        entity["_prov"] = prov
    else:
        _stamp(entity, [k for k in ("industries", "aliases", "cnpj_roots", "ticker",
                                    "parent", "ownership", "certifications", "attribution_role")
                        if k in entity], source, confidence)
    t.put_item(Item=entity)
    for na in norm:
        t.put_item(Item={"pk": f"ALIAS#{na}", "type": "alias", "entity_id": entity_id})
    for r in roots:
        t.put_item(Item={"pk": f"CNPJ#{r}", "type": "cnpj", "entity_id": entity_id})
    _log(entity_id, "put", source,  # ADR 018 Phase 1b
         {"industries": entity.get("industries"), "ticker": entity.get("ticker")})
    return entity


def get_entity(entity_id: str, table: Any | None = None) -> dict[str, Any] | None:
    return _table(table).get_item(Key={"pk": f"ENT#{entity_id}"}).get("Item")


# ADR 011 §2 — B3 ticker → entity. Curated map of tracked, currently-B3-listed
# entities to their primary B3 ticker (ON/PN/UNIT), or the B3 **BDR** code for the
# foreign-primary fintechs (Nubank/XP/Stone/PagSeguro/Inter trade on Nasdaq/NYSE;
# their B3 representation is a BDR). Conservative on purpose — only confident,
# active listings (precision > recall; the discovery scan proposes the rest).
B3_TICKERS: dict[str, str] = {
    "itau": "ITUB4", "bb": "BBAS3", "bradesco": "BBDC4", "santander": "SANB11",
    "btg": "BPAC11", "b3": "B3SA3", "porto_seguro": "PSSA3", "bb_seguridade": "BBSE3",
    "caixa_seguridade": "CXSE3", "banco_pan": "BPAN4", "abc_brasil": "ABCB4",
    "bmg": "BMGB4", "banco_pine": "PINE4",
    # B3 BDRs of the foreign-listed fintechs:
    "nubank": "ROXO34", "xp": "XPBR31", "stone": "STOC31", "pagseguro": "PAGS34",
    "inter": "INBR32",
}


def assign_ticker(entity_id: str, ticker: str, *, source: str = "enrich",
                  table: Any | None = None) -> bool:
    """Set an existing entity's B3 ``ticker`` and make the ticker resolvable.

    Idempotent. Adds the ticker to the entity's aliases/alias_forms so ticker-keyed
    news/filings ("...ITUB4...") cluster into the entity (a B3 ticker is a distinctive
    4-letter+digit token, safe as a non-ambiguous alias). Returns True if it changed.
    Enrichment of an EXISTING entity only — never creates one (that's the discovery
    scan's curated path)."""
    t = _table(table)
    e = get_entity(entity_id, table=t)
    if not e:
        return False
    ticker = str(ticker).strip().upper()
    forms = list(e.get("alias_forms") or [])
    if e.get("ticker") == ticker and ticker in forms:
        return False  # already assigned
    if ticker not in forms:
        forms.append(ticker)
    # Re-upsert through put_entity so the ALIAS# index + all curation fields are
    # rebuilt consistently (put_entity is the single writer of the entity record).
    put_entity(
        entity_id,
        e.get("display_name") or entity_id,
        list(e.get("aliases") or []) + [ticker],
        alias_forms=forms,
        sector=e.get("sector"),
        industries=e.get("industries"),
        license_class=e.get("license_class"),
        cnpj_roots=e.get("cnpj_roots") or [],
        ticker=ticker,
        controllers=e.get("controllers"),
        confidence=e.get("confidence", "curated"),
        canonical_id=e.get("canonical_id"),
        news_term=e.get("news_term"),
        ambiguous_tokens=e.get("ambiguous_tokens"),
        fatos_term=e.get("fatos_term"),
        news_search=e.get("news_search", True),
        ownership=e.get("ownership"),
        certifications=e.get("certifications"),
        # ADR 018: preserve other fields' provenance, re-stamp only `ticker`.
        prov={**(e.get("_prov") or {}), "ticker": _prov_entry(source)},
        table=t,
    )
    return True


def backfill_tickers(table: Any | None = None) -> list[tuple[str, str]]:
    """Assign the curated B3 tickers to existing entities. Returns [(id, ticker)]
    actually changed. Idempotent — safe to re-run (seed-style migration)."""
    t = _table(table)
    changed = []
    for eid, ticker in B3_TICKERS.items():
        if assign_ticker(eid, ticker, table=t):
            changed.append((eid, ticker))
    return changed


# --- Ownership / control nature (curated + derived) -----------------------
# The four-way legal/control classification (owner-requested). Most tracked
# entities DERIVE (listed -> public; else private); this curated map holds only
# the non-derivable cases: wholly state-owned (governmental) and sociedades de
# economia mista (mixed — public control + private capital, usually also listed,
# so they must override the plain "public" derivation).
OWNERSHIP_VALUES = ("public", "governmental", "mixed", "private")
OWNERSHIP: dict[str, str] = {
    "caixa": "governmental",          # 100% federal (empresa pública), não listada
    "bndes": "governmental",          # banco de desenvolvimento, 100% federal
    "bb": "mixed",                    # Banco do Brasil — sociedade de economia mista
    "bb_seguridade": "mixed",         # controlada listada do BB
    "caixa_seguridade": "mixed",      # controlada listada da Caixa
    "banrisul": "mixed",              # controle do Estado do RS, listada
    "banco_do_nordeste": "governmental",
    "banestes": "mixed",
}


def classify_ownership(entity: dict[str, Any]) -> str:
    """Derive an entity's control nature. Curated override wins; else a listed
    issuer (has a ticker or a CVM fatos identity) is `public`; otherwise `private`."""
    eid = str(entity.get("entity_id") or "")
    if eid in OWNERSHIP:
        return OWNERSHIP[eid]
    if entity.get("ownership") in OWNERSHIP_VALUES:
        return entity["ownership"]
    if entity.get("ticker") or entity.get("fatos_term"):
        return "public"
    return "private"


def backfill_ownership(table: Any | None = None) -> list[tuple[str, str]]:
    """Set `ownership` on every active entity from classify_ownership. Idempotent;
    returns [(id, ownership)] actually changed."""
    t = _table(table)
    changed: list[tuple[str, str]] = []
    for e in list_entities(table=t):
        want = classify_ownership(e)
        if e.get("ownership") != want:
            update_entity(e["entity_id"], {"ownership": want}, table=t)
            changed.append((e["entity_id"], want))
    return changed


# --- Compliance / certifications (curated, evidenced — never assumed) ------
# Certification claims are accuracy-critical (a company is/ isn't ISO-certified),
# so this seed is intentionally conservative: populate only from a verifiable
# source (the company's own disclosure / an accredited registry). Empty entries
# are fine — the attribute is *scoped and queryable* even before it is filled,
# via analyst curation or a future evidence-backed detector.
CERTIFICATIONS: dict[str, list[str]] = {}


def set_certifications(
    entity_id: str, certs: Iterable[str], *, table: Any | None = None
) -> bool:
    """Set an entity's curated certification list (non-destructive to other
    fields). Returns True if changed."""
    t = _table(table)
    e = get_entity(entity_id, table=t)
    if not e:
        return False
    want = sorted({str(c).strip() for c in certs if str(c).strip()})
    if sorted(e.get("certifications") or []) == want:
        return False
    update_entity(entity_id, {"certifications": want}, table=t)
    return True


def backfill_certifications(table: Any | None = None) -> list[str]:
    """Apply the curated CERTIFICATIONS seed to existing entities. Idempotent."""
    t = _table(table)
    changed: list[str] = []
    for eid, certs in CERTIFICATIONS.items():
        if set_certifications(eid, certs, table=t):
            changed.append(eid)
    return changed


# --- ESG standing (proxy: B3 ISE membership, issue #30) --------------------
# "Which banks have an ESG rating?" landed as a coverage gap because no source
# ingests it. Proprietary agency ratings (MSCI ESG, Sustainalytics, S&P Global
# ESG) are paid/access-gated — confirmed live 2026-08-30 (Sustainalytics'
# public company page 301-redirects into a gated flow; MSCI's tool is a
# lead-gen search page, not a data feed; CDP has no public bulk-disclosure
# API). BCB's GRSAC report (Resolução CMN 4.945/2021) is a real disclosure
# regime but is published per-institution as a PDF on each bank's own site,
# not centralized/machine-readable by BCB.
#
# The best FREE, OPEN, *citable* proxy is **B3 ISE membership**: B3's own
# Índice de Sustentabilidade Empresarial (ISE B3) is a public equity index
# whose annual constituent selection *is* an ESG standing signal (companies
# apply, are scored on a public questionnaire, and only the top cohort is
# admitted). `src/ingest/esg_ise_b3.py` fetches the live portfolio from B3's
# own (undocumented but working) `indexProxy/GetPortfolioDay` JSON endpoint —
# no auth, no key. Modeled the same way as `certifications`: curated/evidenced,
# never assumed, empty until backfilled from a real fetch.
ESG_KEYS = ("ise_b3", "ise_b3_cycle", "ise_b3_weight_pct", "as_of", "source_url")


def set_esg(entity_id: str, esg: dict[str, Any] | None, *, source: str = "structured",
            table: Any | None = None) -> bool:
    """Set an entity's curated `esg` signal dict (e.g. {"ise_b3": True,
    "ise_b3_cycle": "2026-2027", "weight_pct": 2.809, "as_of": "2026-08-31",
    "source_url": "..."}). Non-destructive to other fields. Returns True if
    changed. `esg=None`/`{}` clears the attribute (e.g. dropped from the index
    on the next annual rebalance)."""
    t = _table(table)
    e = get_entity(entity_id, table=t)
    if not e:
        return False
    want = {k: esg[k] for k in ESG_KEYS if esg and k in esg and esg[k] is not None} if esg else {}
    # Normalize through _ddb_safe (float -> Decimal) before comparing, so a
    # re-run comparing against an already-persisted (Decimal-typed) value is
    # idempotent rather than "changed" on every call.
    want_safe = _ddb_safe(want)
    if (e.get("esg") or {}) == want_safe:
        return False
    prov = {**(e.get("_prov") or {}), "esg": _prov_entry(source)}  # ADR 018
    update_entity(entity_id, {"esg": want_safe, "_prov": prov}, table=t)
    _log(entity_id, "set_esg", source, {"ise_b3": bool(want.get("ise_b3"))})
    return True


def backfill_esg_ise_b3(
    portfolio: dict[str, Any], *, table: Any | None = None
) -> list[tuple[str, dict[str, Any]]]:
    """Apply a fetched ISE B3 portfolio (see `esg_ise_b3.fetch_portfolio` +
    `match_tracked_entities`) to the registry via the curated B3 ticker map.
    Idempotent; returns [(entity_id, esg_dict)] actually changed. A tracked
    entity that HAD `ise_b3: true` but is no longer in this cycle's portfolio
    is explicitly cleared here (annual rebalance can drop a member — leaving
    a stale `ise_b3: true` would be a fabricated claim)."""
    t = _table(table)
    changed: list[tuple[str, dict[str, Any]]] = []
    tickers_in_portfolio = {c.get("ticker") for c in portfolio.get("constituents", [])}
    for eid, ticker in B3_TICKERS.items():
        hit = next((c for c in portfolio.get("constituents", []) if c.get("ticker") == ticker), None)
        if hit:
            esg = {
                "ise_b3": True,
                "ise_b3_cycle": portfolio.get("cycle"),
                "ise_b3_weight_pct": hit.get("weight_pct"),
                "as_of": portfolio.get("as_of"),
                # cite the human ISE B3 page, not the base64 API endpoint.
                "source_url": portfolio.get("page_url") or portfolio.get("source_url"),
            }
            if set_esg(eid, esg, table=t):
                changed.append((eid, esg))
        elif ticker not in tickers_in_portfolio:
            e = get_entity(eid, table=t)
            if e and (e.get("esg") or {}).get("ise_b3"):
                if set_esg(eid, None, table=t):
                    changed.append((eid, {}))
    return changed


# --- Attribution role (news-subject binding, issue #33) -------------------
# Some tracked entities are habitually the *actor / venue / source* of news that
# is really ABOUT another company — a market operator (B3) delists/suspends other
# issuers, a data vendor / credit bureau (Serasa, Economatica, Boa Vista, Quod) is
# cited for figures about others. Attributing those third-party-subject narratives
# to them poisons every downstream signal (feed, threat scores, SWOT, distress —
# see the B3 "recuperação extrajudicial" false positive). This curated role tells
# resolve_entities to attribute such an entity ONLY when it is the story's subject,
# not when it is merely the actor/source. `competitor` (default) keeps the normal,
# lenient attribution — a competitor that also acts (e.g. Cielo antecipando
# recebíveis) is never suppressed.
ATTRIBUTION_ROLES = ("competitor", "operator", "data_provider", "regulator", "advisor")
ATTRIBUTION_ROLE: dict[str, str] = {
    # advisor: investment banks named in OTHERS' news as the analyst/underwriter/
    # advisor ("segundo o JP Morgan", "IPO coordenado pela X", "escolheu a X para
    # liderar") — not the story's subject. They ARE competitors (advisory/IB
    # modules), so this only raises the bar: a genuine subject mention or a
    # structured/ticker match still resolves (issue #38).
    "jpmorgan": "advisor",
    "goldman_sachs": "advisor",
    "morgan_stanley": "advisor",
    "bank_of_america": "advisor",
    "bofa": "advisor",
    "citi": "advisor",
    "citigroup": "advisor",
    "ubs": "advisor",
    "barclays": "advisor",
    "jefferies": "advisor",
    "lazard": "advisor",
    "evercore": "advisor",
    "b3": "operator",                 # bolsa/depositária — delista/suspende emissores
    "cvm": "regulator",
    "bcb": "regulator",
    "bacen": "regulator",
    "susep": "regulator",
    "previc": "regulator",
    "coaf": "regulator",
    "cade": "regulator",
    "serasa": "data_provider",        # bureau de crédito — citado por dados de terceiros
    "serasa_experian": "data_provider",
    "boa_vista": "data_provider",
    "quod": "data_provider",
    "economatica": "data_provider",   # provedor de dados de mercado
    "anbima": "data_provider",
    "neurotech": "data_provider",
}


def classify_attribution_role(entity: dict[str, Any]) -> str:
    """An entity's news-attribution role. Curated seed wins; else a written
    `attribution_role`; else `competitor` (the default, lenient attribution)."""
    eid = str(entity.get("entity_id") or "")
    if eid in ATTRIBUTION_ROLE:
        return ATTRIBUTION_ROLE[eid]
    if entity.get("attribution_role") in ATTRIBUTION_ROLES:
        return entity["attribution_role"]
    return "competitor"


def backfill_attribution_roles(table: Any | None = None) -> list[tuple[str, str]]:
    """Set `attribution_role` on every active entity from
    classify_attribution_role. Idempotent; returns [(id, role)] changed."""
    t = _table(table)
    changed: list[tuple[str, str]] = []
    for e in list_entities(table=t):
        want = classify_attribution_role(e)
        if e.get("attribution_role") != want:
            update_entity(e["entity_id"], {"attribution_role": want}, table=t)
            changed.append((e["entity_id"], want))
    return changed


def set_attribution_role(entity_id: str, role: str, *, table: Any | None = None) -> bool:
    """Curate an entity's attribution role (must be a known role). True if changed."""
    if role not in ATTRIBUTION_ROLES:
        raise ValueError(f"unknown attribution_role: {role!r}")
    t = _table(table)
    e = get_entity(entity_id, table=t)
    if not e or e.get("attribution_role") == role:
        return False
    update_entity(entity_id, {"attribution_role": role}, table=t)
    return True


_ROLE_MAP_CACHE: dict[str, str] | None = None
_SUBENTITY_CACHE: "dict[str, list[tuple[str, frozenset[str]]]] | None" = None


def load_subentities(
    table: Any | None = None, force: bool = False
) -> dict[str, list[tuple[str, frozenset[str]]]]:
    """{parent_id: [(child_id, frozenset(industries))]} — ADR 017 corporate groups,
    cached. Drives news→sub-entity re-attribution (issue #47)."""
    global _SUBENTITY_CACHE
    if _SUBENTITY_CACHE is not None and not force:
        return _SUBENTITY_CACHE
    out: dict[str, list[tuple[str, frozenset[str]]]] = {}
    for e in list_entities(table=table):
        p = e.get("parent")
        if p:
            out.setdefault(str(p), []).append(
                (e["entity_id"], frozenset(str(i).lower() for i in (e.get("industries") or [])))
            )
    _SUBENTITY_CACHE = out
    return out


def load_attribution_roles(table: Any | None = None, force: bool = False) -> dict[str, str]:
    """{entity_id: attribution_role} for every active entity (cached), curated
    seed overriding any written value. Drives resolve_entities' subject-binding."""
    global _ROLE_MAP_CACHE
    if _ROLE_MAP_CACHE is not None and not force:
        return _ROLE_MAP_CACHE
    roles: dict[str, str] = {}
    for e in list_entities(table=table):
        roles[e["entity_id"]] = classify_attribution_role(e)
    _ROLE_MAP_CACHE = roles
    return roles


def list_entity_attributes(table: Any | None = None) -> dict[str, dict[str, Any]]:
    """Compact per-entity classification map for the feed/agent: every active
    entity → {label, ownership, certifications, ticker, industries,
    attribution_role, esg}. Ownership/role are always derived (present even
    when not yet written); esg is empty ({}) until backfilled from a real
    fetch (ise_b3 membership — see esg_ise_b3.py, issue #30)."""
    out: dict[str, dict[str, Any]] = {}
    for e in list_entities(table=table):
        out[e["entity_id"]] = {
            "label": e.get("display_name") or e["entity_id"],
            "ownership": classify_ownership(e),
            "certifications": e.get("certifications") or [],
            "ticker": e.get("ticker"),
            "industries": e.get("industries") or [],
            "attribution_role": classify_attribution_role(e),
            "esg": e.get("esg") or {},
            # ADR 017: the tier-1 conglomerate this entity is a sub-entity of, if any —
            # lets the dashboard fold a parent's lower-industry group in on demand.
            "parent": e.get("parent"),
        }
    return out


def resolve_by_alias(name: str, table: Any | None = None) -> str | None:
    item = _table(table).get_item(Key={"pk": f"ALIAS#{normalize_alias(name)}"}).get("Item")
    return item.get("entity_id") if item else None


def resolve_by_cnpj(cnpj_root: str, table: Any | None = None) -> str | None:
    root = "".join(ch for ch in str(cnpj_root or "") if ch.isdigit())[:8]
    if not root:
        return None
    item = _table(table).get_item(Key={"pk": f"CNPJ#{root}"}).get("Item")
    return item.get("entity_id") if item else None


def resolve_by_name(name: str, table: Any | None = None) -> list[str]:
    """Return every entity_id a name resolves to — the normalized ALIAS# index
    first, then any entity whose display_name normalizes to the same key.

    A discovery/curation helper (not a hot path): it returns a *list* so a caller
    can distinguish a unique hit (safe to enrich) from an ambiguous one (send to
    review), and an empty list when the name is unknown. Unlike ``resolve_by_alias``
    (exact alias index only) this also matches display names, which structured
    discovery keys on before an entity has accumulated aliases.
    """
    na = normalize_alias(name)
    if not na:
        return []
    t = _table(table)
    hits: list[str] = []
    alias = t.get_item(Key={"pk": f"ALIAS#{na}"}).get("Item")
    if alias and alias.get("entity_id"):
        hits.append(alias["entity_id"])
    for e in _scan_type(t, "entity"):
        eid = e.get("entity_id")
        if eid and eid not in hits and normalize_alias(e.get("display_name") or "") == na:
            hits.append(eid)
    return hits


def name_owned_by_other(
    name: str, *, exclude_id: str | None = None, table: Any | None = None
) -> bool:
    """True when ``name`` already resolves to a *different* entity.

    The guard the discovery auto-create path uses so a newly-found fund/brand
    never hijacks a name a curated entity already owns (StoneX must not steal
    StoneCo's name). Mirrors the inline owner check in ``accumulate_aliases``.
    """
    return any(eid != exclude_id for eid in resolve_by_name(name, table=table))


def _slug(value: str) -> str:
    """Readable, ascii entity_id from a name (accent-folded, lowercase, _-joined)."""
    s = re.sub(r"[^a-z0-9]+", "_", normalize_alias(value).lower()).strip("_")
    return s[:40]


def auto_create_from_entrant(entrant: dict[str, Any], *, table: Any | None = None) -> str | None:
    """ADR step 3: CNPJ-keyed auto-create of an entity from a new BCB entrant.

    Makes a quietly-registered fintech resolvable for *future* signals (a later
    CVM offering, DOU act, or news headline) without a redeploy. Expects the
    entrant already enriched by Receita (``trade_name`` / ``legal_name`` /
    ``controllers``). Idempotent by CNPJ root; returns the entity_id when a new
    record is written, else ``None`` (already mapped, or no CNPJ to key on).

    Writes ``confidence="cnpj"`` — the safe, auto-committable case in the ADR.
    Grouping this CNPJ under a parent brand (``canonical_id``) stays a
    review-queue decision (step 5), so this never merges into a curated entity.
    """
    root = "".join(ch for ch in str(entrant.get("cnpj") or "") if ch.isdigit())[:8]
    if len(root) < 8:
        return None
    t = _table(table)
    if resolve_by_cnpj(root, table=t):  # already known — idempotent no-op
        return None

    brand = str(entrant.get("trade_name") or "").strip()
    legal = str(entrant.get("legal_name") or entrant.get("name") or "").strip()
    # Raw substring forms resolve_entities matches against future signal blobs.
    forms: list[str] = []
    for v in (brand, legal, str(entrant.get("name") or "").strip()):
        if len(v) >= 4 and v.upper() not in {f.upper() for f in forms}:
            forms.append(v)
    if not forms:
        return None

    display = brand or legal or f"CNPJ {root}"
    entity_id = _slug(brand or legal) or f"ent_{root}"
    existing = get_entity(entity_id, table=t)
    if existing and root not in (existing.get("cnpj_roots") or []):
        entity_id = f"{entity_id}_{root}"  # never clobber a different entity

    inds, _needs_review = classify_industries(entrant)
    put_entity(
        entity_id,
        display,
        forms,
        alias_forms=forms,
        cnpj_roots=[root],
        controllers=entrant.get("controllers") or None,
        license_class=entrant.get("license_class"),
        sector="fintech" if entrant.get("is_fintech") else None,
        industries=inds or None,
        confidence="cnpj",
        table=t,
    )
    return entity_id


def accumulate_aliases(
    entity_id: str,
    forms: Iterable[str],
    *,
    source: str = "enrich",
    table: Any | None = None,
) -> list[str]:
    """ADR step 4: fold data-derived name forms into a resolved entity's aliases.

    When a structured signal (a CVM offering's razão social, a fato relevante's
    company name) names an entity we *already* resolve by CNPJ, add that name so
    future name-only signals (news, DOU) about it resolve too — recall grows the
    more an entity appears. Only *data-derived* forms belong here; fuzzy or
    colloquial nicknames need review (step 5), never auto-commit.

    Idempotent: writes only genuinely-new forms. A normalized name already owned
    by a *different* entity is left untouched (StoneX must not steal StoneCo's
    name) and skipped for the review queue. Returns the raw forms actually added.
    """
    t = _table(table)
    ent = get_entity(entity_id, table=t)
    if not ent:
        return []
    norm_set = set(ent.get("aliases") or [])
    cur_forms = list(ent.get("alias_forms") or [])
    forms_upper = {f.upper() for f in cur_forms}

    added: list[str] = []
    new_norm: list[str] = []
    for raw in forms:
        f = str(raw or "").strip()
        if len(f) < 4:  # too short to be a safe substring key for resolve_entities
            continue
        if f.upper() not in forms_upper:
            cur_forms.append(f)
            forms_upper.add(f.upper())
            added.append(f)
        na = normalize_alias(f)
        if not na or na in norm_set:
            continue
        owner = t.get_item(Key={"pk": f"ALIAS#{na}"}).get("Item")
        if owner and owner.get("entity_id") not in (None, entity_id):
            continue  # another entity owns this name — leave it for review (step 5)
        norm_set.add(na)
        new_norm.append(na)

    if not added and not new_norm:
        return []  # nothing new — skip the write

    ent["aliases"] = sorted(norm_set)
    ent["alias_forms"] = cur_forms
    _stamp(ent, ["aliases"], source)  # ADR 018
    t.put_item(Item=ent)
    for na in new_norm:
        t.put_item(Item={"pk": f"ALIAS#{na}", "type": "alias", "entity_id": entity_id})
    _log(entity_id, "accumulate_aliases", source, {"added": added})
    return added


def strip_aliases(
    entity_id: str,
    forms: Iterable[str],
    *,
    table: Any | None = None,
) -> list[str]:
    """Remove specific raw alias forms from an entity (and their ``ALIAS#`` index
    items, when still owned by this entity). Idempotent; preserves every other
    field. Returns the raw forms actually removed.

    The inverse of ``accumulate_aliases`` — used to undo data-quality pollution
    where non-identity strings (e.g. a shared administrator/servicer legal name)
    were wrongly indexed as identity aliases and caused resolve_entities to fan a
    signal out to every entity sharing that string. Never touches ``cnpj_roots``,
    ``display_name``, ``ticker``, or aliases not listed in ``forms``.
    """
    t = _table(table)
    ent = get_entity(entity_id, table=t)
    if not ent:
        return []
    strip_norm = {normalize_alias(f) for f in forms if str(f).strip()}
    if not strip_norm:
        return []
    cur_forms = list(ent.get("alias_forms") or [])
    kept_forms = [f for f in cur_forms if normalize_alias(f) not in strip_norm]
    removed = [f for f in cur_forms if normalize_alias(f) in strip_norm]
    if not removed:
        return []
    kept_norm = sorted(
        {
            normalize_alias(f)
            for f in kept_forms
            if str(f).strip() and not str(f).upper().startswith("TICKER:")
        }
    )
    ent["alias_forms"] = kept_forms
    ent["aliases"] = kept_norm
    t.put_item(Item=ent)
    # Delete ALIAS# items for norms we removed and no kept form still maps to,
    # but only when the index still points at THIS entity (don't steal another's).
    for na in {normalize_alias(f) for f in removed} - set(kept_norm):
        if not na:
            continue
        item = t.get_item(Key={"pk": f"ALIAS#{na}"}).get("Item")
        if item and item.get("entity_id") == entity_id:
            t.delete_item(Key={"pk": f"ALIAS#{na}"})
    return removed


def add_cnpj_roots(
    entity_id: str,
    roots: Iterable[str],
    *,
    table: Any | None = None,
) -> list[str]:
    """Attach CNPJ 8-digit roots to an entity, writing the CNPJ# lookup items.

    The counterpart of :func:`accumulate_aliases` for the exact join key. Needed
    because ``cnpj_roots`` is protected from the generic patch path (it requires the
    CNPJ# reindex done here). Non-destructive (preserves every other field), idempotent
    (skips roots the entity already has), and it never **steals** a root already owned
    by a *different* entity (returns it unwritten). Returns the roots actually added.
    """
    t = _table(table)
    ent = get_entity(entity_id, table=t)
    if not ent:
        return []
    cur = set(ent.get("cnpj_roots") or [])
    added: list[str] = []
    for r in roots:
        root = "".join(ch for ch in str(r or "") if ch.isdigit())[:8]
        if len(root) < 8 or root in cur:
            continue
        owner = t.get_item(Key={"pk": f"CNPJ#{root}"}).get("Item")
        if owner and owner.get("entity_id") not in (None, entity_id):
            continue  # another entity owns this CNPJ — never steal it
        cur.add(root)
        added.append(root)
    if not added:
        return []
    ent["cnpj_roots"] = sorted(cur)
    t.put_item(Item=ent)
    for root in added:
        t.put_item(Item={"pk": f"CNPJ#{root}", "type": "cnpj", "entity_id": entity_id})
    return added


# --- Review queue (ADR step 5) -------------------------------------------------
# The "propose, don't auto-commit" cases (grouping CNPJs under one brand, fuzzy
# name matches, colloquial nicknames) never mutate an entity directly. They queue
# a REVIEW# item a human approves/rejects — approval applies the change, rejection
# records the decision so the producer won't re-propose it. Same single table:
#   pk = "REVIEW#<review_id>" -> { kind, entity_id, target_id, proposed, status, ... }


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _scan_type(t: Any, type_: str) -> list[dict[str, Any]]:
    """Return all items of a given ``type`` (paginated scan)."""
    out: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {}
    while True:
        resp = t.scan(**kwargs)
        out.extend(it for it in resp.get("Items", []) if it.get("type") == type_)
        start = resp.get("LastEvaluatedKey")
        if not start:
            break
        kwargs["ExclusiveStartKey"] = start
    return out


def _review_id(kind: str, key: str) -> str:
    """Stable id so a producer re-run proposes the same thing at most once."""
    slug = re.sub(r"[^a-z0-9]+", "_", normalize_alias(key).lower()).strip("_")
    return f"{kind}:{slug}"[:120]


def propose_review(
    kind: str,
    *,
    key: str,
    entity_id: str | None = None,
    target_id: str | None = None,
    proposed: str | None = None,
    reason: str = "",
    hint: str = "",
    confidence: str = "fuzzy",
    payload: dict[str, Any] | None = None,
    table: Any | None = None,
) -> str | None:
    """Queue a human-review proposal. Idempotent by (kind, key): an already
    queued OR already decided proposal is left untouched (never reopened, never
    duplicated). Returns the review_id if newly queued, else ``None``.

    ``payload`` carries structured evidence for the curator (e.g. a discovery
    candidate's profile, source, doc_count, sample titles) — surfaced in the
    review UI but never auto-applied; stored verbatim on the review item.
    """
    t = _table(table)
    rid = _review_id(kind, key)
    if t.get_item(Key={"pk": f"REVIEW#{rid}"}).get("Item"):
        return None
    t.put_item(
        Item={
            "pk": f"REVIEW#{rid}",
            "type": "review",
            "review_id": rid,
            "kind": kind,
            "entity_id": entity_id,
            "target_id": target_id,
            "proposed": proposed,
            "reason": reason,
            "hint": hint,
            "confidence": confidence,
            "payload": _ddb_safe(payload or {}),
            "status": "pending",
            "created_at": _now_iso(),
        }
    )
    return rid


def list_reviews(status: str | None = "pending", table: Any | None = None) -> list[dict[str, Any]]:
    """Return review items (default: pending), oldest first."""
    items = [
        r for r in _scan_type(_table(table), "review")
        if status is None or r.get("status") == status
    ]
    items.sort(key=lambda r: r.get("created_at") or "")
    return items


def _apply_review(
    item: dict[str, Any], *, table: Any, payload: dict[str, Any] | None = None
) -> None:
    """Commit an approved proposal. Group-merge links a member under the group
    leader via ``canonical_id``; fuzzy/nickname promote the proposed alias;
    industry assigns the curator-chosen module(s) (from ``payload['industries']``,
    falling back to the ``proposed`` hint)."""
    kind = item.get("kind")
    if kind == "group_merge" and item.get("entity_id") and item.get("target_id"):
        ent = get_entity(item["entity_id"], table=table)
        if ent:
            ent["canonical_id"] = item["target_id"]
            ent["needs_review"] = False
            table.put_item(Item=ent)
    elif kind in ("fuzzy_alias", "nickname") and item.get("entity_id") and item.get("proposed"):
        accumulate_aliases(item["entity_id"], [item["proposed"]], table=table)
    elif kind == "news_safe" and item.get("entity_id"):
        # promote a vetted new entity so its bare brand resolves from news/DOU
        set_news_safe(item["entity_id"], True, table=table)
    elif kind == "industry" and item.get("entity_id"):
        # curator picks the industry module(s) for an entrant we couldn't classify
        chosen = list((payload or {}).get("industries") or [])
        if not chosen and item.get("proposed"):
            chosen = [item["proposed"]]
        if chosen:
            set_industries(item["entity_id"], chosen, table=table)


def resolve_review(
    review_id: str,
    decision: str,
    table: Any | None = None,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Approve (apply the proposal) or reject a pending review. No-op if the
    review is missing or already decided. Returns the updated item.

    ``payload`` carries decision-time input for reviews whose approval needs a
    choice (industry: ``{"industries": [...]}``)."""
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be 'approved' or 'rejected'")
    t = _table(table)
    item = t.get_item(Key={"pk": f"REVIEW#{review_id}"}).get("Item")
    if not item or item.get("status") != "pending":
        return None
    if decision == "approved":
        _apply_review(item, table=t, payload=payload)
    item["status"] = decision
    item["decided_at"] = _now_iso()
    t.put_item(Item=item)
    return item


def propose_news_safe(entity_id: str, brand: str, *, table: Any | None = None) -> str | None:
    """Queue a review to let a new entity's bare brand resolve from news/DOU
    (ADR 002). Idempotent by entity_id; a curator approves it once the brand is
    verified distinctive enough for free text. Returns the review_id if queued."""
    return propose_review(
        "news_safe",
        key=entity_id,
        entity_id=entity_id,
        proposed=brand,
        reason="novo entrante — liberar a marca em notícias/DOU?",
        confidence="fuzzy",
        table=table,
    )


def propose_industry(
    entity_id: str, brand: str, *, hint: str = "", table: Any | None = None
) -> str | None:
    """Queue a review to assign an industry module to an entrant whose license
    did not map cleanly (ADR 002 Phase B). Idempotent by entity_id; the curator
    picks the module(s) at approval time. Returns the review_id if queued."""
    return propose_review(
        "industry",
        key=entity_id,
        entity_id=entity_id,
        proposed=None,
        reason="entrante sem licença classificável — atribuir módulo de indústria",
        hint=hint,
        confidence="fuzzy",
        table=table,
    )


def propose_group_merges(table: Any | None = None) -> int:
    """ADR step 5 producer: entities sharing a QSA controller are a hint they
    belong to one brand/group. Propose (never auto-commit — StoneX ≠ StoneCo)
    linking each member under a leader via ``canonical_id``. A curated member is
    preferred as leader so auto-created CNPJs group under the trusted brand.
    Returns the count of *newly* queued proposals."""
    t = _table(table)
    by_controller: dict[str, list[dict[str, Any]]] = {}
    for e in _scan_type(t, "entity"):
        for c in e.get("controllers") or []:
            k = normalize_alias(c)
            if k:
                by_controller.setdefault(k, []).append(e)

    queued = 0
    for controller, members in by_controller.items():
        uniq = {m["entity_id"]: m for m in members}
        if len(uniq) < 2:
            continue
        curated = [m for m in uniq.values() if m.get("confidence") == "curated"]
        leader = (curated[0] if curated else min(uniq.values(), key=lambda m: m["entity_id"]))
        for eid, m in uniq.items():
            if eid == leader["entity_id"]:
                continue
            if m.get("canonical_id") and m["canonical_id"] != eid:
                continue  # already grouped under something
            rid = propose_review(
                "group_merge",
                key=f"{eid}->{leader['entity_id']}",
                entity_id=eid,
                target_id=leader["entity_id"],
                reason=f"shared controller: {controller}",
                hint=controller,
                confidence="fuzzy",
                table=t,
            )
            if rid:
                queued += 1
    return queued


# --- Industry taxonomy (ADR 002 Phase B) --------------------------------------
# Canonical modules under the "financial-services" umbrella. Slug -> display +
# provisional pricing tier (NOT final — set from measured volume/concentration).
# Stored as IND#<slug> items so the taxonomy is editable without a redeploy.
INDUSTRIES: dict[str, dict[str, str]] = {
    "banking": {"display_name": "Banking", "tier": "premium"},
    "investment-banking": {"display_name": "Investment Banking", "tier": "premium"},
    "insurance": {"display_name": "Insurance", "tier": "mid"},
    "asset-management": {"display_name": "Asset Management", "tier": "mid"},
    "wealth-management": {"display_name": "Wealth Management", "tier": "mid"},
    "private-markets": {"display_name": "Private Markets (VC/PE)", "tier": "premium"},
    "fintech": {"display_name": "Fintech", "tier": "entry"},
    "financial-data-analytics": {"display_name": "Financial Data & Analytics", "tier": "mid"},
    "advisory": {"display_name": "Advisory", "tier": "entry"},
    "crypto": {"display_name": "Crypto & Digital Assets", "tier": "mid"},
    "consorcio": {"display_name": "Consórcios", "tier": "entry"},
    "betting": {"display_name": "Betting & iGaming", "tier": "mid"},
    "real-estate-funds": {"display_name": "Fundos Imobiliários (FIIs)", "tier": "mid"},
    "agri-funds": {"display_name": "Fundos do Agro (FIAGRO)", "tier": "mid"},
    "acquiring": {"display_name": "Adquirência (Maquininhas)", "tier": "mid"},
}
_PARENT_SECTOR = "financial-services"

# License class -> industry, SAFE subset only (unambiguous). Others (Corretora/
# DTVM, Leasing, Cooperativa, …) are left for review — they don't map 1:1.
LICENSE_INDUSTRY: dict[str, str] = {
    "Instituição de Pagamento": "fintech",
    "Crédito Direto (SCD)": "fintech",
    "Empréstimo P2P (SEP)": "fintech",
    "Financeira (SCFI)": "fintech",
    "Microcrédito (SCMEPP)": "fintech",
    "Banco": "banking",
}


def classify_industries(entrant: dict[str, Any]) -> tuple[list[str], bool]:
    """Auto-tag the SAFE case only. Returns (industries, needs_review):
    a clear license → its industry; anything ambiguous → ([], needs_review=True)
    so a curator assigns it (reuses the step-5 review queue)."""
    lic = str(entrant.get("license_class") or "").strip()
    if lic in LICENSE_INDUSTRY:
        return [LICENSE_INDUSTRY[lic]], False
    if entrant.get("is_fintech"):
        return ["fintech"], False
    return [], True  # unknown/ambiguous — propose for review


def set_industries(
    entity_id: str, industries: Iterable[str], table: Any | None = None,
    *, source: str = "curated",
) -> bool:
    """Assign an entity's industry module(s) and clear its needs_review flag —
    the review-queue action for an entrant we couldn't auto-classify. Returns
    True if the entity existed and was updated."""
    t = _table(table)
    ent = get_entity(entity_id, table=t)
    if not ent:
        return False
    inds = sorted({str(i).strip().lower() for i in industries if str(i).strip()})
    # ADR 018 Phase 2: an automated write must not demote a curated/fixture industry set.
    if not _may_write(ent, "industries", source):
        _log(entity_id, "blocked", source,
             {"field": "industries", "attempted": inds,
              "held_by": (ent.get("_prov") or {}).get("industries", {}).get("source")})
        return False
    if inds:
        ent["industries"] = inds
    else:
        ent.pop("industries", None)
    ent["needs_review"] = False
    _stamp(ent, ["industries"], source)  # ADR 018
    t.put_item(Item=ent)
    _log(entity_id, "set_industries", source, {"new": inds})
    return True


def set_parent(entity_id: str, parent: str | None, table: Any | None = None,
               *, source: str = "curated") -> bool:
    """Link a sub-entity to its tier-1 conglomerate parent (ADR 017), or clear it with
    ``parent=None``. Returns True if the entity existed and was updated."""
    t = _table(table)
    ent = get_entity(entity_id, table=t)
    if not ent:
        return False
    if not _may_write(ent, "parent", source):  # ADR 018 Phase 2
        _log(entity_id, "blocked", source, {"field": "parent", "attempted": parent})
        return False
    if parent:
        ent["parent"] = str(parent)
    else:
        ent.pop("parent", None)
    _stamp(ent, ["parent"], source)  # ADR 018
    t.put_item(Item=ent)
    _log(entity_id, "set_parent", source, {"parent": parent})
    return True


def children_of(parent_id: str, table: Any | None = None) -> list[str]:
    """Entity ids whose `parent` is `parent_id` (ADR 017 corporate group). Drives the
    tier-1 opt-in toggle — folding a conglomerate's lower-industry lines back in."""
    pid = str(parent_id)
    return sorted(
        e["entity_id"]
        for e in _scan_type(_table(table), "entity")
        if e.get("entity_id") and e.get("parent") == pid
    )


def entity_industry_map(table: Any | None = None) -> dict[str, list[str]]:
    """{entity_id: [industry slugs]} for every tracked entity. Used downstream
    (feed builder) to attribute narrative volume to industry modules without a
    second registry scan per narrative."""
    out: dict[str, list[str]] = {}
    for e in _scan_type(_table(table), "entity"):
        eid = e.get("entity_id")
        if eid:
            out[eid] = list(e.get("industries") or [])
    return out


# News search terms are DERIVED from the registry (the single source of truth for
# the tracked-entity set) so the news watchlist can never drift out of sync with
# it again (the C6/PicPay false-silence root cause). The query phrase defaults to a
# cleaned display_name; a few brands whose bare form is ambiguous or an awkward
# query get a curated override (a precise phrase that Google News + the headline
# phrase-match resolve cleanly). New entities auto-inherit the default.
NEWS_TERM_OVERRIDES: dict[str, str] = {
    "pagseguro": "PagBank",
    "inter": "Banco Inter",
    # The press names the listed entity "XP Inc." — "XP Investimentos" as an exact
    # phrase returns nothing (missed its Q2 earnings); bare "XP" is ambiguous.
    "xp": "XP Inc",
    "itau": "Itaú Unibanco",
    "santander": "Santander Brasil",
    # The full legal name "Caixa Econômica Federal" is too precise to match
    # headlines (0 results); bare "Caixa" is the common word (cashbox) and is
    # dropped in free-text. "Caixa Econômica" is the phrase the press actually uses.
    "caixa": "Caixa Econômica",
}


def _clean_news_term(display_name: str | None) -> str:
    """First brand segment of a display_name as a clean query phrase.

    "Nubank / Nu Holdings" -> "Nubank"; "InfinitePay (CloudWalk)" -> "InfinitePay".
    """
    term = str(display_name or "").split("/")[0]
    term = re.sub(r"\(.*?\)", "", term)  # drop parentheticals
    return re.sub(r"\s+", " ", term).strip()


def news_query_term(
    entity_id: str | None, display_name: str | None, *, stored: str | None = None
) -> str:
    """Best Google-News query phrase for an entity.

    Precedence: the registry's own ``news_term`` (``stored`` — API-editable data)
    → the code override map (seed/fallback) → a cleaned display_name → the id.
    """
    return (
        stored
        or NEWS_TERM_OVERRIDES.get(str(entity_id or ""))
        or _clean_news_term(display_name)
        or str(entity_id or "")
    )


def news_terms(table: Any | None = None, *, trusted_only: bool = True) -> list[str]:
    """Derive news search phrases for tracked entities from the registry.

    Only *trusted* entities (curated or human-vetted ``news_safe``) are included by
    default — the same gate that lets a bare brand resolve in free-text — so an
    unvetted auto-created entrant is not news-searched until promoted, and a newly
    curated/promoted entity joins automatically. Sorted, de-duplicated (case-fold).
    """
    out: list[str] = []
    seen: set[str] = set()
    for e in _scan_type(_table(table), "entity"):
        if not e.get("active", True):
            continue
        # Structured-only entities (news_search=False) are excluded from the news
        # query set — their identity comes from a structured source (fatos_term).
        if e.get("news_search", True) is False:
            continue
        if trusted_only and not (
            e.get("confidence") == "curated" or e.get("news_safe")
        ):
            continue
        term = news_query_term(
            e.get("entity_id"), e.get("display_name"), stored=e.get("news_term")
        )
        key = term.lower()
        if term and key not in seen:
            seen.add(key)
            out.append(term)
    return sorted(out)


def fatos_terms(table: Any | None = None, *, trusted_only: bool = True) -> list[str]:
    """Derive CVM material-fact (Fato Relevante) issuer-name phrases from the
    registry — the STRUCTURED lens for B3-listed entities.

    Only entities with an explicit ``fatos_term`` (set for listed issuers) are
    included, so an entity resolves from its filing rather than a fragile news
    headline (and a place-name brand like "Porto Seguro" stops depending on news).
    Mirrors :func:`news_terms`' trust gate. Sorted, de-duplicated (case-fold)."""
    out: list[str] = []
    seen: set[str] = set()
    for e in _scan_type(_table(table), "entity"):
        if not e.get("active", True):
            continue
        term = str(e.get("fatos_term") or "").strip()
        if not term:
            continue
        if trusted_only and not (
            e.get("confidence") == "curated" or e.get("news_safe")
        ):
            continue
        key = term.lower()
        if key not in seen:
            seen.add(key)
            out.append(term)
    return sorted(out)


def seed_industries(table: Any | None = None) -> int:
    """Write the canonical IND# taxonomy items. Idempotent."""
    t = _table(table)
    for slug, meta in INDUSTRIES.items():
        t.put_item(
            Item={
                "pk": f"IND#{slug}",
                "type": "industry",
                "slug": slug,
                "display_name": meta["display_name"],
                "parent": _PARENT_SECTOR,
                "tier": meta["tier"],
            }
        )
    return len(INDUSTRIES)


def industry_rollup(table: Any | None = None) -> dict[str, dict[str, Any]]:
    """Per-industry CONCENTRATION measurement: distinct tracked entities per
    industry (few players = concentrated = premium). One input to data-driven
    pricing; the other (signal/narrative volume) is measured downstream from
    narratives. Untagged entities count under '_unclassified'."""
    rollup: dict[str, dict[str, Any]] = {
        slug: {"display_name": meta["display_name"], "tier": meta["tier"], "entities": 0}
        for slug, meta in INDUSTRIES.items()
    }
    rollup["_unclassified"] = {"display_name": "(unclassified)", "tier": None, "entities": 0}
    for e in _scan_type(_table(table), "entity"):
        inds = e.get("industries") or []
        if not inds:
            rollup["_unclassified"]["entities"] += 1
        for slug in inds:
            rollup.setdefault(
                slug, {"display_name": slug, "tier": None, "entities": 0}
            )["entities"] += 1
    return rollup


def _seed_ambiguous_tokens(aliases: Iterable[str]) -> list[str]:
    """This entity's bare tokens that are common words (its ⊆ of AMBIGUOUS_TOKENS).

    Migration source for the per-entity ``ambiguous_tokens`` field — reads the
    hardcoded AMBIGUOUS_TOKENS set once, at seed time, so it becomes registry data.
    Only the actual common-word token is captured (STONE), not the entity's other
    distinctive single-token aliases (STONECO, STNE).
    """
    from src.synth.entities import AMBIGUOUS_TOKENS

    out: set[str] = set()
    for a in aliases:
        s = str(a).upper().strip()
        if not s:
            continue
        # A ticker symbol (TICKER:XP) is itself a bare token — its symbol can be a
        # common word (XP), so unwrap it before the common-word check.
        tok = s.split(":", 1)[1] if s.startswith("TICKER:") else s
        if tok and " " not in tok and tok in AMBIGUOUS_TOKENS:
            out.add(tok)
    return sorted(out)


def seed(table: Any | None = None) -> int:
    """Populate the registry from the curated ENTITY_ALIASES (confidence=curated)
    and the canonical IND# taxonomy, with curated entity→industry tags. Also seeds
    the per-entity SEARCH curation (news_term, ambiguous) from the code fixtures —
    after which the registry, not the code, is authoritative (API-editable)."""
    from src.synth.entities import ENTITY_ALIASES, ENTITY_INDUSTRIES
    from src.synth.synthesize import ENTITY_LABELS

    t = _table(table)
    seed_industries(table=t)
    count = 0
    for entity_id, aliases in ENTITY_ALIASES.items():
        names: list[str] = []
        ticker: str | None = None
        for alias in aliases:
            if str(alias).upper().startswith("TICKER:"):
                ticker = str(alias).split(":", 1)[1]
                names.append(ticker)
            else:
                names.append(str(alias))
        display_name = ENTITY_LABELS.get(entity_id, entity_id.replace("_", " ").title())
        put_entity(
            entity_id,
            display_name,
            names,
            alias_forms=list(aliases),  # exact curated forms for substring matching
            ticker=ticker,
            industries=ENTITY_INDUSTRIES.get(entity_id),
            confidence="curated",
            news_term=news_query_term(entity_id, display_name),
            ambiguous_tokens=_seed_ambiguous_tokens(aliases),
            source="fixture",  # ADR 018: the code seed is the strongest provenance
            table=t,
        )
        count += 1
    return count


def backfill_curation(table: Any | None = None, *, force: bool = False) -> int:
    """Non-destructive migration: set news_term + ambiguous on EXISTING entities
    without a full reseed, so runtime-set fields (news_safe, accumulated aliases,
    industry assignments) are preserved. Idempotent. Returns entities updated.

    ``force`` re-derives both fields from code (ignoring stored values) — a
    "resync from code" after the seed logic changes; without it, an already-set
    field is left as-is (so a human/API edit is never clobbered)."""
    t = _table(table)
    updated = 0
    for e in _scan_type(t, "entity"):
        eid = e.get("entity_id")
        if not eid:
            continue
        alias_forms = e.get("alias_forms") or e.get("aliases") or []
        stored_term = None if force else e.get("news_term")
        news_term = news_query_term(eid, e.get("display_name"), stored=stored_term)
        toks = e.get("ambiguous_tokens")
        if toks is None or force:
            toks = _seed_ambiguous_tokens(alias_forms)
        changed = e.get("news_term") != news_term or e.get("ambiguous_tokens") != toks
        if changed:
            e["news_term"] = news_term
            e["ambiguous_tokens"] = list(toks)
            e["ambiguous"] = bool(toks)
            t.put_item(Item=e)
            updated += 1
    return updated


# Cached maps for resolve_entities, built in ONE scan and reused per Lambda
# execution env: {entity_id: [raw alias forms]} and {entity_id: trusted_for_free_text}.
# "Trusted" = a curated entity OR one a human promoted (news_safe) — governs whether
# a bare single-token alias may resolve from free-text (news/DOU); see ADR 002.
_ALIAS_MAP_CACHE: dict[str, list[str]] | None = None
_TRUST_MAP_CACHE: dict[str, bool] | None = None
_AMBIG_TOKENS_CACHE: set[str] | None = None


def _load_maps(
    table: Any | None = None, force: bool = False
) -> tuple[dict[str, list[str]], dict[str, bool], set[str]]:
    global _ALIAS_MAP_CACHE, _TRUST_MAP_CACHE, _AMBIG_TOKENS_CACHE
    if (
        _ALIAS_MAP_CACHE is not None
        and _TRUST_MAP_CACHE is not None
        and _AMBIG_TOKENS_CACHE is not None
        and not force
    ):
        return _ALIAS_MAP_CACHE, _TRUST_MAP_CACHE, _AMBIG_TOKENS_CACHE
    t = _table(table)
    aliases: dict[str, list[str]] = {}
    trust: dict[str, bool] = {}
    ambig: set[str] = set()
    kwargs: dict[str, Any] = {}
    while True:
        resp = t.scan(**kwargs)
        for it in resp.get("Items", []):
            if it.get("type") == "entity" and it.get("entity_id"):
                eid = it["entity_id"]
                forms = list(it.get("alias_forms") or it.get("aliases") or [])
                aliases[eid] = forms
                trust[eid] = it.get("confidence") == "curated" or bool(it.get("news_safe"))
                # The entity's own common-word tokens feed the free-text
                # ambiguity set (precise: STONE, not the distinctive STONECO).
                for tok in it.get("ambiguous_tokens") or []:
                    t2 = str(tok).upper().strip()
                    if t2:
                        ambig.add(t2)
        start = resp.get("LastEvaluatedKey")
        if not start:
            break
        kwargs["ExclusiveStartKey"] = start
    _ALIAS_MAP_CACHE, _TRUST_MAP_CACHE, _AMBIG_TOKENS_CACHE = aliases, trust, ambig
    return aliases, trust, ambig


def load_alias_map(table: Any | None = None, force: bool = False) -> dict[str, list[str]]:
    """Return {entity_id: raw alias forms} from the registry (cached)."""
    return _load_maps(table, force)[0]


def load_trust_map(table: Any | None = None, force: bool = False) -> dict[str, bool]:
    """Return {entity_id: trusted-for-free-text}. Trusted iff curated or news_safe."""
    return _load_maps(table, force)[1]


def load_ambiguous_tokens(table: Any | None = None, force: bool = False) -> set[str]:
    """Return the set of bare tokens that are common words (registry-flagged
    ``ambiguous`` entities) — the free-text resolution guardrail as registry data
    rather than the hardcoded AMBIGUOUS_TOKENS set."""
    return _load_maps(table, force)[2]


def set_news_safe(entity_id: str, value: bool = True, table: Any | None = None) -> bool:
    """Promote (or demote) an entity so its bare single-token brand may resolve
    from free-text news/DOU — the review-queue action for a vetted new entity.
    Returns True if the entity existed and was updated."""
    t = _table(table)
    ent = get_entity(entity_id, table=t)
    if not ent:
        return False
    ent["news_safe"] = bool(value)
    t.put_item(Item=ent)
    return True


def clear_cache() -> None:
    global _ALIAS_MAP_CACHE, _TRUST_MAP_CACHE, _AMBIG_TOKENS_CACHE, _ROLE_MAP_CACHE
    _ALIAS_MAP_CACHE = None
    _TRUST_MAP_CACHE = None
    _AMBIG_TOKENS_CACHE = None
    _ROLE_MAP_CACHE = None


# --- Curation CRUD (operator API surface) --------------------------------------
# Primitives for the registry-as-API-product: read/list/create/patch/deactivate
# the ENT# curation records. Aliases are NOT patched here (they need ALIAS#
# reindexing) — use accumulate_aliases via the dedicated aliases endpoint.

# Fields an operator may PATCH, with how each is normalized. Protected keys
# (pk, type, entity_id, canonical_id, aliases, alias_forms, cnpj_roots) are never
# writable through the patch path.
_PATCH_STR = frozenset({"display_name", "news_term", "fatos_term", "confidence", "sector", "license_class", "ticker", "ownership", "attribution_role"})
_PATCH_BOOL = frozenset({"news_safe", "active", "news_search"})


def list_entities(
    table: Any | None = None, *, include_inactive: bool = False
) -> list[dict[str, Any]]:
    """All entity curation records, sorted by id (inactive excluded by default)."""
    out = [
        e
        for e in _scan_type(_table(table), "entity")
        if include_inactive or e.get("active", True)
    ]
    out.sort(key=lambda e: str(e.get("entity_id") or ""))
    return out


def update_entity(
    entity_id: str, patch: dict[str, Any], table: Any | None = None
) -> dict[str, Any] | None:
    """Apply a whitelisted partial update to an entity; return it, or None if
    absent. Preserves every field not named in ``patch``. Unknown/protected keys
    are ignored. ``ambiguous_tokens`` also refreshes the ``ambiguous`` flag."""
    t = _table(table)
    ent = get_entity(entity_id, table=t)
    if not ent:
        return None
    for key, val in (patch or {}).items():
        if key in _PATCH_STR:
            if val in (None, ""):
                ent.pop(key, None)
            else:
                ent[key] = str(val)
        elif key in _PATCH_BOOL:
            ent[key] = bool(val)
        elif key == "ambiguous_tokens":
            toks = sorted(
                {
                    str(x).upper().strip()
                    for x in (val or [])
                    if str(x).strip() and " " not in str(x).strip()
                }
            )
            ent["ambiguous_tokens"] = toks
            ent["ambiguous"] = bool(toks)
        elif key == "industries":
            ent["industries"] = sorted(
                {str(i).strip().lower() for i in (val or []) if str(i).strip()}
            )
            ent["needs_review"] = False
        elif key == "controllers":
            ent["controllers"] = [str(c) for c in (val or [])]
        elif key == "certifications":
            ent["certifications"] = sorted(
                {str(c).strip() for c in (val or []) if str(c).strip()}
            )
        elif key == "esg":
            ent["esg"] = _ddb_safe(dict(val)) if val else {}
        # else: unknown/protected key — ignored
    t.put_item(Item=ent)
    return ent


def deactivate_entity(entity_id: str, table: Any | None = None) -> dict[str, Any] | None:
    """Soft-delete: set active=False (curation is never hard-deleted)."""
    return update_entity(entity_id, {"active": False}, table=table)


if __name__ == "__main__":
    print(f"seeded {seed()} curated entities into {os.environ.get('ONCA_ENTITIES_TABLE', 'onca-entities')}")
