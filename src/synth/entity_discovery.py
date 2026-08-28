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
        "o", "a", "os", "as", "de", "da", "do", "das", "dos", "e", "em", "no", "na",
        "nos", "nas", "um", "uma", "para", "com", "por", "ao", "à", "aos", "às",
        "que", "se", "su", "seu", "sua", "seus", "suas", "este", "esta", "isto",
        "the", "of", "and", "or", "in", "on", "at", "to", "for", "by", "from",
        "fiagro", "fiagros", "fii", "fiis", "fundo", "fundos", "classe", "classes",
        "cota", "cotas", "imobiliário", "imobiliários", "imobiliaria", "imobiliarias",
        "agro", "crédito", "credito", "rural", "agrícola", "agricola", "imob",
        "ltda", "sa", "s.a.", "s/a", "me", "epp", "eireli", "inc", "corp",
    }
)


def _root8(cnpj: str | None) -> str:
    d = "".join(ch for ch in str(cnpj or "") if ch.isdigit())
    return d[:8] if len(d) >= 8 else ""


def _slug(s: str) -> str:
    s = re.sub(r"[^\w\u00c0-\u024f]+", "-", (s or "").lower()).strip("-")
    return s[:48] if s else ""


def _brand_from_name(name: str) -> str | None:
    """Extract a short brand-like token sequence from a long fund legal name.

    Prefer the distinctive proper-name span before generic suffixes like
    FIAGRO / FII / FUNDO / CLASSE. Falls back to the first 3–4 non-stop tokens.
    """
    if not name:
        return None
    clean = re.sub(r"[\s\u00a0]+", " ", str(name)).strip()
    # Drop common trailing noise.
    clean = re.sub(
        r"\s+(FIAGRO[- ]?I?MO?BILI[AÁ]RIO|FIAGRO|FII|FUNDO DE INVESTIMENTO.*)$",
        "",
        clean,
        flags=re.I,
    )
    tokens = [t for t in re.split(r"[^\w\u00c0-\u024f]+", clean) if t]
    keep: list[str] = []
    for t in tokens:
        if t.lower() in _STOP and keep:
            break
        if t.lower() in _STOP:
            continue
        keep.append(t)
        if len(keep) >= 4:
            break
    return " ".join(keep) if keep else None


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
    entity_id = _slug(ticker or brand or name) or f"fiagro-{root or 'unknown'}"
    admin = (row.get("admin") or "").strip() or None
    manager = (row.get("manager") or "").strip() or None
    if admin and admin not in forms:
        forms.append(admin)
    if manager and manager not in forms:
        forms.append(manager)
    # Dedupe preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for f in forms:
        k = f.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return {
        "entity_id": entity_id,
        "display_name": display,
        "aliases": uniq,
        "cnpj_roots": [root] if root else [],
        "industries": ["agri-funds"],
        "ticker": ticker,
        "isin": (row.get("isin") or None),
        "admin": admin,
        "gestor": manager,
        "manager": manager,
        "pl": row.get("pl"),
        "source": "cvm_fiagro",
        "confidence": "cnpj",
        "raw_name": name,
        "as_of": row.get("as_of"),
        "url": row.get("url"),
    }


