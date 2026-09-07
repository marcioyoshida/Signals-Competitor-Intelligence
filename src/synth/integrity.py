"""ADR 018 Phase 3 — continuous integrity audit of the registry + feed.

Turns the recurring silent-corruption incidents (#50, #52, ADR-017) into invariant
detectors run every feed build. Emits durable *findings* (surfaced in the dashboard's
Integridade view); `safe_fix` marks the ones a remediation pass could auto-correct now
that Phase 2 precedence + provenance exist. Read-only — never mutates.
"""
from __future__ import annotations

import datetime as _dt
import re
import unicodedata
from typing import Any

# Entry-tier/fund (leaf) industries: an operating company that carries one of these AND a
# higher-tier industry is the #52 pollution signature.
_LEAF = frozenset({"agri-funds", "real-estate-funds", "consorcio", "betting", "crypto"})
_AUTO_SOURCES = frozenset({"enrich", "discovery", "inferred"})
_FUND_TICKER = re.compile(r"^[A-Z]{4}11$")  # FII/FIAGRO ticker shape
_FUSION_KINDS = frozenset({"entity_fusion", "news_corroborated", "competitor:news"})


def _norm(s: Any) -> str:
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().upper()


def _finding(kind: str, severity: str, summary: str, *, entity_id: str | None = None,
             card_id: str | None = None, safe_fix: bool = False) -> dict[str, Any]:
    key = entity_id or card_id or summary[:40]
    return {"id": f"integ:{kind}:{key}", "kind": kind, "severity": severity,
            "entity_id": entity_id, "card_id": card_id, "summary": summary, "safe_fix": safe_fix}


