"""Coverage-gap loop — self-improving data coverage from unanswered questions.

When the agent (ADR 010) can't answer an *in-domain* question ("não tenho esse
dado"), that is a signal the tool's data has a gap. This module turns that signal
into a tracked, triaged, and — where safe — auto-remediated improvement:

    record gap -> triage (is the data in KB? registry? unsourced?) -> decide
    remediation -> auto-apply the SAFE/bounded ones + re-verify -> open an issue
    for the rest -> confirm & close.

**Autonomy boundary (deliberate).** The loop AUTO-APPLIES only *bounded, reversible,
data/curation* remediations — registry backfills, attribute/curation edits — which
are "deployed" instantly because the registry is the live source of truth (no code
deploy). Remediations that need **new ingestion code** are emitted as a spec + a
GitHub issue for human approval and the existing CI/CD, NOT auto-written-and-shipped
to prod: a free-text question must never steer generated code straight into the
production pipeline (supply-chain safety). `AUTO_CODEGEN` is a hard-off constant.

Pure core (`normalize_q`, `triage`, `merge_gap`) is unit tested; S3 / gh / registry
are thin adapters.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import unicodedata
from typing import Any, Callable

INDEX_KEY = "coverage_gaps/index.json"

# HARD OFF: the loop never autonomously writes+deploys new ingestion code from a
# free-text question. Codegen remediations become a spec + issue for human review.
AUTO_CODEGEN = False

# Gap lifecycle
STATUS_OPEN = "open"
STATUS_AUTO_FIXED = "auto_fixed"
STATUS_PROPOSED = "proposed"     # issue opened, awaits human implementation
STATUS_RESOLVED = "resolved"
STATUS_WONT_FIX = "wont_fix"


def _norm(text: Any) -> str:
    t = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def normalize_q(q: str) -> str:
    """Collapse a question to a dedup key (accent/case/punct/space-insensitive)."""
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", _norm(q))).strip()


def gap_id(q: str) -> str:
    return hashlib.sha1(normalize_q(q).encode("utf-8")).hexdigest()[:16]


# --- triage ---------------------------------------------------------------
# Classify WHY a question couldn't be answered, and what remediation it implies.
#   curation_gap  — a tracked entity is missing a curated ATTRIBUTE we can derive/
#                   backfill (ownership, ticker, industries) -> AUTO-FIXABLE.
#   discovery_gap — the question names an entity NOT in the registry -> propose
#                   entity discovery (ADR 011), curated add.
#   ingestion_gap — asks for a DATA TYPE no source ingests yet (e.g. certifications,
#                   ESG) -> propose an ingestion source / detector (codegen -> issue).
#   retrieval_gap — the KB likely has it but retrieval missed -> tune retrieval.
#   out_of_scope  — not actually about the tracked universe.
_ATTR_CUES = {
    "estatal", "estatais", "governamental", "governamentais",
    "publica", "publicas", "publico", "publicos",
    "privada", "privadas", "privado", "privados",
    "mista", "mistas", "economia", "controle", "natureza", "capital", "listada",
    "ticker", "acao", "acoes", "industria", "industrias", "setor",
}
_INGESTION_CUES = {
    "iso", "certificacao", "certificacoes", "certificada", "compliance", "pci",
    "soc", "esg", "sustentabilidade", "rating", "nota", "reclamacao", "reclame",
    "funcionarios", "empregados", "headcount", "receita", "faturamento",
}


def triage(
    q: str,
    *,
    resolver: Callable[[dict[str, Any]], list[str]] | None = None,
    known_entity_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Heuristic classification of an unanswered question. Pure given its inputs."""
    qn = normalize_q(q)
    toks = set(qn.split())
    entities: list[str] = []
    if resolver is not None:
        try:
            entities = resolver({"source": "News", "title": q}) or []
        except Exception:  # pragma: no cover - resolver best-effort
            entities = []

    if toks & _INGESTION_CUES:
        return {
            "class": "ingestion_gap",
            "auto_fixable": False,
            "recommendation": "Nenhuma fonte ingere esse atributo. Propor detector/fonte "
                              "(ex.: classificar em notícias/fatos ou fonte estruturada) — "
                              "requer código novo, vai para issue + CI/CD com revisão.",
            "entities": entities,
        }
    if entities and toks & _ATTR_CUES:
        return {
            "class": "curation_gap",
            "auto_fixable": True,
            "recommendation": "Entidade conhecida sem atributo curado — rodar backfill "
                              "(ownership/ticker/industries) e reverificar.",
            "entities": entities,
        }
    if not entities and (known_entity_ids is not None):
        # names something specific but nothing resolves -> likely an unknown entity
        if any(len(t) >= 4 for t in toks - _ATTR_CUES - _INGESTION_CUES):
            return {
                "class": "discovery_gap",
                "auto_fixable": False,
                "recommendation": "Possível entidade fora do registro — propor descoberta/"
                                  "curadoria (ADR 011).",
                "entities": [],
            }
    return {
        "class": "retrieval_gap",
        "auto_fixable": False,
        "recommendation": "Dado pode existir na base mas não foi recuperado — revisar "
                          "grounding/KB para essa consulta.",
        "entities": entities,
    }


