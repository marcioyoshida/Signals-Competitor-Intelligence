"""Entities registry (DynamoDB) — ADR docs/2026-08-17-adr-entities-registry.md.

Single-table lookup design (O(1) exact resolution, no GSI):
  pk = "ENT#<entity_id>"    -> entity record (display_name, aliases, sector, ...)
  pk = "ALIAS#<norm>"       -> {entity_id}   (accent-folded name index)
  pk = "CNPJ#<root8>"       -> {entity_id}   (exact join key)

Step 1 (this file): the table + a curated seed from ENTITY_ALIASES, plus the
reusable `put_entity` write primitive and read helpers. Wiring resolution into
`resolve_entities` (step 2) and CNPJ auto-create from the entrant pipeline
(step 3) build on these.

Module-level imports stay free of src.synth.* so ingest can reuse the write
primitives later without pulling synth; the curated seed imports lazily.
"""
from __future__ import annotations

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