def audit_registry(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {e.get("entity_id"): e for e in entities}
    out: list[dict[str, Any]] = []
    for e in entities:
        eid = e.get("entity_id")
        if not eid:
            continue
        inds = set(e.get("industries") or [])
        prov = e.get("_prov") or {}
        # #52 signature: an institution (has a higher-tier industry) that also carries a
        # leaf/fund industry set by an AUTOMATED source.
        if (inds - _LEAF) and (inds & _LEAF):
            src = (prov.get("industries") or {}).get("source")
            if src in _AUTO_SOURCES:
                out.append(_finding(
                    "institution_leaf_pollution", "high",
                    f"{eid}: institutional {sorted(inds - _LEAF)} + leaf {sorted(inds & _LEAF)} "
                    f"(industries provenance={src})", entity_id=eid, safe_fix=True))
        # Fund ticker left as an alias on an institution (the loop's fuel, #52).
        if inds - _LEAF:
            real = (e.get("ticker") or "").upper()
            bad = sorted({a for a in (e.get("aliases") or [])
                          if _FUND_TICKER.match(a) and a != real})
            if bad:
                out.append(_finding("fund_alias_on_institution", "high",
                                    f"{eid} carries fund tickers as aliases: {bad}",
                                    entity_id=eid, safe_fix=True))
        # Unbacked structured identity: claims CNPJ confidence with no CNPJ root.
        if e.get("confidence") == "cnpj" and not (e.get("cnpj_roots") or []):
            out.append(_finding("unbacked_cnpj", "med",
                                f"{eid}: confidence=cnpj but no cnpj_roots", entity_id=eid))
        # ADR-017 inversion: a sub-entity whose parent is itself a pure leaf/fund.
        p = e.get("parent")
        if p and p in by_id:
            pinds = set(by_id[p].get("industries") or [])
            if pinds and not (pinds - _LEAF):
                out.append(_finding("parent_inversion", "med",
                                    f"{eid}.parent={p} is a leaf/fund, not a tier-1 institution",
                                    entity_id=eid))
    return out


def audit_feed(feed: dict[str, Any], entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {e.get("entity_id"): e for e in entities}

    def mentions(eid: str, text: str) -> bool:
        e = by_id.get(eid) or {}
        forms = {a for a in (e.get("aliases") or [])} | {_norm(e.get("display_name"))}
        for f in forms:
            if len(f) >= 2 and re.search(r"(?<![A-Z0-9])" + re.escape(f) + r"(?![A-Z0-9])", text):
                return True
        return False

    out: list[dict[str, Any]] = []
    for c in (feed.get("feed") or []):
        ent, kind = c.get("entity"), (c.get("kind") or "")
        if not ent or c.get("is_inference"):
            continue
        if kind not in _FUSION_KINDS and not kind.startswith("competitor"):
            continue
        if ent not in by_id:  # untracked primary — separate concern
            continue
        if not mentions(ent, _norm(c.get("narrative"))):
            # #50 signature: the attributed subject never appears in its own narrative.
            named = [o for o in by_id if o != ent and mentions(o, _norm(c.get("narrative")))]
            out.append(_finding(
                "card_primary_absent", "med" if named else "low",
                f"card '{c.get('id')}' attributed to {ent}, absent from its narrative"
                + (f"; names instead: {named[:4]}" if named else ""),
                card_id=c.get("id")))
    return out


# #106 (#14 Stage 5) — ingestion follow-up probe: an entity that was added but is not
# surfacing. Two classes: STRUCTURAL (no viable path to ever surface — a config bug) and
# STALE (has a path but produced nothing in the current feed window after M days). Radar
# `registry`/`identified` = the auto-discovered entities (confidence structured/cnpj); curated
# majors are excluded (no provenance stamp / always surface).
_AUTO_CONF = frozenset({"structured", "cnpj"})
_COHORT_MIN = 8          # a cohort must have this many aged entities before a gap is meaningful
_COHORT_QUIET_FRAC = 0.8  # …and this fraction not surfacing to call it a coverage gap


def _created_at(e: dict[str, Any]) -> _dt.datetime | None:
    """Creation time: the durable set-once ``created_at`` (#106), falling back to the
    earliest provenance ``set_at`` for entities written before that field existed."""
    raw = e.get("created_at")
    if not raw:
        stamps = [(v or {}).get("set_at") for v in (e.get("_prov") or {}).values() if isinstance(v, dict)]
        stamps = [s for s in stamps if s]
        raw = min(stamps) if stamps else None
    if not raw:
        return None
    try:
        return _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover - defensive
        return None


def audit_surfacing(
    feed: dict[str, Any], entities: list[dict[str, Any]], *,
    now: _dt.datetime | None = None, min_age_days: int = 21,
) -> list[dict[str, Any]]:
    """#106 (#14 Stage 5) — ingestion follow-up probe.

    STRUCTURAL (per-entity): an auto-discovered entity with no viable path to surface
    (news off + no CNPJ + no filing term) — a config bug, always flagged.
    COVERAGE GAP (per-industry cohort): when most aged auto-discovered entities of an
    industry never surface, that is one gap ("roster added, no per-entity signal source"),
    emitted ONCE per industry rather than as hundreds of per-entity findings.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    surfaced: set[str] = set()
    for c in (feed.get("feed") or []):
        if c.get("entity"):
            surfaced.add(c["entity"])
        surfaced.update(c.get("entities") or [])
    for r in (feed.get("entities") or []):
        if r.get("entity"):
            surfaced.add(r["entity"])

    out: list[dict[str, Any]] = []
    # industry → [aged_total, aged_quiet]
    cohort: dict[str, list[int]] = {}
    for e in entities:
        eid = e.get("entity_id")
        if not eid or str(e.get("confidence") or "") not in _AUTO_CONF:
            continue  # only auto-discovered entities (curated majors excluded)
        seen = eid in surfaced
        # STRUCTURAL — no path to surface at all.
        if (not seen and e.get("news_search") is False
                and not (e.get("cnpj_roots") or [])
                and not e.get("fatos_term")):
            out.append(_finding(
                "entity_no_surface_path", "med",
                f"{eid}: added but has no path to surface (news off, no CNPJ, no filing term)",
                entity_id=eid, safe_fix=True))
            continue
        # COVERAGE-GAP cohorts — count aged non-fund entities by industry (funds excluded:
        # silence is normal for a fund until a PL/cotista move).
        inds = set(e.get("industries") or [])
        if inds and not (inds - _LEAF):
            continue
        created = _created_at(e)
        if created is None or (now - created).days < min_age_days:
            continue
        for ind in (inds or {"(unclassified)"}):
            c = cohort.setdefault(ind, [0, 0])
            c[0] += 1
            if not seen:
                c[1] += 1

    for ind, (total, quiet) in sorted(cohort.items()):
        if total >= _COHORT_MIN and quiet >= _COHORT_QUIET_FRAC * total:
            out.append(_finding(
                "industry_not_surfacing", "med" if quiet == total else "low",
                f"industry {ind}: {quiet}/{total} registry entities ({min_age_days}d+ old) never "
                f"surface — likely no per-entity signal source (coverage gap)",
                entity_id=f"industry:{ind}"))
    return out


def audit(feed: dict[str, Any], entities: list[dict[str, Any]]) -> dict[str, Any]:
    findings = audit_registry(entities) + audit_feed(feed, entities) + audit_surfacing(feed, entities)
    order = {"high": 0, "med": 1, "low": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 3))
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["kind"]] = counts.get(f["kind"], 0) + 1
    return {"findings": findings, "counts": counts, "total": len(findings)}
