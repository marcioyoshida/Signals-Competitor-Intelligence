"""ADR 027 #133 — QA run artifacts + reporting index page.

Takes the FULL structured output of every parallel branch (matrix/smoke/routing/resilience/
vision — passed straight through by the state machine, not re-read from S3) and renders one
static HTML summary: a pass/fail table per branch/check, and every vision finding with its
screenshot inlined via a presigned URL. Uploaded to the SAME private `onca-qa-artifacts`
bucket used throughout this pipeline — "nothing here should require AWS console access to
review a run" (ADR 027) is satisfied via a presigned GET URL, not a new public distribution.

Presigned URLs signed with a Lambda execution role's temporary STS credentials are only
valid as long as those credentials are (well under 24h, often ~1h) — NOT the full
`PRESIGN_EXPIRES_IN` requested. Good enough for "go check the run that just finished," not
for a stable long-lived link. For a report from days ago, regenerate a fresh URL:

    aws s3 presign s3://<bucket>/<run_id>/report/index.html --expires-in 3600

See qa_pipeline/README.md for the full recipe and the rationale for not standing up a new
public CloudFront distribution just for this (these screenshots contain real, if QA-tenant-
scoped, dashboard content — the product itself gates that behind basic-auth + Cognito on
purpose, so a public status page a la Fleet Monitor's uptime page is the wrong model here).

This is also, deliberately, the ONE place in this pipeline that raises when `hard_gates_ok`
is False — every hard-gate branch upstream gets `.add_catch()`'d (infra/qa_pipeline.py)
specifically so a real failure doesn't abort the Parallel state before this report can run,
which means the Step Functions EXECUTION would otherwise show SUCCEEDED even when a hard
gate failed. Raising here, AFTER the report is fully built and uploaded, is what makes
`state_machine.metric_failed()` (the CloudWatch alarm -> SNS -> email notification) fire.
"""
from __future__ import annotations

import html
import json
import time

import boto3

from qa_pipeline.lib.browser import ARTIFACTS_BUCKET, upload_artifact

PRESIGN_EXPIRES_IN = 3600  # seconds; see module docstring for why this is aspirational


def _presign(bucket: str, key: str) -> str:
    if not bucket:
        return ""
    try:
        return boto3.client("s3").generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=PRESIGN_EXPIRES_IN,
        )
    except Exception:  # noqa: BLE001 - a report with a missing thumbnail beats no report
        return ""


def _checklist_rows(name: str, items: list[dict]) -> str:
    rows = []
    for it in items:
        cls = "ok" if it.get("ok") else "fail"
        rows.append(
            f'<tr class="{cls}"><td>{html.escape(str(it.get("name", "")))}</td>'
            f'<td>{"PASS" if it.get("ok") else "FAIL"}</td>'
            f'<td>{html.escape(str(it.get("detail", "")))}</td></tr>'
        )
    body = "\n".join(rows) or "<tr><td colspan=3>(no checks)</td></tr>"
    return f"<h3>{html.escape(name)}</h3><table>{body}</table>"


def _matrix_rows(shards: list[dict]) -> str:
    rows = []
    for s in shards:
        cls = "ok" if s.get("ok") else "fail"
        rows.append(
            f'<tr class="{cls}"><td>{s.get("browser")}</td><td>{s.get("viewport")}</td>'
            f'<td>{"PASS" if s.get("ok") else "FAIL"}</td>'
            f'<td>scroll={s.get("scroll_width")} client={s.get("client_width")}</td></tr>'
        )
    body = "\n".join(rows) or "<tr><td colspan=4>(no shards)</td></tr>"
    return f"<h3>Cross-browser matrix (#128)</h3><table>{body}</table>"


def _vision_section(bucket: str, run_id: str, findings: list[dict]) -> str:
    blocks = []
    for f in findings:
        panel = html.escape(str(f.get("panel", "")))
        if not f.get("captured"):
            blocks.append(f"<div class='vpanel'><h4>{panel}</h4><p>capture failed: "
                           f"{html.escape(str(f.get('error', '')))}</p></div>")
            continue
        img_url = _presign(bucket, f"{run_id}/vision/{f.get('panel')}.png")
        img_tag = f'<img src="{img_url}" alt="{panel}">' if img_url else "<p>(screenshot unavailable)</p>"
        if not f.get("available"):
            checklist_html = f"<p>verdict unavailable: {html.escape(str(f.get('parse_error', '')))}</p>"
        else:
            items = f.get("checklist", [])
            lis = "".join(
                f'<li class="{"ok" if it.get("pass") else "fail"}">'
                f'{"PASS" if it.get("pass") else "FLAGGED"} '
                f'(confidence {it.get("confidence")}) — {html.escape(str(it.get("item", "")))}'
                f'{" — " + html.escape(str(it.get("note", ""))) if it.get("note") else ""}</li>'
                for it in items
            )
            checklist_html = f"<ul>{lis}</ul>"
        blocks.append(f"<div class='vpanel'><h4>{panel}</h4>{img_tag}{checklist_html}</div>")
    return "<h3>Bedrock visual QA (#132, advisory — never fails the run)</h3>" + "".join(blocks)


