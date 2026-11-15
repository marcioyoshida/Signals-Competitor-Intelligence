"""#338 board brief dry run: the real runner (src/board) on a fixed synthetic pack, with the
model call going to the local ``claude`` CLI (Sonnet, funded by the owner's Claude
subscription). No Bedrock spend, no AWS writes, no decision recorded.

The deployed state machine calls ``bedrock_llm.converse``; here only that call is swapped.
Contracts, validation, the challenge map, the token cap and the cited-claims filter are the
same code. Calls run one at a time: the CLI shares the plan's rate limit with the owner's own
interactive sessions.

Credentials: the CLI's own login on this machine. No token is read, written or printed.

    .venv/bin/python scripts/board_dry_run.py                      # scenario downgrade-v1
    .venv/bin/python scripts/board_dry_run.py --model sonnet --cap 400000
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.board import gate, pack as packmod, runner  # noqa: E402

SCENARIO = ROOT / "eval" / "board" / "scenario-downgrade-v1.json"
OUT_DIR = ROOT / "eval" / "board" / "runs"
TASK = ("O Banco Aurora (fictício) teve o rating rebaixado de AA para A+ com perspectiva negativa. "
        "Que opções o conselho deve considerar nos próximos 90 dias para funding, preço de crédito "
        "e relacionamento com clientes, e quais gatilhos monitorar?")
CALL_TIMEOUT_S = 300
CLI_CWD = "/var/tmp/board/cli"


def cli_call(model: str, log: list[dict[str, Any]], ctx: str):
    def call(officer: str, phase: str, system: str, prompt: str) -> str | None:
        # Same shape as converse(context=…): the pack first, then the question.
        body = ctx + "\n\n" + prompt
        argv = ["claude", "-p", "--output-format", "json", "--tools", "",
                "--system-prompt", system, "--model", model]
        t0 = time.monotonic()
        try:
            r = subprocess.run(argv, input=body, capture_output=True, text=True, timeout=CALL_TIMEOUT_S,
                               cwd=CLI_CWD)  # neutral dir: no project CLAUDE.md in the context
        except subprocess.TimeoutExpired:
            log.append({"officer": officer, "phase": phase, "error": "timeout"})
            return None
        try:
            out = json.loads(r.stdout or "{}")
        except ValueError:
            out = {}
        usage = out.get("usage") or {}
        entry = {"officer": officer, "phase": phase, "s": round(time.monotonic() - t0, 1),
                 # The CLI itself reads ~79k cached tokens of its own context on EVERY call
                 # (measured 2026-11-15 on a one-word prompt). That is the harness, not the
                 # run: count it as cache_read, apart from the cap, as converse does.
                 "input_tokens": int(usage.get("input_tokens") or 0)
                 + int(usage.get("cache_creation_input_tokens") or 0),
                 "cache_read": int(usage.get("cache_read_input_tokens") or 0),
                 "output_tokens": int(usage.get("output_tokens") or 0),
                 "is_error": bool(out.get("is_error")) or r.returncode != 0}
        log.append(entry)
        if entry["is_error"]:
            entry["error"] = str(out.get("result") or "")[:200]
            return None
        call.meter.add(entry)
        return str(out.get("result") or "") or None
    return call


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--cap", type=int, default=gate.DEFAULT_TOKEN_CAP)
    ap.add_argument("--scenario", type=Path, default=SCENARIO)
    a = ap.parse_args()

    sc = json.loads(a.scenario.read_text(encoding="utf-8"))
    if "feed" in sc:   # #386: a synthetic FEED through the real pack builder + proximity
        p = packmod.seal({**packmod.build_pack(sc["feed"], sc["focal"], subject=sc.get("subject")),
                          "scenario": sc["scenario"]})
        task = sc.get("task") or TASK
    else:              # v1: a hand-written pack (no focal/proximity)
        p, task = packmod.seal(sc), TASK
    ctx = packmod.context_text(p)
    log: list[dict[str, Any]] = []
    call = cli_call(a.model, log, ctx)
    call.meter = gate.Meter(cap=a.cap)
    runner.MAX_WORKERS = 1  # sequential: shared subscription rate limit
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%MZ")
    run_id = f"dry-{p['scenario']}-{stamp}"
    print(f"pack {p['sha256'][:12]} · {len(p['facts'])} facts · model {a.model} · cap {a.cap}")
    focal_kw = {}
    if p.get("focal"):
        focal_kw = dict(focal=p["focal"], subject=p.get("subject"), proximity_of=packmod.proximity_of(p),
                        fact_entity={f["id"]: f.get("entity") for f in p["facts"]},
                        proximity_score={x["peer"]: x["score"] for x in p.get("proximity") or []})
    result = runner.run_brief(run_id=run_id, pack_sha=p["sha256"], pack_ids=packmod.fact_ids(p),
                              task=task, call=call, stop=call.meter.stop, **focal_kw,
                              timebox_s={"diagnosis": 1500, "challenge": 1000,
                                         "quantify": 1000, "synthesize": 400})
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{run_id}.json"
    out.write_text(json.dumps({"model": a.model, "provider": "claude-cli (subscription)",
                               "scenario": p["scenario"], "pack_sha256": p["sha256"],
                               "usage": call.meter.summary(), "calls": log, "result": result,
                               "focal": p.get("focal"), "subject": p.get("subject"),
                               "proximity": p.get("proximity")},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"status {result['status']} · {call.meter.summary()} · gaps {len(result['gaps'])} · "
          f"dropped {len(result['dropped'])} · {result['elapsed_s']} s")
    print(f"wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
