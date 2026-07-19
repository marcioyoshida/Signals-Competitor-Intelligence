"""Extract fusion candidates from an ingest digest (Stage B v1 heuristics)."""
from __future__ import annotations

import re
from typing import Any
from uuid import uuid4


def extract_candidates(
    digest: dict[str, Any],
    max_candidates: int = 10,
) -> list[dict[str, Any]]:
    """Build correlation candidates from digest sections.

    Prefer regulatory seeds; attach competitor signals as related context.
    Standalone competitor signals fill remaining slots.
    """
    regulatory = _section_items(digest, "regulatory") or digest.get("new_normativos") or []
    competitor_blocks = _all_competitor_signals(digest)

    candidates: list[dict[str, Any]] = []
    used_comp_ids: set[str] = set()

    for reg in regulatory:
        if len(candidates) >= max_candidates:
            break
        related = _match_related(reg, competitor_blocks, used_comp_ids)
        for r in related:
            if r.get("id"):
                used_comp_ids.add(str(r["id"]))
        sources = [reg, *related]
        candidates.append(
            {
                "id": f"cand-{_short_id(reg.get('id') or uuid4().hex)}",
                "kind": "regulatory_fusion" if related else "regulatory",
                "seed": reg,
                "related": related,
                "sources": sources,
                "threat_score": _threat_score(reg, related),
            }
        )

    for block_name, items in competitor_blocks:
        for item in items:
            if len(candidates) >= max_candidates:
                break
            iid = str(item.get("id") or "")
            if iid and iid in used_comp_ids:
                continue
            if iid:
                used_comp_ids.add(iid)
            candidates.append(
                {
                    "id": f"cand-{_short_id(iid or uuid4().hex)}",
                    "kind": f"competitor:{block_name}",
                    "seed": item,
                    "related": [],
                    "sources": [item],
                    "threat_score": _threat_score(item, []),
                }
            )

    candidates.sort(key=lambda c: c.get("threat_score") or 0, reverse=True)
    return candidates[:max_candidates]


def _section_items(digest: dict[str, Any], key: str) -> list[dict[str, Any]]:
    section = digest.get(key)
    if isinstance(section, dict):
        return list(section.get("items") or [])
    if isinstance(section, list):
        return section
    return []


def _all_competitor_signals(
    digest: dict[str, Any],
) -> list[tuple[str, list[dict[str, Any]]]]:
    mapping = [
        ("funds", _section_items(digest, "competitor") or digest.get("new_fund_filings") or []),
        ("ofertas", _section_items(digest, "ofertas") or digest.get("new_ofertas") or []),
        ("entrants", _section_items(digest, "new_entrants") or digest.get("new_entrants") or []),
        ("pix_moves", _section_items(digest, "pix_moves") or digest.get("pix_moves") or []),
        ("juros_moves", _section_items(digest, "juros_moves") or digest.get("juros_moves") or []),
        (
            "inf_diario_moves",
            _section_items(digest, "inf_diario_moves") or digest.get("inf_diario_moves") or [],
        ),
        ("sec", digest.get("new_sec_filings") or _section_items(digest, "sec_filings") or []),
    ]
    out: list[tuple[str, list[dict[str, Any]]]] = []
    for name, items in mapping:
        # pix/juros sections may nest items under dict
        if isinstance(items, dict):
            items = list(items.get("items") or [])
        clean = [i for i in items if isinstance(i, dict)]
        if clean:
            out.append((name, clean))
    return out


def _match_related(
    reg: dict[str, Any],
    competitor_blocks: list[tuple[str, list[dict[str, Any]]]],
    used: set[str],
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Loose keyword overlap between regulatory subject and competitor names."""
    hay = " ".join(
        str(reg.get(k) or "") for k in ("subject", "doc_type", "number", "title")
    ).upper()
    tokens = {t for t in re.split(r"[^A-Z0-9]+", hay) if len(t) >= 4}
    related: list[dict[str, Any]] = []
    for _name, items in competitor_blocks:
        for item in items:
            iid = str(item.get("id") or "")
            if iid and iid in used:
                continue
            blob = " ".join(
                str(item.get(k) or "")
                for k in (
                    "institution",
                    "name",
                    "issuer",
                    "leader",
                    "admin",
                    "fund_name",
                    "modality",
                    "security",
                )
            ).upper()
            if tokens and any(t in blob for t in tokens):
                related.append(item)
            if len(related) >= limit:
                return related
    return related


def _threat_score(seed: dict[str, Any], related: list[dict[str, Any]]) -> float:
    """Placeholder scoring — not a calibrated model; labeled estimated later."""
    score = 0.3
    if seed.get("kind") == "regulatory" or seed.get("doc_type"):
        score += 0.3
    score += min(0.3, 0.1 * len(related))
    if seed.get("pct_change") and abs(float(seed["pct_change"])) >= 15:
        score += 0.2
    return round(min(1.0, score), 3)


def _short_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9:_-]+", "-", str(value))[:48]
    return cleaned or uuid4().hex[:12]