CSS = """
body { font: 14px/1.5 -apple-system, sans-serif; margin: 2rem; color: #1a1a1a; }
h1 { font-size: 1.4rem; } h3 { margin-top: 2rem; border-bottom: 1px solid #ddd; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0; }
td, th { padding: 4px 8px; border-bottom: 1px solid #eee; text-align: left; font-size: 13px; }
tr.fail td:nth-child(2), tr.fail td:nth-child(3) { color: #b00; font-weight: 600; }
tr.ok td:nth-child(2), tr.ok td:nth-child(3) { color: #070; }
.vpanel { margin: 1rem 0; padding: 1rem; border: 1px solid #ddd; border-radius: 6px; }
.vpanel img { max-width: 100%; border: 1px solid #ccc; }
.vpanel li.fail { color: #b00; } .vpanel li.ok { color: #070; }
.summary { display: flex; gap: 1.5rem; margin: 1rem 0; }
.summary div { padding: .5rem 1rem; border-radius: 6px; background: #f4f4f4; }
.summary .allok { background: #e6f7e6; } .summary .anyfail { background: #fbe6e6; }
"""


def run(event: dict) -> dict:
    run_id = event.get("run_id") or time.strftime("%Y%m%dT%H%M%SZ")
    branches = event.get("branches") or []
    matrix = branches[0] if len(branches) > 0 else []
    smoke = branches[1] if len(branches) > 1 else {}
    routing = branches[2] if len(branches) > 2 else {}
    resilience = branches[3] if len(branches) > 3 else {}
    vision = branches[4] if len(branches) > 4 else {}

    hard_gates_ok = (
        all(s.get("ok") for s in matrix) and smoke.get("ok", False)
        and routing.get("ok", False) and resilience.get("ok", False)
    )
    summary_cls = "allok" if hard_gates_ok else "anyfail"

    parts = [
        f"<html><head><meta charset='utf-8'><title>OncaQaPipeline · {html.escape(run_id)}</title>"
        f"<style>{CSS}</style></head><body>",
        f"<h1>OncaQaPipeline run {html.escape(run_id)}</h1>",
        f"<div class='summary'><div class='{summary_cls}'>"
        f"{'ALL HARD GATES PASSED' if hard_gates_ok else 'AT LEAST ONE HARD GATE FAILED'}"
        f"</div></div>",
        _matrix_rows(matrix),
        _checklist_rows("Smoke & critical-path navigation (#129)", smoke.get("checks", [])),
        _checklist_rows("Deep-linking & routing (#130)", routing.get("checks", [])),
        _checklist_rows("Resiliency (#131)", resilience.get("checks", [])),
        _vision_section(ARTIFACTS_BUCKET, run_id, vision.get("findings", [])),
        "</body></html>",
    ]
    report_html = "\n".join(parts)

    report_key = f"{run_id}/report/index.html"
    upload_artifact(ARTIFACTS_BUCKET, report_key, body=report_html.encode())
    # A well-known, always-overwritten key so "the latest report" doesn't require knowing
    # the newest run_id — pairs with the presign recipe in the module docstring.
    upload_artifact(ARTIFACTS_BUCKET, "latest/report/index.html", body=report_html.encode())

    report_url = _presign(ARTIFACTS_BUCKET, report_key)
    result = {"ok": hard_gates_ok, "run_id": run_id, "hard_gates_ok": hard_gates_ok, "report_url": report_url}
    upload_artifact(ARTIFACTS_BUCKET, f"{run_id}/report-result.json", body=json.dumps(result).encode())

    if not hard_gates_ok:
        # The report is fully built and uploaded ABOVE this line regardless — raising here
        # only affects what happens AFTER: every hard-gate branch's failure got
        # `.add_catch()`'d (infra/qa_pipeline.py) specifically so a real failure doesn't
        # abort QaBranches before the report can run. But that means the Step Functions
        # EXECUTION itself would otherwise show SUCCEEDED even when a hard gate failed —
        # this task is the one place left where a genuine failure needs to surface as an
        # execution failure, so state_machine.metric_failed() (the CloudWatch alarm ->
        # SNS -> email notification) actually fires. The report_url is still recoverable
        # from the run_id even though it won't be in this failed execution's own output —
        # see qa_pipeline/README.md's presign recipe.
        raise AssertionError(f"hard gate(s) failed — see {report_url or report_key}")
    return result
