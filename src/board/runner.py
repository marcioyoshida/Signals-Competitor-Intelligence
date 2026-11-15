"""Phase runner (#326): diagnosis → challenge → quantify → synthesize over one frozen pack.

The model call is injected (``call(officer, phase, system, prompt) -> text | None``), so the
Step Functions tasks (#322), the dry run (#338) and the tests share this code. The caller binds
the pack as ``converse(context=…)``, the tenant's model target and the usage meter.

Rules (ADR 031, amended 2026-11-14):
- every phase has a time box; an officer that misses it is a visible gap, never a silence;
- a ``stop()`` hook (the #329 spend cap) ends the run between phases with partial results;
- dissents are copied **verbatim by code** from the challenge replies, never paraphrased;
- the synthesis keeps only options and triggers that cite pack facts (ADR-010 guard family).
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import time
from typing import Any, Callable

from src.board import contracts as c

Call = Callable[[str, str, str, str], "str | None"]

# Who challenges whom (one reply each). Regulatório + Compliance challenge Produto + Risco;
# Risco challenges Estratégia.
CHALLENGES: dict[str, tuple[str, ...]] = {"cro": ("cpo", "risk"), "cco": ("cpo", "risk"),
                                          "risk": ("cso",)}
QUANTIFY_TASKS = {
    "risk": "Quantifique: anexe as diferenças das métricas prudenciais públicas do alvo contra "
            "os pares (Basileia, alavancagem, LCR/NSFR, inadimplência), com trimestre e defasagem.",
    "cpo": "Quantifique: anexe a evidência de taxas, reclamações e reputação que sustenta ou "
           "contraria cada opção.",
    "cso": "Quantifique: ordene as opções por encaixe estratégico e diga por quê.",
}
DEFAULT_TIMEBOX_S = {"diagnosis": 240, "challenge": 180, "quantify": 180, "synthesize": 240}
MAX_WORKERS = 5

SYNTH_SYSTEM = (
    "Você é o secretário do conselho da Onça. Consolide as posições dos cinco oficiais em um "
    "briefing externo (outside-in), nunca uma decisão de capital ou preço. Não invente fatos: "
    "toda opção e todo gatilho precisa citar fatos do pacote pelo pack_fact_id.")
SYNTH_CONTRACT = """Responda SOMENTE com JSON:
{"summary": "texto curto", "options": [{"id": "opt-1", "description": "texto",
  "customer_impact": "low|medium|high", "capital_liquidity_effect": "texto",
  "supported_by": ["cso|risk|cco|cpo|cro"], "citations": ["pack_fact_id"]}],
 "residual_regulatory_risk": "texto", "monitoring_triggers": [{"text": "texto",
  "citations": ["pack_fact_id"]}]}
De 2 a 3 opções."""


def _gap(officer: str | None, phase: str, why: str) -> dict[str, Any]:
    return {"officer": officer, "phase": phase, "gap": why}


def _run_phase(phase: str, asks: dict[str, dict[str, Any]], *, call: Call, pack_ids: set[str],
               timebox_s: float, gaps: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Run ``{officer: envelope}`` in parallel under one time box."""
    out: dict[str, dict[str, Any]] = {}
    if not asks:
        return out
    pool = cf.ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(asks)))
    futs = {pool.submit(c.ask_officer, off, env,
                        call=lambda s, p, off=off: call(off, phase, s, p),
                        pack_fact_ids=pack_ids): off for off, env in asks.items()}
    try:
        for fut in cf.as_completed(futs, timeout=timebox_s):
            off = futs[fut]
            try:
                out[off] = fut.result()
            except Exception as exc:  # an officer crash is a gap, the run continues
                gaps.append(_gap(off, phase, f"erro: {type(exc).__name__}"))
    except cf.TimeoutError:
        pass
    for off in asks:
        if off not in out and not any(g["officer"] == off and g["phase"] == phase for g in gaps):
            gaps.append(_gap(off, phase, f"sem resposta no prazo ({int(timebox_s)} s)"))
    pool.shutdown(wait=False, cancel_futures=True)
    for off, r in out.items():
        if r["status"] != c.STATUS_OK:
            gaps.append(_gap(off, phase, f"{r['status']}: " + "; ".join(r.get("errors", [])[:3])))
    return out


