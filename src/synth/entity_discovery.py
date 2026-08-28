"""Entity discovery & enrichment pipeline (ADR 011 / issue #14).

Closes the "unknown unknowns" gap: companies/funds that recur in news or in
regulator registries but are not yet in the entities registry stay invisible
to synthesis. This module is the producer that *finds*, *enriches*, and
*proposes* — precision-first, never silent pollution of the registry.

Verticals (first cut):
  1. **Structured official registry sync** — CVM FIAGRO Informe Mensal
     (and later FII, BCB lists, CVM cias abertas). CNPJ-keyed → strong
     identity → eligible for auto-add under the industry module.
  2. **Keyword / industry harvest from news** — scan recent free-text for a
     keyword (e.g. "FIAGRO") and associated fund names / B3 tickers that do
     not resolve; queue as discovery candidates with evidence.

Promotion policy (mirrors ADR 011 §4):
  - Strong structured identity (CNPJ from CVM/BCB filing, or B3 ticker matched
    to CVM registry) → ``auto_create`` at confidence=cnpj (or enrich existing).
  - News-only brand → ``propose_review(kind="discovery")`` for analyst vetting.
  - Never hijack a name already owned by another entity.

Usage:
  from src.synth.entity_discovery import discover_fiagro, harvest_keyword
  report = discover_fiagro()          # structured CVM path
  cands  = harvest_keyword("FIAGRO")  # news path (needs recent news items)

Wired as a best-effort pass in the ingest Lambda (gated by
``ONCA_ENTITY_DISCOVERY``, default off until validated) or a weekly schedule.
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable

from src.synth import entity_registry
from src.synth.entities import detect_b3_tickers, resolve_entities

# Industry keyword → registry industry slug (must exist in INDUSTRIES).
INDUSTRY_KEYWORDS: dict[str, str] = {
    "FIAGRO": "agri-funds",
    "FIAGROS": "agri-funds",
    "FII": "real-estate-funds",
    "FIIS": "real-estate-funds",
    "FUNDO IMOBILIÁRIO": "real-estate-funds",
    "FUNDOS IMOBILIÁRIOS": "real-estate-funds",
}

# Stop words / generic tokens that should never become entity brands alone.
_STOP = frozenset(
    {
        "FUNDO",
        "FUNDOS",
        "INVESTIMENTO",
        "INVESTIMENTOS",
        "CLASSE",
        "COTA",
        "COTAS",
        "FIAGRO",
        "FII",
        "FIDC",
        "CRA",
        "CRI",
        "DE",
        "DA",
        "DO",
        "DAS",
        "DOS",
        "E",
        "EM",
        "NAS",
        "NOS",
        "CADEIAS",
        "PRODUTIVAS",
        "AGROINDUSTRIAIS",
        "AGRO",
        "AGROPECUARIA",
        "IMOBILIARIO",
        "IMOBILIÁRIO",
        "RESPONSABILIDADE",
        "LIMITADA",
        "RESP",
        "LTDA",
        "S.A",
        "SA",
        "BANCO",
        "ASSET",
        "GESTAO",
        "GESTÃO",
        "ADMINISTRADOR",
        "GESTOR",
    }
)


def _slug(value: str) -> str:
    return entity_registry._slug(value)


def _root8(cnpj: str | None) -> str:
    d = "".join(ch for ch in str(cnpj or "") if ch.isdigit())
    return d[:8] if len(d) >= 8 else ""


# ---------------------------------------------------------------------------
# 1. Structured CVM FIAGRO → registry (strong identity path)
# ---------------------------------------------------------------------------


def _profile_from_fiagro(row: dict[str, Any]) -> dict[str, Any]:
    """Compose a registry-ready profile from a CVM FIAGRO informe row."""
    name = str(row.get("fund_name") or "").strip()
    ticker = (row.get("ticker") or "").strip().upper() or None
    cnpj = row.get("cnpj") or ""
    root = _root8(cnpj)
    forms: list[str] = []
    if name and len(name) >= 4:
        forms.append(name)
    if ticker:
        forms.append(ticker)
        forms.append(f"TICKER:{ticker}")
    # Short distinctive brand: first 2–3 meaningful tokens of the name.
    brand = _brand_from_name(name)
    if brand and brand.upper() not in {f.upper() for f in forms}:
        forms.append(brand)
    display = brand or name or (ticker or f"FIAGRO {root}")
    entity_id = _slug(ticker or brand or name) or f"fiagro_{root}"
    return {
        "entity_id": entity_id,
        "display_name": display,
        "aliases": forms,
        "alias_forms": forms,
        "cnpj_roots": [root] if root else [],
        "ticker": ticker,
        "industries": ["agri-funds"],
        "sector": "asset-management",
        "license_class": "FIAGRO",
        "confidence": "cnpj",
        "news_term": ticker or brand or display,
        "fatos_term": None,  # funds rarely file Fato Relevante as issuers
        "news_search": bool(ticker),  # only search news if we have a distinctive ticker
        "source_row": {
            "cnpj": cnpj,
            "isin": row.get("isin"),
            "admin": row.get("admin"),
            "manager": row.get("manager"),
            "pl": row.get("pl"),
            "as_of": row.get("as_of"),
            "url": row.get("url"),
        },
    }


def _brand_from_name(name: str) -> str | None:
    """Extract a short distinctive brand from a long legal FIAGRO name.

    e.g. \"KINEA CRÉDITO AGRO FIAGRO-IMOBILIÁRIO\" → \"Kinea Crédito Agro\"
         \"XP CRÉDITO AGRO - FI NAS CADEIAS...\" → \"XP Crédito Agro\"
    """
    if not name:
        return None
    # Drop parentheticals and after dashes that introduce the type.
    cleaned = re.split(r"\s+-\s+|/", name)[0]
    toks = re.split(r"[^A-Za-zÀ-ÿ0-9]+", cleaned)
    keep: list[str] = []
    for t in toks:
        if not t:
            continue
        if t.upper() in _STOP:
            if keep:  # stop once we hit a generic after some brand tokens
                break
            continue
        keep.append(t)
        if len(keep) >= 4:
            break
    if not keep:
        return None
    brand = " ".join(keep)
    return brand if len(brand) >= 3 else None


def discover_fiagro(
    *,
    min_pl: float = 50_000_000.0,  # R$ 50mi floor — drop micro vehicles
    max_new: int = 40,
    auto_create: bool = True,
    table: Any | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Sync the CVM FIAGRO universe into the entities registry.

    For each fund (PL ≥ ``min_pl``):
      - If CNPJ root already resolves → enrich (add ticker / industries / aliases).
      - Else if ``auto_create`` → write a new ENT# (confidence=cnpj, industry=agri-funds).
      - Else → queue a discovery review proposal.

    Returns a report: {fetched, already, created, enriched, proposed, skipped, errors}.
    """
    if rows is None:
        from src.ingest import cvm_fiagro

        rows = cvm_fiagro.fetch_fiagro(min_pl=min_pl)

    report: dict[str, Any] = {
        "fetched": len(rows),
        "already": 0,
        "created": [],
        "enriched": [],
        "proposed": [],
        "skipped": 0,
        "errors": [],
    }
    t = entity_registry._table(table)
    created_count = 0

    for row in rows:
        if created_count >= max_new and auto_create:
            report["skipped"] += 1
            continue
        try:
            profile = _profile_from_fiagro(row)
            root = (profile["cnpj_roots"] or [None])[0]
            existing_id = (
                entity_registry.resolve_by_cnpj(root, table=t) if root else None
            )
            # Also try ticker alias if present.
            if not existing_id and profile.get("ticker"):
                existing_id = entity_registry.resolve_by_alias(
                    profile["ticker"], table=t
                )

            if existing_id:
                report["already"] += 1
                # Enrich: industries, ticker, aliases (non-destructive).
                changed = _enrich_existing(existing_id, profile, table=t)
                if changed:
                    report["enriched"].append(existing_id)
                continue

            if not auto_create:
                rid = entity_registry.propose_review(
                    "discovery",
                    key=profile["entity_id"],
                    entity_id=None,
                    proposed=profile["display_name"],
                    reason=f"FIAGRO CVM CNPJ={root} PL={row.get('pl')}",
                    hint=profile.get("ticker") or root or "",
                    confidence="cnpj",
                    table=t,
                )
                if rid:
                    report["proposed"].append(rid)
                continue

            # Auto-create with strong structured identity.
            eid = profile["entity_id"]
            if entity_registry.get_entity(eid, table=t):
                # slug collision with a different entity — namespace by root
                eid = f"{eid}_{root}" if root else f"{eid}_fiagro"
            entity_registry.put_entity(
                eid,
                profile["display_name"],
                profile["aliases"],
                alias_forms=profile["alias_forms"],
                cnpj_roots=profile["cnpj_roots"],
                ticker=profile.get("ticker"),
                industries=profile["industries"],
                sector=profile.get("sector"),
                license_class=profile.get("license_class"),
                confidence="cnpj",
                news_term=profile.get("news_term"),
                news_search=profile.get("news_search", True),
                table=t,
            )
            report["created"].append(eid)
            created_count += 1
        except Exception as exc:  # pragma: no cover - best-effort per row
            report["errors"].append(f"{row.get('cnpj')}: {exc}")

    return report


