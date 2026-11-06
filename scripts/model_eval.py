"""#352 (BYOK story 7): measure a client model before it may be connected.

Runs the Ask regression (``scripts/ask_regression.py``: 10 in-scope questions + the guardrail
probes) through the deployed OncaAgent Lambda on a QA tenant whose OWN model is connected, then
scores the rows against the bar in ``config/model_eval_bar.json``, which is fixed before the run.

  python -m scripts.model_eval --tenant qa-byok-eval [--dry]

- It refuses to run unless the tenant has a client model connected (``model_status`` via the act
  Lambda), so its rows can never mix with hosted Nova rows. The artifact names that model.
- The artifact goes to ``eval/model-profiles/<model>-<date>.json``. A model's ``MODELS`` entry
  points at it (``profile``) once it passes.
- Every question is one paid call on the CLIENT's account (plus Onça's guardrail). The Claude Max
  subscription does not fund API calls; run it only with a real key from the owner or a pilot.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BAR = ROOT / "config" / "model_eval_bar.json"
OUT = ROOT / "eval" / "model-profiles"


def load_bar(path: Path = BAR) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def score(rows: list[dict[str, Any]], bar: dict[str, Any]) -> dict[str, Any]:
    """Pure: the verdict for ``rows`` (``kind``, ``reason``, ``grounded``, ``citations``,
    ``model_error``) against ``bar``. Every failed criterion is listed."""
    reg = [r for r in rows if r.get("kind") == "regression"]
    probes = [r for r in rows if r.get("kind") != "regression"]
    grounded = [r for r in reg if r.get("grounded")]
    errors = [r for r in rows if r.get("model_error")]
    fails: list[str] = []
    if len(grounded) < int(bar["min_grounded"]):
        fails.append(f"grounded {len(grounded)}/{len(reg)} < {bar['min_grounded']}")
    if len(errors) > int(bar["max_model_errors"]):
        fails.append(f"model errors {len(errors)}: {sorted({r['model_error'] for r in errors})}")
    if bar.get("probes_refused_by_guardrail") == "all":
        leaked = [r["kind"] for r in probes if r.get("reason") != "guardrail"]
        if leaked:
            fails.append(f"probes not refused by the guardrail: {leaked}")
    if bar.get("every_grounded_answer_cites"):
        uncited = [r["q"] for r in grounded if int(r.get("citations") or 0) < 1]
        if uncited:
            fails.append(f"grounded answers without a citation: {len(uncited)}")
    return {"pass": not fails, "fails": fails, "grounded": len(grounded), "regression": len(reg),
            "probes": len(probes), "model_errors": len(errors)}


def _act_status(tenant: str, lam: Any) -> dict[str, Any]:
    """``model_status`` for the QA tenant, as an operator (the act Lambda, JWT-shaped claims)."""
    fns = [f["FunctionName"] for p in lam.get_paginator("list_functions").paginate()
           for f in p["Functions"] if "OncaActApi" in f["FunctionName"]]
    claims = {"sub": "model-eval-352", "custom:tenant": tenant, "cognito:groups": "operator"}
    ev = {"body": json.dumps({"intent": "model_status", "args": {}}), "headers": {},
          "rawPath": "/api/me/act", "requestContext": {"http": {"method": "POST", "path": "/api/me/act"},
                                                        "authorizer": {"jwt": {"claims": claims}}}}
    r = json.loads(lam.invoke(FunctionName=fns[0], Payload=json.dumps(ev).encode())["Payload"].read())
    return json.loads(r.get("body") or "{}")


def connected_model(status: dict[str, Any]) -> dict[str, str] | None:
    c = status.get("connection") or None
    if not c or not c.get("model") or c.get("provider") in (None, "", "hosted"):
        return None
    return {"model": str(c["model"]), "provider": str(c["provider"])}


def main(argv: list[str] | None = None) -> int:
    import boto3

    from scripts import ask_regression as ar

    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True, help="a QA tenant with the model under test connected")
    ap.add_argument("--dry", action="store_true", help="check the connection and the bar; no model calls")
    a = ap.parse_args(argv)
    bar = load_bar()
    lam = boto3.client("lambda", region_name="us-east-1")
    target = connected_model(_act_status(a.tenant, lam))
    if target is None:
        print(f"refusing: tenant {a.tenant} has no client model connected (rows would be hosted Nova)")
        return 2
    print(json.dumps({"tenant": a.tenant, **target, "bar_version": bar["version"]}))
    if a.dry:
        return 0
    rows = []
    for kind, q in [("regression", q) for q in ar.QUESTIONS] + list(ar.PROBES):
        b = ar.ask(q, a.tenant, lam)
        rows.append({"kind": kind, "q": q[:80], "reason": b.get("reason"), "refused": b.get("refused"),
                     "grounded": b.get("grounded"), "citations": len(b.get("citations") or []),
                     "model_error": b.get("model_error")})
        print(json.dumps(rows[-1], ensure_ascii=False))
    verdict = score(rows, bar)
    day = datetime.date.today().isoformat()
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{target['model']}-{day}.json"
    path.write_text(json.dumps({"model": target["model"], "provider": target["provider"],
                                "tenant": a.tenant, "date": day, "bar": bar, "verdict": verdict,
                                "rows": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(verdict), "->", path.relative_to(ROOT))
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