def _ok(results: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {o: r["position"] for o, r in results.items() if r["status"] == c.STATUS_OK}


def validate_synthesis(obj: Any, pack_ids: set[str]) -> tuple[dict[str, Any] | None, list[str]]:
    """Keep only cited options/triggers. Returns (clean synthesis or None, dropped notes)."""
    if not isinstance(obj, dict):
        return None, ["síntese sem JSON"]
    dropped: list[str] = []
    opts = []
    for o in obj.get("options") or []:
        if not isinstance(o, dict) or not str(o.get("description") or "").strip():
            continue
        cites = [x for x in o.get("citations") or [] if x in pack_ids]
        if not cites:
            dropped.append(f"opção sem citação válida descartada: {str(o.get('description'))[:80]}")
            continue
        if o.get("customer_impact") not in c.IMPACT_BANDS:
            dropped.append(f"opção sem faixa de impacto ao cliente descartada: {o.get('id')}")
            continue
        sup = [s for s in o.get("supported_by") or [] if s in c.BOARD_OFFICERS]
        opts.append({**o, "citations": cites, "supported_by": sup})
    trig = []
    for t in obj.get("monitoring_triggers") or []:
        cites = [x for x in (t or {}).get("citations") or [] if x in pack_ids] if isinstance(t, dict) else []
        if cites and str(t.get("text") or "").strip():
            trig.append({"text": t["text"], "citations": cites})
        elif isinstance(t, dict):
            dropped.append(f"gatilho sem citação descartado: {str(t.get('text'))[:80]}")
    if len(opts) < 2:
        return None, dropped + [f"síntese com {len(opts)} opção(ões) citada(s); mínimo 2"]
    return {"summary": str(obj.get("summary") or "").strip(), "options": opts[:c.MAX_OPTIONS],
            "residual_regulatory_risk": str(obj.get("residual_regulatory_risk") or "").strip(),
            "monitoring_triggers": trig}, dropped


def run_brief(*, run_id: str, pack_sha: str, pack_ids: set[str], task: str, call: Call,
              stop: Callable[[], bool] = lambda: False,
              timebox_s: dict[str, float] | None = None) -> dict[str, Any]:
    """One board run. Always returns a result; ``status`` is ``complete`` or ``partial``."""
    tb = {**DEFAULT_TIMEBOX_S, **(timebox_s or {})}
    gaps: list[dict[str, Any]] = []
    phases: dict[str, dict[str, Any]] = {}
    t0 = time.monotonic()

    def env(phase: str, t: str, prior: list[dict[str, Any]]) -> dict[str, Any]:
        return c.make_envelope(run_id=run_id, phase=phase, shared_context_id=pack_sha, task=t,
                               prior_positions=prior)

    def stopped(before: str) -> bool:
        if stop():
            gaps.append(_gap(None, before, "limite de custo atingido: fases seguintes não executadas"))
            return True
        return False

    def result(status: str, synthesis: dict[str, Any] | None = None, dissents=None, dropped=None):
        return {"run_id": run_id, "shared_context_id": pack_sha, "task": task, "status": status,
                "phases": phases, "synthesis": synthesis, "dissents": dissents or [],
                "dropped": dropped or [], "gaps": gaps,
                "elapsed_s": round(time.monotonic() - t0, 1)}

    # 1. diagnosis — all five, in parallel
    if stopped("diagnosis"):
        return result("partial")
    phases["diagnosis"] = _run_phase(
        "diagnosis", {o: env("diagnosis", task, []) for o in c.BOARD_OFFICERS},
        call=call, pack_ids=pack_ids, timebox_s=tb["diagnosis"], gaps=gaps)
    diag = _ok(phases["diagnosis"])

    # 2. challenge — one reply per challenger, against the targets that answered
    if stopped("challenge"):
        return result("partial")
    asks = {}
    for ch, targets in CHALLENGES.items():
        prior = [diag[t] for t in targets if t in diag]
        if ch in diag and prior:
            names = ", ".join(t for t in targets if t in diag)
            asks[ch] = env("challenge", f"{task}\n\nConteste as opções de: {names}. Aponte linhas "
                                        "vermelhas, riscos e o que falta. Uma resposta.", prior)
    phases["challenge"] = _run_phase("challenge", asks, call=call, pack_ids=pack_ids,
                                     timebox_s=tb["challenge"], gaps=gaps)
    chal = _ok(phases["challenge"])
    dissents = [{"officer": o, "challenged": list(CHALLENGES[o]), "text": p["diagnosis"],
                 "red_lines": [rl for opt in p.get("options") or [] for rl in opt.get("red_lines") or []]}
                for o, p in chal.items()]

    # 3. quantify
    if stopped("quantify"):
        return result("partial", dissents=dissents)
    prior_all = list(diag.values()) + list(chal.values())
    phases["quantify"] = _run_phase(
        "quantify", {o: env("quantify", f"{task}\n\n{t}", prior_all)
                     for o, t in QUANTIFY_TASKS.items() if o in diag},
        call=call, pack_ids=pack_ids, timebox_s=tb["quantify"], gaps=gaps)
    quant = _ok(phases["quantify"])

    # 4. synthesize — one call; code keeps only cited claims
    if stopped("synthesize"):
        return result("partial", dissents=dissents)
    if not diag:
        gaps.append(_gap(None, "synthesize", "nenhum oficial respondeu ao diagnóstico"))
        return result("partial", dissents=dissents)
    positions = {"diagnosis": diag, "challenge": chal, "quantify": quant}
    prompt = (f"Tarefa: {task}\n\nPosições:\n{json.dumps(positions, ensure_ascii=False)}\n\n"
              + SYNTH_CONTRACT)
    synth, dropped = None, []
    pool = cf.ThreadPoolExecutor(max_workers=1)  # not a `with`: its exit would wait out a hang
    fut = pool.submit(call, "secretary", "synthesize", SYNTH_SYSTEM, prompt)
    try:
        text = fut.result(timeout=tb["synthesize"])
    except cf.TimeoutError:
        text = None
        gaps.append(_gap(None, "synthesize", f"sem resposta no prazo ({int(tb['synthesize'])} s)"))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if text is not None:
        synth, dropped = validate_synthesis(c.parse_position(text), pack_ids)
        if synth is None:
            gaps.append(_gap(None, "synthesize", "; ".join(dropped[-1:]) or "síntese inválida"))
    phases["synthesize"] = {"secretary": {"status": c.STATUS_OK if synth else c.STATUS_INVALID}}
    complete = synth is not None and not gaps
    return result("complete" if complete else "partial", synth, dissents, dropped)