def _enrich_existing(
    entity_id: str, profile: dict[str, Any], *, table: Any
) -> bool:
    """Attach missing ticker / industries / CNPJ / aliases to an existing entity."""
    ent = entity_registry.get_entity(entity_id, table=table)
    if not ent:
        return False
    changed = False
    # CNPJ roots
    new_roots = entity_registry.add_cnpj_roots(
        entity_id, profile.get("cnpj_roots") or [], table=table
    )
    if new_roots:
        changed = True
    # Aliases / ticker forms
    forms = list(profile.get("alias_forms") or profile.get("aliases") or [])
    added = entity_registry.accumulate_aliases(entity_id, forms, table=table)
    if added:
        changed = True
    # Industries (union)
    cur_inds = set(ent.get("industries") or [])
    want = set(profile.get("industries") or [])
    if want - cur_inds:
        entity_registry.set_industries(
            entity_id, sorted(cur_inds | want), table=table
        )
        changed = True
    # Ticker
    if profile.get("ticker") and not ent.get("ticker"):
        if entity_registry.assign_ticker(entity_id, profile["ticker"], table=table):
            changed = True
    return changed


# ---------------------------------------------------------------------------
# 2. Keyword harvest from free-text news (propose-only path)
# ---------------------------------------------------------------------------