# --- store (merge / list) -------------------------------------------------

def merge_gap(
    existing: dict[str, Any] | None,
    q: str,
    *,
    scope: dict[str, Any] | None = None,
    reason: str = "no-grounding",
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Upsert a gap record (dedup by normalized question)."""
    today = today or dt.date.today()
    idx = dict(existing or {})
    records: dict[str, dict[str, Any]] = dict(idx.get("records") or {})
    gid = gap_id(q)
    now = today.isoformat()
    rec = records.get(gid)
    if rec is None:
        records[gid] = {
            "id": gid, "question": q.strip(), "normalized": normalize_q(q),
            "scope": scope or {}, "reason": reason,
            "first_seen": now, "last_seen": now, "count": 1,
            "status": STATUS_OPEN,
        }
    else:
        rec["count"] = int(rec.get("count", 0)) + 1
        rec["last_seen"] = now
        # a resolved gap that recurs re-opens
        if rec.get("status") == STATUS_RESOLVED:
            rec["status"] = STATUS_OPEN
    return {"as_of": now, "count": len(records), "records": records}


def list_open(index: dict[str, Any]) -> list[dict[str, Any]]:
    recs = [r for r in (index.get("records") or {}).values()
            if r.get("status") in (STATUS_OPEN, STATUS_PROPOSED)]
    recs.sort(key=lambda r: (int(r.get("count", 0)), str(r.get("last_seen") or "")), reverse=True)
    return recs


# --- S3 adapters ----------------------------------------------------------

def load_index(bucket: str, *, s3: Any | None = None) -> dict[str, Any]:
    import boto3
    s3 = s3 or boto3.client("s3")
    try:
        body = s3.get_object(Bucket=bucket, Key=INDEX_KEY)["Body"].read()
        data = json.loads(body)
        return data if isinstance(data, dict) else {}
    except Exception:  # pragma: no cover - first run
        return {}


def publish(index: dict[str, Any], bucket: str, *, s3: Any | None = None) -> str:
    import boto3
    s3 = s3 or boto3.client("s3")
    s3.put_object(
        Bucket=bucket, Key=INDEX_KEY,
        Body=json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{bucket}/{INDEX_KEY}"


def record(
    q: str, bucket: str, *,
    scope: dict[str, Any] | None = None, reason: str = "no-grounding",
    s3: Any | None = None, today: dt.date | None = None,
) -> dict[str, Any]:
    """Capture stage: fold an unanswered in-domain question into the gap store."""
    index = load_index(bucket, s3=s3)
    merged = merge_gap(index, q, scope=scope, reason=reason, today=today)
    publish(merged, bucket, s3=s3)
    return merged["records"][gap_id(q)]


# --- safe auto-fix (bounded, reversible, data-only) -----------------------

def safe_autofix() -> dict[str, Any]:
    """Apply the bounded/reversible registry remediations (idempotent). These
    "deploy" instantly — the registry is the live source of truth, no code deploy.
    Returns what changed. This is the ONLY thing the loop mutates autonomously."""
    from src.synth import entity_registry as reg
    changed: dict[str, Any] = {}
    try:
        changed["ownership"] = len(reg.backfill_ownership())
        changed["tickers"] = len(reg.backfill_tickers())
        changed["curation"] = reg.backfill_curation()
    except Exception as exc:  # pragma: no cover - best-effort
        changed["error"] = str(exc)
    return changed


# --- GitHub issue (dedup by gap id) ---------------------------------------

def open_issue(gap: dict[str, Any], triage_result: dict[str, Any]) -> str | None:
    """Open (or find) a GitHub issue for a gap via the `gh` CLI. Deduped by a
    marker line carrying the gap id. Returns the issue URL, or None on failure."""
    import subprocess
    marker = f"coverage-gap-id: {gap['id']}"
    try:
        found = subprocess.run(
            ["gh", "issue", "list", "--search", marker, "--state", "all",
             "--json", "url", "--limit", "1"],
            capture_output=True, text=True, timeout=30,
        )
        if found.returncode == 0 and found.stdout.strip():
            arr = json.loads(found.stdout)
            if arr:
                return arr[0].get("url")
        title = f"Coverage gap: {gap['question'][:70]}"
        body = (
            f"Pergunta não respondida pelo agente (ADR 010).\n\n"
            f"**Pergunta:** {gap['question']}\n"
            f"**Classe:** {triage_result['class']}\n"
            f"**Recomendação:** {triage_result['recommendation']}\n"
            f"**Ocorrências:** {gap.get('count', 1)} · visto por último {gap.get('last_seen')}\n"
            f"**Entidades resolvidas:** {', '.join(triage_result.get('entities') or []) or '—'}\n\n"
            f"<!-- {marker} -->\n"
            f"_Auto-aberto pelo coverage-gap loop. Remediações de código novo passam por "
            f"revisão humana + CI/CD (AUTO_CODEGEN=off)._"
        )
        created = subprocess.run(
            ["gh", "issue", "create", "--title", title, "--body", body,
             "--label", "coverage-gap"],
            capture_output=True, text=True, timeout=30,
        )
        if created.returncode == 0:
            return created.stdout.strip().splitlines()[-1]
        print(f"Warning: gh issue create failed: {created.stderr[:200]}")
    except Exception as exc:  # pragma: no cover - gh optional
        print(f"Warning: gh unavailable: {exc}")
    return None


def close_issue(issue_url: str | None, *, token: str | None = None) -> bool:
    """Close a GitHub issue via the REST API (owner/repo/number parsed from the
    url). Works anywhere `ONCA_GH_TOKEN` is set (incl. the Lambda) — no `gh` CLI.
    Returns True if closed; False (no-op) when there's no url or token."""
    token = token or os.environ.get("ONCA_GH_TOKEN")
    if not issue_url or not token:
        return False
    m = re.search(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)", str(issue_url))
    if not m:
        return False
    owner, repo, number = m.group(1), m.group(2), m.group(3)
    import urllib.request
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{number}",
        data=json.dumps({"state": "closed",
                         "state_reason": "completed"}).encode("utf-8"),
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "onca-coverage-loop",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:  # pragma: no cover - network best-effort
        print(f"Warning: close_issue failed: {exc}")
        return False


# --- remediation driver (the pipeline) ------------------------------------

def remediate(
    bucket: str, *,
    resolver: Callable[[dict[str, Any]], list[str]] | None = None,
    known_entity_ids: set[str] | None = None,
    verifier: Callable[[str], bool] | None = None,
    autofixer: Callable[[], dict[str, Any]] = safe_autofix,
    issuer: Callable[[dict[str, Any], dict[str, Any]], str | None] = open_issue,
    closer: Callable[[str | None], bool] = close_issue,
    only_id: str | None = None,
    s3: Any | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Run the loop over open gaps: triage -> auto-fix safe ones + re-verify ->
    open an issue for the rest -> close what's now answerable.

    `verifier(question) -> bool` re-asks the agent and reports whether it now
    grounds; injected so the driver is testable without live Bedrock. `only_id`
    restricts the run to a single gap (the dashboard "Remediar" button).
    """
    index = load_index(bucket, s3=s3)
    records = index.get("records") or {}
    summary = {"triaged": 0, "auto_fixed": 0, "proposed": 0, "resolved": 0}
    did_autofix = False

    for gid, rec in records.items():
        if only_id is not None and gid != only_id:
            continue
        if rec.get("status") not in (STATUS_OPEN, STATUS_PROPOSED):
            continue
        t = triage(rec["question"], resolver=resolver, known_entity_ids=known_entity_ids)
        rec["triage"] = t
        summary["triaged"] += 1

        if t.get("auto_fixable"):
            if not did_autofix:
                rec["autofix"] = autofixer()  # once per run (backfills are global)
                did_autofix = True
            # re-verify: can the agent answer now?
            if verifier is not None and verifier(rec["question"]):
                rec["status"] = STATUS_RESOLVED
                rec["resolved_at"] = (today or dt.date.today()).isoformat()
                rec["issue_closed"] = bool(rec.get("issue_url")) and closer(rec.get("issue_url"))
                summary["auto_fixed"] += 1
                summary["resolved"] += 1
            else:
                url = issuer(rec, t)
                if url:
                    rec["issue_url"] = url
                rec["status"] = STATUS_PROPOSED
                summary["proposed"] += 1
        else:
            # verify first — maybe a prior fix or new ingest already covers it
            if verifier is not None and verifier(rec["question"]):
                rec["status"] = STATUS_RESOLVED
                rec["resolved_at"] = (today or dt.date.today()).isoformat()
                rec["issue_closed"] = bool(rec.get("issue_url")) and closer(rec.get("issue_url"))
                summary["resolved"] += 1
            else:
                url = issuer(rec, t)
                if url:
                    rec["issue_url"] = url
                if rec.get("status") != STATUS_PROPOSED:
                    summary["proposed"] += 1
                rec["status"] = STATUS_PROPOSED

    index["records"] = records
    publish(index, bucket, s3=s3)
    return summary


# --- runnable driver: the coverage-gap pipeline ---------------------------

def _agent_verifier(site_bucket: str) -> Callable[[str], bool]:
    """A verifier that re-asks the live agent (fresh feed + Bedrock) and reports
    whether the question now grounds. Used to confirm+close after a fix."""
    from src.dashboard import agent_ask
    from src.synth.bedrock_llm import converse

    def verify(q: str) -> bool:
        try:
            feed = agent_ask._load_feed(site_bucket)
            res = agent_ask.answer(q, feed=feed, converser=converse,
                                   kb_retrieve=agent_ask._kb_retrieve
                                   if os.environ.get("ONCA_KB_ID") else None)
            return bool(res.get("grounded"))
        except Exception as exc:  # pragma: no cover
            print(f"verify failed: {exc}")
            return False
    return verify


def run_pipeline() -> dict[str, Any]:
    """Wire the real adapters and run the loop once (schedulable / CI / manual)."""
    bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    site = os.environ.get("ONCA_SITE_BUCKET")
    if not bucket:
        return {"status": "no_bucket"}
    from src.synth.entities import resolve_entities
    verifier = _agent_verifier(site) if site else None
    return remediate(bucket, resolver=resolve_entities, verifier=verifier)


if __name__ == "__main__":
    print(json.dumps(run_pipeline(), ensure_ascii=False))
