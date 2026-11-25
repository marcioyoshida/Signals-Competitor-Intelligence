"""Board eval scoring and release gate (#340, ADR 031 Evaluation harness).

Scores one board run (a dry-run file from ``scripts/board_dry_run.py`` or a live run item
from OncaBoardRuns) against the pack it ran on:

- **citation validity** — every citation, in every officer position and in the synthesis,
  resolves to a fact of the frozen pack; and the numbers the brief states appear in the pack;
- **regulatory completeness** — a fixed checklist (capital, liquidity, credit, conduct,
  disclosure, supervisor), plus the episode's curated rubric for historic episodes;
- **dissent preservation** — each challenge reply reaches the rendered board pack verbatim;
- **option coherence** — 2–3 distinct options, each traced to an officer's own option, with a
  capital/liquidity effect and a customer-impact band; for episodes, how many of the board's
  actual (non-routine) decision categories an option covers, and whether a visible pre-event
  signal was flagged;
- **cost** — calls and tokens (the CLI's own cached harness context is reported apart);
- **latency** — run time, slowest call, errors and timeouts.

``gate`` applies ``config/board_eval_gate.json``: ``hard`` checks fail a run; ``soft`` checks
are reported only (eight episodes cannot calibrate them). Pure functions: no model calls, no
AWS. Persisting a score is ``store.put_eval``.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.board import artifact
from src.board import pack as packmod

ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = ROOT / "config" / "board_eval_gate.json"
ROUTINE = {"accounts", "other"}


EVAL_DIR = ROOT / "eval" / "board"
SYNTHETIC_TASK = ("O Banco Aurora (fictício) teve o rating rebaixado de AA para A+ com perspectiva negativa. "
                  "Que opções o conselho deve considerar nos próximos 90 dias para funding, preço de crédito "
                  "e relacionamento com clientes, e quais gatilhos monitorar?")


def scenario_pack(sc: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """(sealed pack, task) for a scenario file: a feed (synthetic v2 or a historic episode)
    goes through the real pack builder; v1 is a hand-written pack."""
    if "feed" in sc:
        p = packmod.seal({**packmod.build_pack(sc["feed"], sc["focal"], subject=sc.get("subject")),
                          "scenario": sc["scenario"]})
        return p, str(sc.get("task") or SYNTHETIC_TASK)
    return packmod.seal(dict(sc)), SYNTHETIC_TASK


def scenario_files() -> dict[str, Path]:
    """{scenario id: file} for the synthetic scenarios and the built historic episodes."""
    out = {}
    for f in sorted(EVAL_DIR.glob("scenario-*.json")) + sorted((EVAL_DIR / "episodes").glob("*-*.json")):
        if f.name == "episodes.json":
            continue
        sc = json.loads(f.read_text(encoding="utf-8"))
        if sc.get("scenario"):
            out[sc["scenario"]] = f
    return out


def required_set() -> list[str]:
    """What the release gate needs: every synthetic scenario and every usable episode."""
    req = []
    for sid, f in scenario_files().items():
        sc = json.loads(f.read_text(encoding="utf-8"))
        if sc.get("schema") != "board/v1/episode" or sc.get("usable"):
            req.append(sid)
    return req


def load_gate(path: Path = GATE_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def fold(s: Any) -> str:
    """Lower case, no accents, single spaces: what keyword matching compares."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode("ascii")
    return " ".join(s.lower().split())


def hits(text: str, any_of: Iterable[str]) -> list[str]:
    """The keywords of ``any_of`` found in ``text`` (already folded), word-start anchored."""
    return [k for k in any_of if re.search(r"(?<![a-z0-9])" + re.escape(fold(k)), text)]


# ---- numbers ------------------------------------------------------------------------------