def harvest_keyword(
    keyword: str,
    news_items: Iterable[dict[str, Any]],
    *,
    industry: str | None = None,
    min_docs: int = 2,
    table: Any | None = None,
) -> list[dict[str, Any]]:
    """Scan news items for a keyword and collect unresolved associated entities.

    For each item whose title/subject mentions ``keyword`` (case-insensitive):
      - Extract B3 tickers and multi-token company-like spans.
      - Drop anything that already resolves via ``resolve_entities``.
      - Aggregate by surface form; keep candidates that appear in ≥ ``min_docs``
        distinct documents (precision gate).

    Returns candidate dicts ready for ``propose_review`` or further enrichment:
      {surface, tickers, evidence_ids, industry, count, sample_titles}.
    """
    industry = industry or INDUSTRY_KEYWORDS.get(keyword.upper())
    key_re = re.compile(re.escape(keyword), re.I)
    # surface → {docs, tickers, titles}
    bucket: dict[str, dict[str, Any]] = {}

    for item in news_items:
        title = str(item.get("title") or item.get("subject") or "")
        blob = f"{title} {item.get('summary') or ''}"
        if not key_re.search(blob):
            continue
        # Already-resolved entities in this item are not candidates.
        resolved = set(resolve_entities(item))
        tickers = detect_b3_tickers(blob.upper())
        # Candidate surfaces: the tickers themselves + short brands near the keyword.
        surfaces: list[str] = list(tickers)
        for m in re.finditer(
            rf"([A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÀ-ÿ0-9&.]{{2,}}(?:\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-Za-zÀ-ÿ0-9&.]{{1,}}){{0,3}})\s+(?:{re.escape(keyword)})",
            blob,
            re.I,
        ):
            surfaces.append(m.group(1).strip())

        doc_id = str(item.get("id") or item.get("url") or title)[:120]
        for surface in surfaces:
            if not surface or len(surface) < 3:
                continue
            # Skip pure stop / the keyword itself.
            if surface.upper() in _STOP or surface.upper() == keyword.upper():
                continue
            # Skip if any alias of a resolved entity matches this surface.
            # (cheap check: if resolve_entities on a synthetic item hits something)
            probe = {"title": surface, "source": "NEWS", "kind": "competitor"}
            if resolve_entities(probe):
                continue
            # Also skip if the surface is already an ALIAS# for someone.
            if entity_registry.resolve_by_alias(surface, table=table):
                continue
            key = surface.upper()
            b = bucket.setdefault(
                key,
                {
                    "surface": surface,
                    "tickers": set(),
                    "docs": set(),
                    "titles": [],
                    "industry": industry,
                },
            )
            b["docs"].add(doc_id)
            for tk in tickers:
                b["tickers"].add(tk)
            if len(b["titles"]) < 3:
                b["titles"].append(title[:160])

    # Frequency gate
    out: list[dict[str, Any]] = []
    for b in bucket.values():
        if len(b["docs"]) < min_docs:
            continue
        out.append(
            {
                "surface": b["surface"],
                "tickers": sorted(b["tickers"]),
                "count": len(b["docs"]),
                "evidence_ids": sorted(b["docs"])[:10],
                "sample_titles": b["titles"],
                "industry": b["industry"],
            }
        )
    out.sort(key=lambda c: c["count"], reverse=True)
    return out


