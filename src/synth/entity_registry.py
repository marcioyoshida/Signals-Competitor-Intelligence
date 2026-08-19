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
from typing import Any, Iterable


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
    t.put_item(Item=entity)
    for na in norm:
        t.put_item(Item={"pk": f"ALIAS#{na}", "type": "alias", "entity_id": entity_id})
    for r in roots:
        t.put_item(Item={"pk": f"CNPJ#{r}", "type": "cnpj", "entity_id": entity_id})
    return entity


def get_entity(entity_id: str, table: Any | None = None) -> dict[str, Any] | None:
    return _table(table).get_item(Key={"pk": f"ENT#{entity_id}"}).get("Item")


def resolve_by_alias(name: str, table: Any | None = None) -> str | None:
    item = _table(table).get_item(Key={"pk": f"ALIAS#{normalize_alias(name)}"}).get("Item")
    return item.get("entity_id") if item else None


def resolve_by_cnpj(cnpj_root: str, table: Any | None = None) -> str | None:
    root = "".join(ch for ch in str(cnpj_root or "") if ch.isdigit())[:8]
    if not root:
        return None
    item = _table(table).get_item(Key={"pk": f"CNPJ#{root}"}).get("Item")
    return item.get("entity_id") if item else None


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
    t.put_item(Item=ent)
    for na in new_norm:
        t.put_item(Item={"pk": f"ALIAS#{na}", "type": "alias", "entity_id": entity_id})
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
    table: Any | None = None,
) -> str | None:
    """Queue a human-review proposal. Idempotent by (kind, key): an already
    queued OR already decided proposal is left untouched (never reopened, never
    duplicated). Returns the review_id if newly queued, else ``None``."""
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
    entity_id: str, industries: Iterable[str], table: Any | None = None
) -> bool:
    """Assign an entity's industry module(s) and clear its needs_review flag —
    the review-queue action for an entrant we couldn't auto-classify. Returns
    True if the entity existed and was updated."""
    t = _table(table)
    ent = get_entity(entity_id, table=t)
    if not ent:
        return False
    inds = sorted({str(i).strip().lower() for i in industries if str(i).strip()})
    if inds:
        ent["industries"] = inds
    else:
        ent.pop("industries", None)
    ent["needs_review"] = False
    t.put_item(Item=ent)
    return True


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
    global _ALIAS_MAP_CACHE, _TRUST_MAP_CACHE, _AMBIG_TOKENS_CACHE
    _ALIAS_MAP_CACHE = None
    _TRUST_MAP_CACHE = None
    _AMBIG_TOKENS_CACHE = None


# --- Curation CRUD (operator API surface) --------------------------------------
# Primitives for the registry-as-API-product: read/list/create/patch/deactivate
# the ENT# curation records. Aliases are NOT patched here (they need ALIAS#
# reindexing) — use accumulate_aliases via the dedicated aliases endpoint.

# Fields an operator may PATCH, with how each is normalized. Protected keys
# (pk, type, entity_id, canonical_id, aliases, alias_forms, cnpj_roots) are never
# writable through the patch path.
_PATCH_STR = frozenset({"display_name", "news_term", "confidence", "sector", "license_class", "ticker"})
_PATCH_BOOL = frozenset({"news_safe", "active"})


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
        # else: unknown/protected key — ignored
    t.put_item(Item=ent)
    return ent


def deactivate_entity(entity_id: str, table: Any | None = None) -> dict[str, Any] | None:
    """Soft-delete: set active=False (curation is never hard-deleted)."""
    return update_entity(entity_id, {"active": False}, table=table)


if __name__ == "__main__":
    print(f"seeded {seed()} curated entities into {os.environ.get('ONCA_ENTITIES_TABLE', 'onca-entities')}")
