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
# Length limits (dry run 2026-11-15: ~3.5k output tokens per officer, a long and costly read).
# The prompt states the TARGET; validation enforces a looser BOUND, so only a real overrun
# costs a re-prompt.
DIAGNOSIS_TARGET, DIAGNOSIS_MAX = 900, 1500
DESCRIPTION_TARGET, DESCRIPTION_MAX = 400, 700
MAX_CONSTRAINTS, MAX_RED_LINES = 5, 4
MAX_TEXT = DIAGNOSIS_MAX

# A position is valid, or it is recorded as invalid after one re-prompt (ADR 031 §2).
STATUS_OK, STATUS_INVALID, STATUS_NO_ANSWER = "ok", "invalid", "no_answer"


def make_envelope(*, run_id: str, phase: str, shared_context_id: str, task: str,
                  prior_positions: list[dict[str, Any]] | None = None,
                  focal: str | None = None, subject: str | None = None) -> dict[str, Any]:
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", shared_context_id or ""):
        raise ValueError("shared_context_id must be the pack's sha256 hex")
    if not (task or "").strip():
        raise ValueError("task is required")
    env = {"run_id": run_id, "phase": phase, "shared_context_id": shared_context_id,
           "prior_positions": list(prior_positions or []), "task": task.strip(),
           "output_schema": SCHEMA_ID}
    if focal:  # #386: whose board decides, and what the question is about
        env["focal"], env["subject"] = focal, subject or focal
    return env


def _strs(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def validate_position(obj: Any, *, officer: str, pack_fact_ids: set[str] | None,
                      proximity_of: dict[str, str] | None = None) -> list[str]:
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
    elif len(obj.get("constraints", [])) > MAX_CONSTRAINTS:
        errs.append(f"at most {MAX_CONSTRAINTS} constraints")
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
        elif len(o["description"]) > DESCRIPTION_MAX:
            errs.append(f"{where}.description longer than {DESCRIPTION_MAX} characters")
        if o.get("customer_impact") not in IMPACT_BANDS:
            errs.append(f"{where}.customer_impact must be one of {', '.join(IMPACT_BANDS)}")
        if not isinstance(o.get("capital_liquidity_effect", ""), str):
            errs.append(f"{where}.capital_liquidity_effect must be a string")
        if not _strs(o.get("red_lines", [])):
            errs.append(f"{where}.red_lines must be a list of strings")
        elif len(o.get("red_lines", [])) > MAX_RED_LINES:
            errs.append(f"{where}: at most {MAX_RED_LINES} red_lines")
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
    # #386: a competitor's signal is weighted by its distance — citing a competitor's fact
    # requires citing that competitor's proximity fact too.
    if proximity_of and isinstance(obj.get("citations"), list):
        cited = {c.get("pack_fact_id") for c in obj["citations"] if isinstance(c, dict)}
        need = sorted({proximity_of[f] for f in cited if f in proximity_of} - cited)
        if need:
            errs.append("competitor facts cited without their proximity: also cite " + ", ".join(need))
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
defasagem: diga o trimestre/data (as_of) dos números que usar.
Tamanho: diagnosis com até %(diag)d caracteres; cada description com até %(desc)d; no máximo
%(cons)d constraints e %(red)d red_lines por opção. Seja direto: números e o fato citado, sem repetir
o pacote."""

_FOCAL = """Você assessora o conselho da instituição focal %(focal)s: toda posição responde "o que
isto significa para %(focal)s". Assunto da pergunta: %(subject)s. Sinais de concorrentes pesam pela
proximidade à focal (fatos f-proximity-*, de 0 a 1): ao usar um fato de um concorrente, cite também
o f-proximity desse concorrente e diga a distância; um concorrente distante pode ser citado, mas
pesa menos."""


# #340 eval 2026-11-25: 3 of 4 sovereign episodes named the downgrade and the "exposição soberana"
# but never the bank's own rating following the sovereign. Board-only (not in the officers.py
# persona, which the live Ask also uses) and task-agnostic: it acts only when the pack holds a
# sovereign rating action. It names a mechanism, not a fact, so the cite-only-pack-facts rule
# still governs every claim about the bank.
SOVEREIGN_RATING_RULE = (
    "Se o pacote trouxer uma ação de rating soberano (rebaixamento, perda do grau de investimento "
    "ou perspectiva negativa), trate também o efeito de segunda ordem: as agências costumam limitar "
    "o rating dos bancos domésticos ao teto soberano e rebaixá-los junto com o soberano, o que "
    "encarece e encurta a captação, sobretudo a externa. Isso é um mecanismo de análise, não um "
    "fato do pacote: apresente-o como risco ou gatilho a monitorar, citando o fato da ação "
    "soberana; não afirme que uma agência rebaixou ou vai rebaixar o banco, nem dê nota de rating "
    "do banco, salvo se o pacote trouxer esse fato.")


def system_prompt(officer: str) -> str:
    persona = officers.brief_persona(officer) or ""
    return (persona + "\n\nVocê participa de um briefing de conselho (board brief). "
            "É um briefing externo (outside-in), nunca uma decisão de capital ou preço.\n\n"
            + SOVEREIGN_RATING_RULE).strip()


def user_prompt(officer: str, envelope: dict[str, Any]) -> str:
    prior = envelope.get("prior_positions") or []
    parts = [f"Fase: {envelope['phase']}", f"Tarefa: {envelope['task']}"]
    if envelope.get("focal"):
        subj = envelope.get("subject") or envelope["focal"]
        parts.append(_FOCAL % {"focal": envelope["focal"],
                               "subject": subj if subj != envelope["focal"] else "a própria instituição focal"})
    if prior:
        parts.append("Posições anteriores:\n" + json.dumps(prior, ensure_ascii=False))
    parts.append(_CONTRACT % {"officer": officer, "max_options": MAX_OPTIONS,
                              "diag": DIAGNOSIS_TARGET, "desc": DESCRIPTION_TARGET,
                              "cons": MAX_CONSTRAINTS, "red": MAX_RED_LINES})
    return "\n\n".join(parts)


def ask_officer(officer: str, envelope: dict[str, Any], *, call: Callable[[str, str], str | None],
                pack_fact_ids: set[str] | None,
                proximity_of: dict[str, str] | None = None) -> dict[str, Any]:
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
            obj, officer=officer, pack_fact_ids=pack_fact_ids, proximity_of=proximity_of)
        if not errs:
            return {**base, "status": STATUS_OK, "attempts": attempt, "position": obj}
        prompt = (user_prompt(officer, envelope) + "\n\nSua resposta anterior foi rejeitada: "
                  + "; ".join(errs[:8]) + ". Corrija e responda só com o JSON.")
    return {**base, "status": STATUS_INVALID, "attempts": 2, "errors": errs}