def discover_fiagro(
    *,
    min_pl: float = 50_000_000.0,
    max_new: int = 40,
    auto_create: bool = True,
    table: Any | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Sync CVM FIAGRO universe into the entities registry.

    For each class with PL ≥ min_pl:
      - resolve by CNPJ root or ticker alias
      - if hit → enrich (aliases, industries, ticker, admin/gestor)
      - if miss and auto_create → put_entity (strong CNPJ identity)
      - if miss and not auto_create → propose_review

    Returns a report dict with created / enriched / already / proposed / skipped.
    """
    from src.ingest import cvm_fiagro

    if rows is None:
        rows = cvm_fiagro.fetch_fiagro(min_pl=min_pl)
    report: dict[str, Any] = {
        "fetched": len(rows),
        "created": [],
        "enriched": [],
        "already": 0,
        "proposed": [],
        "skipped": [],
        "errors": [],
    }
    if not rows:
        return report

    new_budget = max_new if auto_create else 0

    for row in rows:
        try:
            profile = _profile_from_fiagro(row)
        except Exception as exc:  # pragma: no cover
            report["errors"].append({"row": row.get("cnpj"), "error": str(exc)})
            continue

        root = (profile.get("cnpj_roots") or [None])[0]
        ticker = profile.get("ticker")
        eid = None

        if root:
            eid = entity_registry.resolve_by_cnpj(root, table=table)
        if not eid and ticker:
            eid = entity_registry.resolve_by_alias(ticker, table=table)
        if not eid:
            brand = profile.get("display_name")
            if brand:
                hits = entity_registry.resolve_by_name(brand, table=table)
                if len(hits) == 1:
                    eid = hits[0]

        if eid:
            try:
                changed = False
                if entity_registry.accumulate_aliases(
                    eid, profile.get("aliases") or [], table=table
                ):
                    changed = True
                ent = entity_registry.get_entity(eid, table=table) or {}
                inds = list(ent.get("industries") or [])
                if "agri-funds" not in inds:
                    inds.append("agri-funds")
                    entity_registry.put_entity(
                        {**ent, "entity_id": eid, "industries": inds}, table=table
                    )
                    changed = True
                if ticker and not ent.get("ticker"):
                    entity_registry.put_entity(
                        {**ent, "entity_id": eid, "ticker": ticker}, table=table
                    )
                    changed = True
                if changed:
                    report["enriched"].append(eid)
                else:
                    report["already"] += 1
            except Exception as exc:  # pragma: no cover
                report["errors"].append({"eid": eid, "error": str(exc)})
            continue

        if auto_create and new_budget > 0 and root:
            try:
                brand = profile["display_name"]
                if brand and entity_registry.name_owned_by_other(
                    brand, exclude_id=None, table=table
                ):
                    pid = entity_registry.propose_review(
                        kind="discovery",
                        payload={
                            "reason": "name_collision",
                            "profile": profile,
                            "evidence": {"source": "cvm_fiagro", "cnpj": root},
                        },
                        table=table,
                    )
                    report["proposed"].append(pid or brand)
                    continue
                new_id = entity_registry.put_entity(
                    {
                        "entity_id": profile["entity_id"],
                        "display_name": profile["display_name"],
                        "aliases": profile.get("aliases") or [],
                        "cnpj_roots": profile.get("cnpj_roots") or [],
                        "industries": ["agri-funds"],
                        "ticker": ticker,
                        "confidence": "cnpj",
                        "source": "cvm_fiagro",
                        "admin": profile.get("admin"),
                        "gestor": profile.get("gestor"),
                    },
                    table=table,
                )
                report["created"].append(new_id or profile["entity_id"])
                new_budget -= 1
            except Exception as exc:  # pragma: no cover
                report["errors"].append(
                    {"profile": profile.get("entity_id"), "error": str(exc)}
                )
        else:
            try:
                pid = entity_registry.propose_review(
                    kind="discovery",
                    payload={
                        "reason": "fiagro_missing" if root else "fiagro_no_cnpj",
                        "profile": profile,
                        "evidence": {
                            "source": "cvm_fiagro",
                            "cnpj": root,
                            "ticker": ticker,
                            "pl": profile.get("pl"),
                        },
                    },
                    table=table,
                )
                report["proposed"].append(pid or profile["entity_id"])
            except Exception as exc:  # pragma: no cover
                report["errors"].append(
                    {"profile": profile.get("entity_id"), "error": str(exc)}
                )

    return report


def harvest_keyword(
    keyword: str,
    news_items: Iterable[dict[str, Any]],
    *,
    industry: str | None = None,
    min_docs: int = 2,
    table: Any | None = None,
) -> list[dict[str, Any]]:
    """Scan news items for a keyword and collect unresolved associated entities.

    Frequency-gated (min_docs): a name must appear in ≥ min_docs distinct items
    before becoming a candidate. Resolved entities (via resolve_entities) are
    dropped. Returns a list of candidate dicts with evidence (surface form).
    """
    kw = (keyword or "").strip().upper()
    if not kw:
        return []

    ticker_hits: dict[str, list[str]] = {}
    brand_hits: dict[str, list[str]] = {}
    evidence: dict[str, list[dict[str, Any]]] = {}

    ticker_re = re.compile(r"\b([A-Z]{4}11)\b")
    brand_re = re.compile(
        r"\b([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÁÉÍÓÚÂÊÔÃÕÇáéíóúâêôãõç]{1,20}"
        r"(?:\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÁÉÍÓÚÂÊÔÃÕÇáéíóúâêôãõç]{1,20}){1,3})\b"
    )

    for item in news_items or []:
        if not isinstance(item, dict):
            continue
        text = " ".join(
            str(item.get(k) or "") for k in ("title", "text", "summary", "headline")
        )
        if not text or kw not in text.upper():
            continue
        doc_id = str(item.get("id") or item.get("url") or hash(text) % 10**10)
        for m in ticker_re.finditer(text.upper()):
            t = m.group(1)
            ticker_hits.setdefault(t, []).append(doc_id)
            evidence.setdefault(t, []).append(
                {"doc_id": doc_id, "snippet": text[max(0, m.start() - 40) : m.end() + 40]}
            )
        for m in brand_re.finditer(text):
            brand = m.group(1).strip()
            toks = [t for t in brand.split() if t.lower() not in _STOP]
            if len(toks) < 1:
                continue
            window = text[max(0, m.start() - 120) : m.end() + 120].upper()
            if kw not in window:
                continue
            brand_hits.setdefault(brand, []).append(doc_id)
            evidence.setdefault(brand, []).append(
                {"doc_id": doc_id, "snippet": text[max(0, m.start() - 40) : m.end() + 40]}
            )

    candidates: list[dict[str, Any]] = []
    ind = industry or INDUSTRY_KEYWORDS.get(kw) or INDUSTRY_KEYWORDS.get(kw.rstrip("S"))

    def _freq_ok(ids: list[str]) -> bool:
        return len(set(ids)) >= min_docs

    all_labels = list(ticker_hits.keys()) + list(brand_hits.keys())
    resolved_map: dict[str, Any] = {}
    try:
        resolved_map = resolve_entities(all_labels, table=table) or {}
    except Exception:
        resolved_map = {}

    for label, ids in list(ticker_hits.items()) + list(brand_hits.items()):
        if not _freq_ok(ids):
            continue
        if resolved_map.get(label) or resolved_map.get(label.upper()) or resolved_map.get(
            label.lower()
        ):
            continue
        try:
            if entity_registry.resolve_by_alias(label, table=table):
                continue
            if entity_registry.resolve_by_name(label, table=table):
                continue
        except Exception:
            pass
        candidates.append(
            {
                "surface": label,
                "kind": "ticker" if re.fullmatch(r"[A-Z]{4}11", label) else "brand",
                "tickers": [label] if re.fullmatch(r"[A-Z]{4}11", label) else [],
                "doc_count": len(set(ids)),
                "count": len(set(ids)),
                "industry": ind,
                "keyword": kw,
                "evidence_ids": list(set(ids))[:8],
                "evidence": (evidence.get(label) or [])[:5],
                "sample_titles": [
                    (e.get("snippet") or "")[:80] for e in (evidence.get(label) or [])[:3]
                ],
            }
        )

    candidates.sort(
        key=lambda c: (-c["doc_count"], 0 if c["kind"] == "ticker" else 1, c["surface"])
    )
    return candidates


def propose_news_candidates(
    candidates: list[dict[str, Any]],
    *,
    table: Any | None = None,
    max_propose: int = 20,
) -> list[str]:
    """Emit review-queue proposals for news-only discovery candidates.

    Never auto-creates from news alone (ADR 011 §4). Returns proposal ids.
    """
    proposed: list[str] = []
    for c in (candidates or [])[:max_propose]:
        try:
            pid = entity_registry.propose_review(
                kind="discovery",
                payload={
                    "reason": "news_keyword_harvest",
                    "surface": c.get("surface") or c.get("label"),
                    "kind": c.get("kind"),
                    "industry": c.get("industry"),
                    "keyword": c.get("keyword"),
                    "doc_count": c.get("doc_count") or c.get("count"),
                    "evidence": c.get("evidence") or [],
                    "evidence_ids": c.get("evidence_ids") or [],
                    "sample_titles": c.get("sample_titles") or [],
                    "tickers": c.get("tickers") or [],
                },
                table=table,
            )
            if pid:
                proposed.append(pid)
            else:
                proposed.append(str(c.get("surface") or c.get("label")))
        except Exception:
            continue
    return proposed


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

    write = "--write" in sys.argv
    if not write:
        os.environ.pop("ONCA_ENTITIES_TABLE", None)
        print("Dry-run (pass --write and set ONCA_ENTITIES_TABLE to mutate registry)")

    from src.ingest import cvm_fiagro

    rows = cvm_fiagro.fetch_fiagro(min_pl=50e6)
    print(f"Fetched {len(rows)} FIAGRO classes with PL ≥ R$50mi")
    for r in rows[:8]:
        p = _profile_from_fiagro(r)
        print(
            f"  {p['entity_id']:20}  ticker={p.get('ticker')}  "
            f"cnpj={p['cnpj_roots']}  display={str(p['display_name'])[:40]}"
        )

    if write and os.environ.get("ONCA_ENTITIES_TABLE"):
        report = discover_fiagro(min_pl=50e6, auto_create=True)
        print(
            json.dumps(
                {k: (v if not isinstance(v, list) else len(v)) for k, v in report.items()},
                indent=2,
            )
        )
        print("created:", report["created"][:15])
