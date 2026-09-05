"""ADR 021 §D Step 1 — the append-only decision-capture store (`OncaDecisionLog`).

Every executive decision on an officer recommendation — *aprovado / rejeitado / adiado* — is
recorded here, then (once observed) stamped with a realized *outcome*. Decisions + outcomes are
the highest-value proprietary data the product produces: the labels for the §E metrics (ETS/TDR)
and the §F expertise flywheel (promoted into the KB as precedents).

Storage: `DECISION#<id>` items in the entities table (type=``decision``), reusing the registry
client — same pattern as `ACT#`/`WATCH#`/`REVIEW#`, so no new table/grant is needed to ship the
capture. (A dedicated `OncaDecisionLog` table is a clean future extraction once volume/query
patterns justify it.) The §H CORS beacon later appends consulted-source links to `references`.
"""
from __future__ import annotations

import uuid
from typing import Any

from src.synth import entity_registry as _er

_VERDICTS = {"aprovado", "rejeitado", "adiado"}
_OUTCOMES = {"pendente", "favoravel", "desfavoravel", "neutro"}


def _table(table: Any | None = None):
    return _er._table(table)


def record_decision(
    *,
    officer: str,
    recommendation: str,
    verdict: str,
    actor: str,
    industry: str | None = None,
    action_ref: str | None = None,
    evidence_id: str | None = None,
    context_id: str | None = None,
    rationale: str | None = None,
    table: Any | None = None,
) -> dict[str, Any]:
    """Append a decision. Returns the stored item (incl. the generated ``decision_id``).

    ``verdict`` ∈ {aprovado, rejeitado, adiado}. Outcome starts ``pendente`` — set later via
    :func:`set_outcome` when the result is observed. Raises ValueError on a bad verdict / empty
    recommendation."""
    verdict = (verdict or "").strip().lower()
    if verdict not in _VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(_VERDICTS)}")
    if not (recommendation or "").strip():
        raise ValueError("recommendation required")
    did = uuid.uuid4().hex[:16]
    item = {
        "pk": f"DECISION#{did}",
        "type": "decision",
        "decision_id": did,
        "context_id": context_id,
        "officer": (officer or "").strip() or None,
        "industry": (industry or "").strip() or None,
        "recommendation": recommendation.strip(),
        "action_ref": action_ref,
        "evidence_id": evidence_id,
        "verdict": verdict,
        "rationale": (rationale or "").strip() or None,
        "actor": actor,
        "created_at": _er._now_iso(),
        "outcome": "pendente",
        "outcome_note": None,
        "outcome_at": None,
        "references": [],
    }
    _table(table).put_item(Item={k: v for k, v in item.items() if v is not None or k in
                                 ("context_id", "action_ref", "evidence_id", "rationale",
                                  "outcome_note", "outcome_at")})
    return item


def get_decision(decision_id: str, table: Any | None = None) -> dict[str, Any] | None:
    it = _table(table).get_item(Key={"pk": f"DECISION#{decision_id}"}).get("Item")
    return it or None


def set_outcome(
    decision_id: str,
    outcome: str,
    *,
    actor: str,
    note: str | None = None,
    table: Any | None = None,
) -> dict[str, Any] | None:
    """Stamp the realized outcome on an existing decision. ``outcome`` ∈ {pendente, favoravel,
    desfavoravel, neutro}. Returns the updated item, or None if the decision does not exist.
    (This is the reward-signal label for §E/§F.)"""
    outcome = (outcome or "").strip().lower()
    if outcome not in _OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(_OUTCOMES)}")
    t = _table(table)
    it = t.get_item(Key={"pk": f"DECISION#{decision_id}"}).get("Item")
    if not it:
        return None
    it["outcome"] = outcome
    it["outcome_note"] = (note or "").strip() or None
    it["outcome_at"] = _er._now_iso()
    it["outcome_by"] = actor
    t.put_item(Item=it)
    return it


def append_reference(
    decision_id: str,
    url: str,
    *,
    officer: str | None = None,
    table: Any | None = None,
) -> bool:
    """§H beacon hook: append a consulted-source link to a decision's evidence trail. Best-effort,
    idempotent per url. Returns True if the reference was added."""
    if not (url or "").strip():
        return False
    t = _table(table)
    it = t.get_item(Key={"pk": f"DECISION#{decision_id}"}).get("Item")
    if not it:
        return False
    refs = list(it.get("references") or [])
    if any(r.get("url") == url for r in refs):
        return False
    refs.append({"url": url, "officer": officer, "ts": _er._now_iso()})
    it["references"] = refs
    t.put_item(Item=it)
    return True


def mark_promoted(decision_id: str, table: Any | None = None) -> bool:
    """Seen-set gate for §H decision→KB promotion: stamp a decision as promoted so the next
    pipeline cycle never re-ingests it. Returns True if stamped."""
    t = _table(table)
    it = t.get_item(Key={"pk": f"DECISION#{decision_id}"}).get("Item")
    if not it:
        return False
    it["kb_promoted"] = True
    it["kb_promoted_at"] = _er._now_iso()
    t.put_item(Item=it)
    return True


def list_decisions(
    *,
    officer: str | None = None,
    industry: str | None = None,
    since: str | None = None,
    table: Any | None = None,
) -> list[dict[str, Any]]:
    """Scan captured decisions (newest first), optionally filtered by officer / industry /
    created-at floor. Powers the §E metrics rollup and the §F KB promotion."""
    items = _er._scan_type(_table(table), "decision")
    out = []
    for d in items:
        if officer and d.get("officer") != officer:
            continue
        if industry and d.get("industry") != industry:
            continue
        if since and str(d.get("created_at") or "") < since:
            continue
        out.append(d)
    out.sort(key=lambda d: str(d.get("created_at") or ""), reverse=True)
    return out