# A stated figure: has a decimal part, or carries a unit (%, p.p., bi, mi, bps). Bare integers
# (horizons, counts, years) are not checked.
_NUM = re.compile(r"(?<![\w.,])(-?\d{1,3}(?:\.\d{3})+(?:,\d+)?|-?\d+(?:[.,]\d+)?)\s*"
                  r"(%|p\.?\s?p\.?|pp\b|bps?\b|pb\b|bi\b|bilh|mi\b|milh|x\b)?", re.I)


def _value(tok: str) -> float | None:
    t = tok
    if re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+(?:,\d+)?", t):
        t = t.replace(".", "")
    t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def figures(text: str) -> list[tuple[str, float, int]]:
    """``(as written, value, decimals)`` for each stated figure in ``text``."""
    out = []
    for m in _NUM.finditer(text or ""):
        tok, unit = m.group(1), m.group(2)
        has_dec = bool(re.search(r"[.,]\d+$", tok)) and not re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", tok)
        if not (has_dec or unit):
            continue
        v = _value(tok)
        if v is None:
            continue
        dec = len(re.split(r"[.,]", tok)[-1]) if has_dec else 0
        out.append((m.group(0).strip(), v, dec))
    return out


def _pack_values(texts: Iterable[str]) -> list[float]:
    vals = []
    for t in texts:
        for m in re.finditer(r"-?\d{1,3}(?:\.\d{3})+(?:,\d+)?|-?\d+(?:[.,]\d+)?", t or ""):
            v = _value(m.group(0))
            if v is not None:
                vals.append(v)
    return vals


def grounded(value: float, dec: int, pack_vals: list[float]) -> bool:
    """A figure is grounded if a pack number equals it at the precision written (a brief may
    round 13.47 to 13,5), its absolute value included (a fall of -26 written as 26)."""
    tol = 0.5 * 10 ** -dec + 1e-9
    return any(abs(abs(p) - abs(value)) <= tol for p in pack_vals)


# ---- run views ----------------------------------------------------------------------------

