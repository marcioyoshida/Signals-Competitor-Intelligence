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
    license_class: str | None = None,
    cnpj_roots: Iterable[str] = (),
    ticker: str | None = None,
    controllers: list[str] | None = None,
    confidence: str = "cnpj",
    canonical_id: str | None = None,
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
    if sector:
        entity["sector"] = sector
    if license_class:
        entity["license_class"] = license_class
    if ticker:
        entity["ticker"] = ticker
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

    put_entity(
        entity_id,
        display,
        forms,
        alias_forms=forms,
        cnpj_roots=[root],
        controllers=entrant.get("controllers") or None,
        license_class=entrant.get("license_class"),
        sector="fintech" if entrant.get("is_fintech") else None,
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


def _apply_review(item: dict[str, Any], *, table: Any) -> None:
    """Commit an approved proposal. Group-merge links a member under the group
    leader via ``canonical_id``; fuzzy/nickname promote the proposed alias."""
    kind = item.get("kind")
    if kind == "group_merge" and item.get("entity_id") and item.get("target_id"):
        ent = get_entity(item["entity_id"], table=table)
        if ent:
            ent["canonical_id"] = item["target_id"]
            ent["needs_review"] = False
            table.put_item(Item=ent)
    elif kind in ("fuzzy_alias", "nickname") and item.get("entity_id") and item.get("proposed"):
        accumulate_aliases(item["entity_id"], [item["proposed"]], table=table)


def resolve_review(review_id: str, decision: str, table: Any | None = None) -> dict[str, Any] | None:
    """Approve (apply the proposal) or reject a pending review. No-op if the
    review is missing or already decided. Returns the updated item."""
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be 'approved' or 'rejected'")
    t = _table(table)
    item = t.get_item(Key={"pk": f"REVIEW#{review_id}"}).get("Item")
    if not item or item.get("status") != "pending":
        return None
    if decision == "approved":
        _apply_review(item, table=t)
    item["status"] = decision
    item["decided_at"] = _now_iso()
    t.put_item(Item=item)
    return item


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


def seed(table: Any | None = None) -> int:
    """Populate the registry from the curated ENTITY_ALIASES (confidence=curated)."""
    from src.synth.entities import ENTITY_ALIASES
    from src.synth.synthesize import ENTITY_LABELS

    t = _table(table)
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
        put_entity(
            entity_id,
            ENTITY_LABELS.get(entity_id, entity_id.replace("_", " ").title()),
            names,
            alias_forms=list(aliases),  # exact curated forms for substring matching
            ticker=ticker,
            confidence="curated",
            table=t,
        )
        count += 1
    return count


# Cached {entity_id: [raw alias forms]} map for resolve_entities. Loaded once per
# Lambda execution env (pipeline runs daily; a warm-reuse day-old cache is fine).
_ALIAS_MAP_CACHE: dict[str, list[str]] | None = None


def load_alias_map(table: Any | None = None, force: bool = False) -> dict[str, list[str]]:
    """Return {entity_id: raw alias forms} from the registry (cached)."""
    global _ALIAS_MAP_CACHE
    if _ALIAS_MAP_CACHE is not None and not force:
        return _ALIAS_MAP_CACHE
    t = _table(table)
    out: dict[str, list[str]] = {}
    kwargs: dict[str, Any] = {}
    while True:
        resp = t.scan(**kwargs)
        for it in resp.get("Items", []):
            if it.get("type") == "entity" and it.get("entity_id"):
                out[it["entity_id"]] = list(it.get("alias_forms") or it.get("aliases") or [])
        start = resp.get("LastEvaluatedKey")
        if not start:
            break
        kwargs["ExclusiveStartKey"] = start
    _ALIAS_MAP_CACHE = out
    return out


def clear_cache() -> None:
    global _ALIAS_MAP_CACHE
    _ALIAS_MAP_CACHE = None


if __name__ == "__main__":
    print(f"seeded {seed()} curated entities into {os.environ.get('ONCA_ENTITIES_TABLE', 'onca-entities')}")
