"""board/v1 contracts (#324): the envelope the orchestrator sends an officer, the position an
officer must return, validation, and the one-re-prompt rule.

An officer here is one of the five /exec boards (ADR 032), keyed by its internal id. It answers
from the run's **frozen context pack only** (passed as ``converse(context=…)``), never from a
live KB retrieval, so a run can be replayed and every citation resolves to a pack fact.

Nothing in this module calls a model by itself: ``ask_officer`` takes the model call as a
function, so the phase runner and the tests decide what that call is.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from src.dashboard import officers

SCHEMA_ID = "board/v1/position"

# Board order = the /exec display order (ADR 032).
BOARD_OFFICERS: tuple[str, ...] = ("cso", "risk", "cco", "cpo", "cro")
PHASES: tuple[str, ...] = ("diagnosis", "challenge", "quantify", "synthesize")
IMPACT_BANDS: tuple[str, ...] = ("low", "medium", "high")
MAX_OPTIONS = 3
MAX_TEXT = 4000  # per free-text field; a longer field is a filler signal, not more substance

# A position is valid, or it is recorded as invalid after one re-prompt (ADR 031 §2).
STATUS_OK, STATUS_INVALID, STATUS_NO_ANSWER = "ok", "invalid", "no_answer"


def make_envelope(*, run_id: str, phase: str, shared_context_id: str, task: str,
                  prior_positions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", shared_context_id or ""):
        raise ValueError("shared_context_id must be the pack's sha256 hex")
    if not (task or "").strip():
        raise ValueError("task is required")
    return {"run_id": run_id, "phase": phase, "shared_context_id": shared_context_id,
            "prior_positions": list(prior_positions or []), "task": task.strip(),
            "output_schema": SCHEMA_ID}


def _strs(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def validate_position(obj: Any, *, officer: str, pack_fact_ids: set[str] | None) -> list[str]:
    """Every problem with a position, as short pt-BR-neutral messages (empty = valid).

    ``pack_fact_ids`` is the set of fact ids in the frozen pack; a citation to anything else is
    an error, because an uncited or mis-cited claim must never reach the brief."""
    errs: list[str] = []
    if not isinstance(obj, dict):
        return ["position must be a JSON object"]
    if obj.get("officer") != officer:
        errs.append(f"officer must be {officer!r}")
    diag = obj.get("diagnosis")
    if not isinstance(diag, str) or not diag.strip():
        errs.append("diagnosis must be a non-empty string")
    elif len(diag) > MAX_TEXT:
        errs.append(f"diagnosis longer than {MAX_TEXT} characters")
    if not _strs(obj.get("constraints", [])):
        errs.append("constraints must be a list of strings")
    opts = obj.get("options", [])
    if not isinstance(opts, list) or len(opts) > MAX_OPTIONS:
        errs.append(f"options must be a list of at most {MAX_OPTIONS}")
        opts = []
    seen: set[str] = set()
    for i, o in enumerate(opts):
        where = f"options[{i}]"
        if not isinstance(o, dict):
            errs.append(f"{where} must be an object")
            continue
        oid = o.get("id")
        if not isinstance(oid, str) or not oid.strip():
            errs.append(f"{where}.id is required")
        elif oid in seen:
            errs.append(f"{where}.id {oid!r} is repeated")
        else:
            seen.add(oid)
        if not isinstance(o.get("description"), str) or not o["description"].strip():
            errs.append(f"{where}.description is required")
        if o.get("customer_impact") not in IMPACT_BANDS:
            errs.append(f"{where}.customer_impact must be one of {', '.join(IMPACT_BANDS)}")
        if not isinstance(o.get("capital_liquidity_effect", ""), str):
            errs.append(f"{where}.capital_liquidity_effect must be a string")
        if not _strs(o.get("red_lines", [])):
            errs.append(f"{where}.red_lines must be a list of strings")
    cites = obj.get("citations")
    if not isinstance(cites, list) or not cites:
        errs.append("citations must list at least one pack fact")
        cites = []
    for i, c in enumerate(cites):
        fid = c.get("pack_fact_id") if isinstance(c, dict) else None
        if not isinstance(fid, str) or not fid:
            errs.append(f"citations[{i}].pack_fact_id is required")
        elif pack_fact_ids is not None and fid not in pack_fact_ids:
            errs.append(f"citations[{i}].pack_fact_id {fid!r} is not in the pack")
    if not isinstance(obj.get("tool_call_summary", []), list):
        errs.append("tool_call_summary must be a list")
    return errs


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_position(text: str | None) -> Any:
    """The JSON object in a model reply (bare, or inside a ```json fence). None if absent."""
    if not text:
        return None
    m = _FENCE.search(text)
    body = m.group(1) if m else text
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(body[start:end + 1])
    except ValueError:
        return None


_CONTRACT = """Responda SOMENTE com um objeto JSON no esquema board/v1/position:
{"officer": "%(officer)s", "diagnosis": "texto", "constraints": ["texto"],
 "options": [{"id": "opt-1", "description": "texto", "customer_impact": "low|medium|high",
              "capital_liquidity_effect": "texto", "red_lines": ["texto"]}],
 "citations": [{"pack_fact_id": "id de um fato do pacote", "source": "", "as_of": ""}],
 "tool_call_summary": []}
Regras: use apenas fatos do pacote de contexto; cite cada fato pelo pack_fact_id; no máximo
%(max_options)d opções; se o pacote não sustenta uma afirmação, não a faça. Dados públicos têm
defasagem: diga o trimestre/data (as_of) dos números que usar."""


def system_prompt(officer: str) -> str:
    persona = officers.brief_persona(officer) or ""
    return (persona + "\n\nVocê participa de um briefing de conselho (board brief). "
            "É um briefing externo (outside-in), nunca uma decisão de capital ou preço.").strip()


def user_prompt(officer: str, envelope: dict[str, Any]) -> str:
    prior = envelope.get("prior_positions") or []
    parts = [f"Fase: {envelope['phase']}", f"Tarefa: {envelope['task']}"]
    if prior:
        parts.append("Posições anteriores:\n" + json.dumps(prior, ensure_ascii=False))
    parts.append(_CONTRACT % {"officer": officer, "max_options": MAX_OPTIONS})
    return "\n\n".join(parts)


def ask_officer(officer: str, envelope: dict[str, Any], *, call: Callable[[str, str], str | None],
                pack_fact_ids: set[str] | None) -> dict[str, Any]:
    """One officer turn with at most one re-prompt.

    ``call(system, prompt)`` returns the model text (the caller binds the pack as ``context``,
    the tenant's model target and the usage meter). The result always has ``officer``,
    ``phase``, ``status`` and ``attempts``; ``position`` when ok, ``errors`` otherwise."""
    if officer not in BOARD_OFFICERS:
        raise ValueError(f"unknown board officer {officer!r}")
    system, prompt = system_prompt(officer), user_prompt(officer, envelope)
    base = {"officer": officer, "phase": envelope["phase"], "run_id": envelope["run_id"]}
    errs: list[str] = []
    for attempt in (1, 2):
        text = call(system, prompt)
        if text is None:
            return {**base, "status": STATUS_NO_ANSWER, "attempts": attempt,
                    "errors": ["model returned no answer"]}
        obj = parse_position(text)
        errs = ["reply has no JSON object"] if obj is None else validate_position(
            obj, officer=officer, pack_fact_ids=pack_fact_ids)
        if not errs:
            return {**base, "status": STATUS_OK, "attempts": attempt, "position": obj}
        prompt = (user_prompt(officer, envelope) + "\n\nSua resposta anterior foi rejeitada: "
                  + "; ".join(errs[:8]) + ". Corrija e responda só com o JSON.")
    return {**base, "status": STATUS_INVALID, "attempts": 2, "errors": errs}