def positions(result: Mapping[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """``{phase: {officer: position}}`` for the officer replies that validated."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for ph, by in (result.get("phases") or {}).items():
        for off, r in (by or {}).items():
            if isinstance(r, Mapping) and r.get("status") == "ok" and isinstance(r.get("position"), Mapping):
                out.setdefault(ph, {})[off] = dict(r["position"])
    return out


def brief_text(result: Mapping[str, Any]) -> str:
    """What a board member reads, without the fact appendix: synthesis + dissents."""
    syn = result.get("synthesis") or {}
    parts = [syn.get("summary"), syn.get("residual_regulatory_risk")]
    for o in syn.get("options") or []:
        parts += [o.get("description"), o.get("capital_liquidity_effect")]
    parts += [t.get("text") for t in syn.get("monitoring_triggers") or []]
    for d in result.get("dissents") or []:
        parts += [d.get("text")] + list(d.get("red_lines") or [])
    return "\n".join(str(p) for p in parts if p)


def synthesis_text(result: Mapping[str, Any]) -> str:
    syn = result.get("synthesis") or {}
    parts = [syn.get("summary"), syn.get("residual_regulatory_risk")]
    for o in syn.get("options") or []:
        parts += [o.get("description"), o.get("capital_liquidity_effect")]
    parts += [t.get("text") for t in syn.get("monitoring_triggers") or []]
    return "\n".join(str(p) for p in parts if p)


# ---- scores -------------------------------------------------------------------------------

def score_citations(result: Mapping[str, Any], pack: Mapping[str, Any], task: str = "") -> dict[str, Any]:
    ids = {f["id"] for f in pack.get("facts") or []}
    total, bad = 0, []
    for ph, by in positions(result).items():
        for off, pos in by.items():
            for c in pos.get("citations") or []:
                fid = c.get("pack_fact_id") if isinstance(c, Mapping) else c
                total += 1
                if fid not in ids:
                    bad.append(f"{ph}/{off}:{fid}")
    syn = result.get("synthesis") or {}
    for item in list(syn.get("options") or []) + list(syn.get("monitoring_triggers") or []):
        for fid in item.get("citations") or []:
            total += 1
            if fid not in ids:
                bad.append(f"synthesis:{fid}")
    vals = _pack_values([f.get("text") for f in pack.get("facts") or []] + [task])
    figs = figures(synthesis_text(result))
    ungrounded = [w for w, v, d in figs if not grounded(v, d, vals)]
    return {"citations": total, "invalid": bad,
            "citation_validity": round((total - len(bad)) / total, 4) if total else 0.0,
            "figures": len(figs), "ungrounded_figures": ungrounded,
            "numbers_grounded": round((len(figs) - len(ungrounded)) / len(figs), 4) if figs else 1.0,
            "dropped_by_guard": len(result.get("dropped") or [])}


def score_checklist(text: str, items: list[Mapping[str, Any]]) -> dict[str, Any]:
    t = fold(text)
    found = {i["id"]: hits(t, i["any_of"]) for i in items}
    covered = [k for k, v in found.items() if v]
    return {"covered": covered, "missing": [k for k in found if k not in covered],
            "n": len(covered), "of": len(items), "coverage": round(len(covered) / len(items), 4) if items else 0.0}


def score_dissent(result: Mapping[str, Any]) -> dict[str, Any]:
    """Each validated challenge reply must be a dissent, copied verbatim, and printed verbatim
    in the rendered board pack (``artifact.render_markdown``)."""
    chal = positions(result).get("challenge") or {}
    by_officer = {d.get("officer"): d for d in result.get("dissents") or []}
    md = artifact.render_markdown({**result, "status": result.get("status")})
    problems = []
    for off, pos in chal.items():
        d = by_officer.get(off)
        if d is None:
            problems.append(f"{off}: challenge reply has no dissent")
            continue
        if d.get("text") != pos.get("diagnosis"):
            problems.append(f"{off}: dissent text differs from the challenge reply")
        if str(pos.get("diagnosis") or "").strip() not in md:
            problems.append(f"{off}: dissent not verbatim in the board pack")
        for rl in (rl for o in pos.get("options") or [] for rl in o.get("red_lines") or []):
            if rl not in md:
                problems.append(f"{off}: red line missing from the board pack")
                break
    contested = sum(1 for o in (result.get("synthesis") or {}).get("options") or [] if o.get("contested_by"))
    n = len(chal)
    return {"challenges": n, "problems": problems, "contested_options": contested,
            "dissent_preservation": round((n - len({p.split(':')[0] for p in problems})) / n, 4) if n else 0.0}


def _tokens(s: Any) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", fold(s)) if len(w) > 3}


def jaccard(a: Any, b: Any) -> float:
    x, y = _tokens(a), _tokens(b)
    return len(x & y) / len(x | y) if x | y else 0.0


def score_options(result: Mapping[str, Any]) -> dict[str, Any]:
    syn = result.get("synthesis") or {}
    opts = list(syn.get("options") or [])
    traced = [o for o in opts if o.get("derived_from")]
    with_effect = [o for o in opts if str(o.get("capital_liquidity_effect") or "").strip()]
    banded = [o for o in opts if o.get("customer_impact") in ("low", "medium", "high")]
    sim = max((jaccard(a.get("description"), b.get("description"))
               for i, a in enumerate(opts) for b in opts[i + 1:]), default=0.0)
    return {"options": len(opts), "triggers": len(syn.get("monitoring_triggers") or []),
            "option_traced": round(len(traced) / len(opts), 4) if opts else 0.0,
            "with_capital_liquidity_effect": len(with_effect), "with_impact_band": len(banded),
            "max_jaccard": round(sim, 4),
            "impact_bands": sorted({str(o.get("customer_impact")) for o in opts})}


def score_answer_key(result: Mapping[str, Any], episode: Mapping[str, Any], gate_cfg: Mapping[str, Any]
                     ) -> dict[str, Any]:
    """Historic episodes only: the brief against what actually happened."""
    key = episode.get("answer_key") or {}
    text = fold(brief_text(result))
    opt_text = fold("\n".join(f"{o.get('description')} {o.get('capital_liquidity_effect')}"
                              for o in (result.get("synthesis") or {}).get("options") or []))
    out: dict[str, Any] = {}
    rub = (episode.get("rubric") or {}).get("items") or []
    if rub:
        out["rubric"] = score_checklist(text, rub)
    cats = [c for c in (key.get("decision_categories") or {}) if c not in ROUTINE]
    kw = gate_cfg.get("decision_keywords") or {}
    if cats:
        matched = [c for c in cats if hits(opt_text, kw.get(c) or [])]
        out["answer_key"] = {"board_categories": cats, "matched": matched,
                             "alignment": round(len(matched) / len(cats), 4)}
    sig = key.get("pre_event_signal") or {}
    if sig.get("visible"):
        skw = gate_cfg.get("signal_keywords") or {}
        basis = {b: hits(text, skw.get(b) or []) for b in sig.get("basis") or []}
        out["signal"] = {"basis": list(basis), "caught": all(basis.values()) if basis else False,
                         "matched": basis}
    return out


def score_cost(usage: Mapping[str, Any] | None, calls: list[Mapping[str, Any]] | None) -> dict[str, Any]:
    u = usage or {}
    tin, tout = int(u.get("tokens_in") or 0), int(u.get("tokens_out") or 0)
    by_phase: dict[str, int] = {}
    for c in calls or []:
        ph = str(c.get("phase"))
        by_phase[ph] = by_phase.get(ph, 0) + int(c.get("input_tokens") or 0) + int(c.get("output_tokens") or 0)
    return {"calls": int(u.get("calls") or len(calls or [])), "tokens_in": tin, "tokens_out": tout,
            "tokens": tin + tout, "cache_read": int(u.get("cache_read") or 0),
            "token_cap": u.get("token_cap"), "tokens_by_phase": by_phase}


def score_latency(result: Mapping[str, Any], calls: list[Mapping[str, Any]] | None) -> dict[str, Any]:
    cs = list(calls or [])
    secs = [float(c.get("s") or 0) for c in cs if c.get("s") is not None]
    errors = [f"{c.get('phase')}/{c.get('officer')}: {c.get('error') or 'error'}"
              for c in cs if c.get("error") or c.get("is_error")]
    timeouts = [g for g in result.get("gaps") or [] if "prazo" in str((g or {}).get("gap") if isinstance(g, Mapping) else g)]
    return {"elapsed_s": result.get("elapsed_s"), "slowest_call_s": max(secs) if secs else None,
            "mean_call_s": round(sum(secs) / len(secs), 1) if secs else None,
            "call_errors": errors, "timeouts": len(timeouts)}


def score(run: Mapping[str, Any], pack: Mapping[str, Any], *, episode: Mapping[str, Any] | None = None,
          gate_cfg: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """All scores for one run. ``run`` is a dry-run file (``result``, ``usage``, ``calls``)."""
    g = gate_cfg or load_gate()
    result = run.get("result") or run
    task = str(result.get("task") or "")
    s = {"run_id": result.get("run_id"), "scenario": run.get("scenario") or pack.get("scenario"),
         "model": run.get("model"), "provider": run.get("provider"), "status": result.get("status"),
         "pack_sha256": pack.get("sha256"),
         "pack_matches": (run.get("pack_sha256") or result.get("shared_context_id")) == pack.get("sha256"),
         "gaps": [g_.get("gap") if isinstance(g_, Mapping) else g_ for g_ in result.get("gaps") or []],
         "citations": score_citations(result, pack, task),
         "regulatory": score_checklist(brief_text(result), g.get("regulatory_checklist") or []),
         "dissent": score_dissent(result), "options": score_options(result),
         "cost": score_cost(run.get("usage"), run.get("calls")),
         "latency": score_latency(result, run.get("calls"))}
    if episode:
        s["episode"] = score_answer_key(result, episode, g)
    s["gate"] = gate(s, g)
    return s


def gate(s: Mapping[str, Any], g: Mapping[str, Any]) -> dict[str, Any]:
    h, soft = g.get("hard") or {}, g.get("soft") or {}
    fails: list[str] = []
    warns: list[str] = []

    def need(ok: bool, msg: str, into: list[str] = fails) -> None:
        if not ok:
            into.append(msg)
    need(s["status"] == h.get("status", "complete"), f"status {s['status']}")
    need(bool(s.get("pack_matches")), "pack sha differs from the run's: citations checked against a rebuilt pack")
    c, d, o = s["citations"], s["dissent"], s["options"]
    need(c["citation_validity"] >= h["citation_validity_min"], f"citation validity {c['citation_validity']}")
    need(c["numbers_grounded"] >= h["numbers_grounded_min"],
         f"numbers grounded {c['numbers_grounded']} ({', '.join(c['ungrounded_figures'][:5])})")
    need(d["dissent_preservation"] >= h["dissent_preservation_min"], f"dissent preservation {d['dissent_preservation']}")
    need(h["options_min"] <= o["options"] <= h["options_max"], f"{o['options']} options")
    need(o["option_traced"] >= h["option_traced_min"], f"options traced {o['option_traced']}")
    need(o["max_jaccard"] <= h["option_distinct_max_jaccard"], f"options overlap (jaccard {o['max_jaccard']})")
    need(o["triggers"] >= h["triggers_min"], f"{o['triggers']} monitoring triggers")
    need(s["regulatory"]["n"] >= h["regulatory_min"],
         f"regulatory checklist {s['regulatory']['n']}/{s['regulatory']['of']} (missing {', '.join(s['regulatory']['missing'])})")
    need(s["cost"]["tokens"] <= h["max_tokens"], f"{s['cost']['tokens']} tokens")
    el = s["latency"]["elapsed_s"]
    need(el is not None and el <= h["max_elapsed_s"], f"elapsed {el} s")
    need(len(s["latency"]["call_errors"]) <= h["max_call_errors"], f"{len(s['latency']['call_errors'])} call errors")
    ep = s.get("episode") or {}
    if "rubric" in ep:
        need(ep["rubric"]["coverage"] >= soft["rubric_coverage_min"],
             f"rubric {ep['rubric']['n']}/{ep['rubric']['of']} (missing {', '.join(ep['rubric']['missing'])})", warns)
    if "answer_key" in ep:
        need(ep["answer_key"]["alignment"] >= soft["answer_key_alignment_min"],
             f"answer key {ep['answer_key']['matched']} of {ep['answer_key']['board_categories']}", warns)
    if "signal" in ep and soft.get("signal_caught"):
        need(ep["signal"]["caught"], f"pre-event signal ({', '.join(ep['signal']['basis'])}) not flagged", warns)
    return {"version": g.get("version"), "passed": not fails, "fails": fails, "warnings": warns}


def release_gate(scores: list[Mapping[str, Any]], required: Iterable[str]) -> dict[str, Any]:
    """The set passes when every required scenario has a scored run and its latest run passes."""
    latest: dict[str, Mapping[str, Any]] = {}
    for s in scores:
        k = str(s.get("scenario"))
        if k not in latest or str(s.get("run_id")) > str(latest[k].get("run_id")):
            latest[k] = s
    req = list(required)
    missing = [r for r in req if r not in latest]
    failed = [r for r in req if r in latest and not latest[r]["gate"]["passed"]]
    return {"passed": not missing and not failed, "required": req, "missing": missing, "failed": failed,
            "warnings": {r: latest[r]["gate"]["warnings"] for r in req if r in latest and latest[r]["gate"]["warnings"]}}
