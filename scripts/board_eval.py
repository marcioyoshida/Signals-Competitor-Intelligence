#!/usr/bin/env python3
"""#340 board eval: score board runs and apply the release gate. No model calls.

    .venv/bin/python scripts/board_eval.py                       # score every run in eval/board/runs
    .venv/bin/python scripts/board_eval.py RUN.json [RUN.json …]
    .venv/bin/python scripts/board_eval.py --gate                 # exit 1 unless the set passes
    AWS_PROFILE=my2027 .venv/bin/python scripts/board_eval.py --record   # + OncaBoardRuns (_eval)

A run comes from ``scripts/board_dry_run.py --scenario <file>`` (that is the step that costs
model calls). The pack is rebuilt from the scenario file and must have the run's sha256, so
every citation is checked against the very pack the officers read. Scores go to
``eval/board/scores/<run_id>.json`` and ``eval/board/scores/summary.json``.

The release gate (``--gate``) needs a passing latest run for every synthetic scenario and
every usable historic episode. Thresholds: ``config/board_eval_gate.json``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.board import evaluate as ev  # noqa: E402

RUNS = ROOT / "eval" / "board" / "runs"
SCORES = ROOT / "eval" / "board" / "scores"
STACK = "OncaBoardStack"


def _table():
    import boto3
    outs = boto3.client("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0].get("Outputs") or []
    name = next(o["OutputValue"] for o in outs if o["OutputKey"] == "BoardRunsTable")
    return boto3.resource("dynamodb").Table(name)


def score_file(path: Path, files: dict[str, Path], g: dict) -> dict:
    run = json.loads(path.read_text(encoding="utf-8"))
    sid = run.get("scenario")
    if sid not in files:
        raise SystemExit(f"{path.name}: no scenario file for {sid!r}")
    sc = json.loads(files[sid].read_text(encoding="utf-8"))
    pack, _ = ev.scenario_pack(sc)
    episode = sc if sc.get("schema") == "board/v1/episode" else None
    return ev.score(run, pack, episode=episode, gate_cfg=g)


def line(s: dict) -> str:
    c, o, d = s["citations"], s["options"], s["dissent"]
    ep = s.get("episode") or {}
    extra = ""
    if "rubric" in ep:
        extra += f" · rubric {ep['rubric']['n']}/{ep['rubric']['of']}"
    if "answer_key" in ep:
        extra += f" · key {len(ep['answer_key']['matched'])}/{len(ep['answer_key']['board_categories'])}"
    if "signal" in ep:
        extra += f" · signal {'yes' if ep['signal']['caught'] else 'NO'}"
    verdict = "PASS" if s["gate"]["passed"] else "FAIL"
    return (f"{verdict} {s['run_id']}: cites {c['citation_validity']:.0%} of {c['citations']}, "
            f"figures {c['numbers_grounded']:.0%} of {c['figures']} · reg {s['regulatory']['n']}/{s['regulatory']['of']} "
            f"· dissent {d['dissent_preservation']:.0%} · {o['options']} options (overlap {o['max_jaccard']:.2f}) "
            f"· {s['cost']['tokens']} tok/{s['cost']['calls']} calls · {s['latency']['elapsed_s']} s{extra}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("runs", nargs="*", type=Path)
    ap.add_argument("--gate", action="store_true", help="exit 1 unless the release gate passes")
    ap.add_argument("--record", action="store_true", help="write each score to OncaBoardRuns (tenant _eval)")
    a = ap.parse_args()

    g = ev.load_gate()
    files = ev.scenario_files()
    paths = a.runs or sorted(RUNS.glob("*.json"))
    SCORES.mkdir(parents=True, exist_ok=True)
    scores = []
    for p in paths:
        s = score_file(p, files, g)
        scores.append(s)
        (SCORES / f"{s['run_id']}.json").write_text(json.dumps(s, ensure_ascii=False, indent=1) + "\n",
                                                   encoding="utf-8")
        print(line(s))
        for f in s["gate"]["fails"]:
            print(f"    fail: {f}")
        for w in s["gate"]["warnings"]:
            print(f"    warn: {w}")
    rg = ev.release_gate(scores, ev.required_set())
    (SCORES / "summary.json").write_text(json.dumps(
        {"schema": "board/v1/eval-summary", "gate_version": g["version"], "release_gate": rg,
         "runs": [{"run_id": s["run_id"], "scenario": s["scenario"], "model": s["model"],
                   "passed": s["gate"]["passed"], "fails": s["gate"]["fails"],
                   "warnings": s["gate"]["warnings"]} for s in scores]},
        ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if a.record:
        from src.board import store
        t = _table()
        n = sum(store.put_eval(t, run_id=s["run_id"], scores=s) for s in scores)
        print(f"recorded {n} new score(s) in OncaBoardRuns (tenant {store.EVAL_TENANT})")
    print(f"release gate v{g['version']}: {'PASS' if rg['passed'] else 'FAIL'}"
          + (f" · missing {len(rg['missing'])}: {', '.join(rg['missing'])}" if rg["missing"] else "")
          + (f" · failed: {', '.join(rg['failed'])}" if rg["failed"] else ""))
    return 0 if (rg["passed"] or not a.gate) else 1


if __name__ == "__main__":
    raise SystemExit(main())
