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

# DEC-5 (#98) — decisions under ADR-018 governance (WRITE side). The reward-signal labels a
# human stamps on a decision (its realized ``outcome`` and ``board_adopted`` flag) are the
# highest-value fields the product produces (§E/§F). They get the same per-field provenance +
# write-precedence + rollback the entity registry gives curated classification: a weaker/
# automated write may never DEMOTE a human's label, and a bad stamp can be reverted over the
# journal. We reuse the registry's ADR-018 primitives (``_prov_entry`` / ``_PROV_PROTECT`` /
# ``_log`` / ``entity_history``) so there is ONE governance mechanism, not two. Journal + prov
# key the decision as ``DECISION#<id>`` (the item pk), so ``entity_history`` reads it back.
_MISSING = object()
_GOVERNED_FIELDS = ("outcome", "board_adopted")  # rollback-supported reward-signal labels


def _table(table: Any | None = None):
    return _er._table(table)


def _prov_of(it: dict[str, Any], field: str) -> dict[str, Any] | None:
    return (it.get("_prov") or {}).get(field)


def _may_write(it: dict[str, Any], field: str, source: str) -> bool:
    """ADR-018 precedence for a decision field: a field with no provenance is open; otherwise
    the write must be at least as strong as the field's current provenance. Blocks an automated
    (inferred/enrich) writer from demoting a human ``curated`` label."""
    cur = _prov_of(it, field)
    if not cur:
        return True
    return _er._PROV_PROTECT.get(source, 0) >= _er._PROV_PROTECT.get(cur.get("source"), 0)


def _stamp(it: dict[str, Any], fields: tuple[str, ...], source: str,
           confidence: str | None = None) -> None:
    prov = it.setdefault("_prov", {})
    for f in fields:
        prov[f] = dict(_er._prov_entry(source, confidence))


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
    started_at: str | None = None,
    source: str = "curated",
    table: Any | None = None,
) -> dict[str, Any]:
    """Append a decision. Returns the stored item (incl. the generated ``decision_id``).

    ``verdict`` ∈ {aprovado, rejeitado, adiado}. Outcome starts ``pendente`` — set later via
    :func:`set_outcome` when the result is observed. ``started_at`` (ISO) is when the executive
    FIRST engaged the item — with ``created_at`` it gives the real deliberation time (§E TDR).
    ``source`` (ADR-018, DEC-5) stamps who set the label — a human/API decision is ``curated``.
    Raises ValueError on a bad verdict / empty recommendation."""
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
        "started_at": (started_at or "").strip() or None,
        "created_at": _er._now_iso(),
        "outcome": "pendente",
        "outcome_note": None,
        "outcome_at": None,
        "references": [],
    }
    _stamp(item, _GOVERNED_FIELDS, source)  # DEC-5: birth-provenance on the reward-signal labels
    _table(table).put_item(Item={k: v for k, v in item.items() if v is not None or k in
                                 ("context_id", "action_ref", "evidence_id", "rationale",
                                  "outcome_note", "outcome_at")})
    _er._log(f"DECISION#{did}", "record_decision", source,
             {"field": "verdict", "new": verdict, "before": None, "officer": item["officer"]})
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
    source: str = "curated",
    table: Any | None = None,
) -> dict[str, Any] | None:
    """Stamp the realized outcome on an existing decision. ``outcome`` ∈ {pendente, favoravel,
    desfavoravel, neutro}. Returns the updated item, or None if the decision does not exist.
    (This is the reward-signal label for §E/§F.)

    DEC-5 (#98): under ADR-018 write-precedence, a weaker/automated ``source`` may NOT demote a
    human ``curated`` outcome — such a write is rejected (item unchanged) and journalled as
    ``blocked``. Human/API stamps are ``curated`` (the default) and always win. Every applied
    write is journalled with its before/after so it can be rolled back."""
    outcome = (outcome or "").strip().lower()
    if outcome not in _OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(_OUTCOMES)}")
    t = _table(table)
    it = t.get_item(Key={"pk": f"DECISION#{decision_id}"}).get("Item")
    if not it:
        return None
    if not _may_write(it, "outcome", source):
        _er._log(f"DECISION#{decision_id}", "blocked", source,
                 {"field": "outcome", "rejected": outcome,
                  "held": it.get("outcome"), "reason": "precedence"})
        return it
    before = it.get("outcome")
    it["outcome"] = outcome
    it["outcome_note"] = (note or "").strip() or None
    it["outcome_at"] = _er._now_iso()
    it["outcome_by"] = actor
    _stamp(it, ("outcome",), source)
    t.put_item(Item=it)
    _er._log(f"DECISION#{decision_id}", "set_outcome", source,
             {"field": "outcome", "new": outcome, "before": before, "actor": actor})
    return it