def propose_news_candidates(
    candidates: list[dict[str, Any]],
    *,
    table: Any | None = None,
) -> list[str]:
    """Queue discovery reviews for news-only candidates. Returns new review_ids."""
    queued: list[str] = []
    for c in candidates:
        rid = entity_registry.propose_review(
            "discovery",
            key=c["surface"],
            entity_id=None,
            proposed=c["surface"],
            reason=(
                f"news keyword harvest: {c['count']} docs; "
                f"tickers={c.get('tickers')}; industry={c.get('industry')}"
            ),
            hint="; ".join(c.get("sample_titles") or [])[:200],
            confidence="fuzzy",
            table=table,
        )
        if rid:
            queued.append(rid)
    return queued


# ---------------------------------------------------------------------------
# 3. Orchestrator used by Lambda / CLI
# ---------------------------------------------------------------------------


def run_discovery(
    *,
    fiagro: bool = True,
    keyword: str | None = "FIAGRO",
    news_items: Iterable[dict[str, Any]] | None = None,
    auto_create_structured: bool = True,
    min_pl: float = 50_000_000.0,
    table: Any | None = None,
) -> dict[str, Any]:
    """Run the discovery pipeline end-to-end for the FIAGRO vertical (extensible).

    Structured CVM path runs first (strong ids). News keyword harvest is
    propose-only and only runs when ``news_items`` is supplied.
    """
    summary: dict[str, Any] = {"fiagro": None, "keyword": None}

    if fiagro:
        summary["fiagro"] = discover_fiagro(
            min_pl=min_pl,
            auto_create=auto_create_structured,
            table=table,
        )

    if keyword and news_items is not None:
        cands = harvest_keyword(keyword, news_items, table=table)
        proposed = propose_news_candidates(cands, table=table)
        summary["keyword"] = {
            "keyword": keyword,
            "candidates": len(cands),
            "proposed": proposed,
            "top": cands[:10],
        }

    return summary


if __name__ == "__main__":
    import json
    import sys

    # Dry-run against live CVM (no Dynamo writes unless ONCA_ENTITIES_TABLE + --write).
    write = "--write" in sys.argv
    if not write:
        os.environ.pop("ONCA_ENTITIES_TABLE", None)  # force no-op table? we mock below
        print("Dry-run (pass --write and set ONCA_ENTITIES_TABLE to mutate registry)")

    from src.ingest import cvm_fiagro

    rows = cvm_fiagro.fetch_fiagro(min_pl=50e6)
    print(f"Fetched {len(rows)} FIAGRO classes with PL ≥ R$50mi")
    for r in rows[:8]:
        p = _profile_from_fiagro(r)
        print(
            f"  {p['entity_id']:20}  ticker={p.get('ticker')}  "
            f"cnpj={p['cnpj_roots']}  display={p['display_name'][:40]}"
        )

    if write and os.environ.get("ONCA_ENTITIES_TABLE"):
        report = discover_fiagro(min_pl=50e6, auto_create=True)
        print(json.dumps({k: (v if not isinstance(v, list) else len(v)) for k, v in report.items()}, indent=2))
        print("created:", report["created"][:15])
