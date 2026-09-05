"""ADR 021 §H/§F — promote closed decisions (and the officer reference) into the Knowledge Base.

The flywheel's data-generation step: a decision **with an observed outcome** is a *precedent* —
promoted, once, into the Bedrock KB's S3 data source (the raw corpus bucket) as a cited document,
so an officer's next grounded read (`agent_ask` + `_kb_retrieve`) can retrieve its own past
decisions and their outcomes (§F Mechanism 1). Continuous + **seen-set-gated** (`kb_promoted` on
the decision item) so only new closed decisions are ingested each cycle.

Also seeds the per-officer **reference/playbook** (§H) into the KB — written once (idempotent by
key), so every officer grounds on its curated baseline even at zero live signal.

Docs follow Bedrock's convention: a `.txt` object + a sibling `.metadata.json` carrying citable
attributes. No PII — a decision precedent records the recommendation, verdict, outcome, officer,
industry and the first-party sources consulted (the §H beacon trail), never personal data.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

_DECISION_PREFIX = "decisions/"
_REFERENCE_PREFIX = "reference/"


def _client(s3: Any | None):
    if s3 is not None:
        return s3
    import boto3
    return boto3.client("s3")


def decision_doc(d: dict[str, Any]) -> str:
    """A cited decision-precedent document (pt-BR)."""
    lines = [
        f"Precedente de decisão executiva — Onça · {(d.get('officer') or '').upper()}",
        f"Gatilho/recomendação: {d.get('recommendation') or ''}",
        f"Setor: {d.get('industry') or 'geral'}",
        f"Decisão: {d.get('verdict') or ''}"
        + (f" — {d.get('rationale')}" if d.get("rationale") else ""),
        f"Resultado observado: {d.get('outcome') or 'pendente'}"
        + (f" ({d.get('outcome_note')})" if d.get("outcome_note") else ""),
        f"Ação de catálogo: {d.get('action_ref') or '-'}",
    ]
    refs = [r.get("url") for r in (d.get("references") or []) if r.get("url")]
    if refs:
        lines.append("Fontes consultadas na decisão: " + "; ".join(refs))
    lines.append(f"Data: {d.get('created_at') or ''}")
    return "\n".join(x for x in lines if x)


def _metadata(attrs: dict[str, Any]) -> bytes:
    return json.dumps({"metadataAttributes": {k: v for k, v in attrs.items() if v not in (None, "")}},
                      ensure_ascii=False).encode("utf-8")


def promote_decisions(decisions: list[dict[str, Any]], *, bucket: str, table: Any | None = None,
                      s3: Any | None = None, mark=None) -> list[str]:
    """Write each CLOSED, not-yet-promoted decision to the KB bucket (+ metadata sidecar) and
    stamp it promoted. Returns the promoted decision ids. Caller triggers one ingestion job if
    the list is non-empty. `mark` defaults to `decision_log.mark_promoted`."""
    if mark is None:
        from src.synth import decision_log
        mark = lambda did: decision_log.mark_promoted(did, table=table)
    s3 = _client(s3)
    promoted: list[str] = []
    for d in decisions:
        if d.get("outcome") in (None, "pendente") or d.get("kb_promoted"):
            continue  # only closed decisions, once (seen-set)
        did = d.get("decision_id")
        if not did:
            continue
        key = f"{_DECISION_PREFIX}{did}.txt"
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=decision_doc(d).encode("utf-8"),
                          ContentType="text/plain; charset=utf-8")
            s3.put_object(Bucket=bucket, Key=key + ".metadata.json",
                          Body=_metadata({"source": "onca-decision", "doc_type": "decision_precedent",
                                          "officer": d.get("officer"), "industry": d.get("industry"),
                                          "outcome": d.get("outcome"), "date": (d.get("created_at") or "")[:10]}),
                          ContentType="application/json")
        except Exception as exc:  # pragma: no cover - write best-effort
            print(f"Warning: decision KB write failed for {did}: {exc}")
            continue
        try:
            mark(did)
        except Exception as exc:  # pragma: no cover
            print(f"Warning: decision mark_promoted failed for {did}: {exc}")
        promoted.append(did)
    return promoted


def seed_reference(reference: dict[str, Any], *, bucket: str, s3: Any | None = None) -> list[str]:
    """Seed the per-officer reference/playbook into the KB (§H). Idempotent by a content hash
    marker (`reference/<officer>.hash`) — rewrites + re-ingests only when the curated content
    actually changes. Returns the officer keys (re)written."""
    s3 = _client(s3)
    written: list[str] = []
    for officer, doc in (reference or {}).items():
        text = _reference_doc(officer, doc)
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        marker = f"{_REFERENCE_PREFIX}{officer}.hash"
        try:
            prev = s3.get_object(Bucket=bucket, Key=marker)["Body"].read().decode().strip()
        except Exception:
            prev = None
        if prev == h:
            continue
        key = f"{_REFERENCE_PREFIX}{officer}.txt"
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=text.encode("utf-8"),
                          ContentType="text/plain; charset=utf-8")
            s3.put_object(Bucket=bucket, Key=key + ".metadata.json",
                          Body=_metadata({"source": "onca-reference", "doc_type": "officer_reference",
                                          "officer": officer}), ContentType="application/json")
            s3.put_object(Bucket=bucket, Key=marker, Body=h.encode("utf-8"))
        except Exception as exc:  # pragma: no cover
            print(f"Warning: reference KB seed failed for {officer}: {exc}")
            continue
        written.append(officer)
    return written


def _reference_doc(officer: str, doc: dict[str, Any]) -> str:
    lines = [f"Referência (base) — Onça · {officer.upper()}: {doc.get('title') or ''}"]
    for sec in (doc.get("sections") or []):
        lines.append(f"\n{sec.get('h') or ''}")
        for it in (sec.get("items") or []):
            lines.append(f"- {it}")
    return "\n".join(lines)