def set_board_adoption(
    decision_id: str,
    adopted: bool,
    *,
    actor: str,
    source: str = "curated",
    table: Any | None = None,
) -> dict[str, Any] | None:
    """Flag whether a decision was escalated to / adopted by the board — the §E Board-Adoption
    component of the ETS. Returns the updated item, or None if the decision does not exist.

    DEC-5 (#98): governed like :func:`set_outcome` — a weaker/automated ``source`` may not demote
    a human ``curated`` flag; applied writes are journalled with before/after for rollback."""
    t = _table(table)
    it = t.get_item(Key={"pk": f"DECISION#{decision_id}"}).get("Item")
    if not it:
        return None
    if not _may_write(it, "board_adopted", source):
        _er._log(f"DECISION#{decision_id}", "blocked", source,
                 {"field": "board_adopted", "rejected": bool(adopted),
                  "held": it.get("board_adopted"), "reason": "precedence"})
        return it
    before = it.get("board_adopted")
    it["board_adopted"] = bool(adopted)
    it["board_at"] = _er._now_iso()
    it["board_by"] = actor
    _stamp(it, ("board_adopted",), source)
    t.put_item(Item=it)
    _er._log(f"DECISION#{decision_id}", "set_board_adoption", source,
             {"field": "board_adopted", "new": bool(adopted), "before": before, "actor": actor})
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


def link_action(decision_id: str, *, intent: str, outcome: str, actor: str,
                act_key: str | None = None, table: Any | None = None) -> bool:
    """DEC-6 (#99): record on a decision the `/api/act` action it authorized — closing the loop
    from a captured decision to its EXECUTION and effect. Appends to the decision's ``actions``
    trail (intent + the call's outcome). Best-effort; returns True if stamped."""
    t = _table(table)
    it = t.get_item(Key={"pk": f"DECISION#{decision_id}"}).get("Item")
    if not it:
        return False
    acts = list(it.get("actions") or [])
    acts.append({"intent": intent, "outcome": outcome, "actor": actor,
                 "act_key": act_key, "at": _er._now_iso()})
    it["actions"] = acts
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


_TDR_CONFIG_PK = "CONFIG#tdr_baseline"


def set_tdr_baseline(hours: Any, *, actor: str, table: Any | None = None) -> dict[str, Any]:
    """Record the per-tenant **TDR baseline** (ADR-021 §E, DEC-2): the executive's typical
    *pre-Onça* decision latency in hours — a REAL number supplied by the tenant, never assumed.
    Stored as a singleton ``CONFIG#tdr_baseline`` item so it survives without a redeploy (the env
    ``ONCA_TDR_BASELINE_HOURS`` remains a fallback). Raises ValueError on a non-positive value."""
    try:
        h = float(hours)
    except (TypeError, ValueError):
        raise ValueError("hours must be a number")
    if h <= 0:
        raise ValueError("baseline hours must be > 0")
    item = {"pk": _TDR_CONFIG_PK, "type": "config", "config": "tdr_baseline",
            "baseline_hours": h, "actor": actor, "set_at": _er._now_iso()}
    _table(table).put_item(Item=item)
    return item


def get_tdr_baseline(table: Any | None = None) -> float | None:
    """The recorded per-tenant TDR baseline in hours, or None if never recorded."""
    it = _table(table).get_item(Key={"pk": _TDR_CONFIG_PK}).get("Item")
    if not it:
        return None
    try:
        h = float(it.get("baseline_hours"))
        return h if h > 0 else None
    except (TypeError, ValueError):
        return None


# DEC-5 (#98) — ADR-018 rollback over the decision journal. Each governed write logs its NEW and
# BEFORE value, so a field's value "just before time T" is the NEW value of the most recent write
# strictly earlier than T (mirrors the registry's ``field_value_before``). Rollback re-applies it
# as a ``curated`` write, so it wins precedence and re-stamps provenance.
def decision_history(decision_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """The mutation journal for one decision, newest first. [] if no log table is configured."""
    return _er.entity_history(f"DECISION#{decision_id}", limit=limit)


def _dec_field_before(history: list[dict[str, Any]], field: str, before_ts: str) -> Any:
    for h in history:  # newest first
        if str(h.get("ts", "")) >= str(before_ts):
            continue
        d = h.get("detail") or {}
        if d.get("field") == field and "new" in d:
            return d.get("new")
    return _MISSING


def rollback_decision_field(decision_id: str, field: str, before_ts: str, *,
                            actor: str, table: Any | None = None) -> bool:
    """Restore a governed field (``outcome`` / ``board_adopted``) to its value just before
    ``before_ts`` — undoes a bad stamp. Re-applied as a ``curated`` write (wins precedence).
    Returns True if a rollback was applied."""
    if field not in _GOVERNED_FIELDS:
        raise ValueError(f"rollback unsupported for {field!r}; one of {_GOVERNED_FIELDS}")
    v = _dec_field_before(decision_history(decision_id), field, before_ts)
    if v is _MISSING:
        return False
    if field == "outcome":
        ok = set_outcome(decision_id, str(v), actor=actor, source="curated", table=table) is not None
    else:
        ok = set_board_adoption(decision_id, bool(v), actor=actor, source="curated", table=table) is not None
    if ok:
        _er._log(f"DECISION#{decision_id}", "rollback", "curated",
                 {"field": field, "restored": v, "before": before_ts, "actor": actor})
    return ok


def revert_decision_since(decision_id: str, since_ts: str, *, actor: str,
                          table: Any | None = None) -> list[str]:
    """Roll back every governed field a decision changed at/after ``since_ts`` to its prior
    state. Returns the fields reverted (undoes a bad automated run in one shot)."""
    hist = decision_history(decision_id)
    touched = {(h.get("detail") or {}).get("field") for h in hist
               if str(h.get("ts", "")) >= str(since_ts)}
    return [f for f in _GOVERNED_FIELDS
            if f in touched and rollback_decision_field(decision_id, f, since_ts,
                                                        actor=actor, table=table)]


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
