"""ADR 022 Phase 5 — FinBERT-PT-BR financial-tone feature (SHADOW).

A deterministic, cross-entity **financial tone** signal derived from the prudential-solvency store
(`bcb_soundness` Tier A). For each tracked entity we state its real figures as pt-BR sentences and
score them with **FinBERT-PT-BR** (`lucas-leme/FinBERT-PT-BR`), producing a net tone in [-1, 1] =
P(POSITIVE) − P(NEGATIVE). Written to a durable **shadow** store `financial_tone/index.json`.

**Shadow-first (the ADR guardrail).** This is computed and stored but NOT surfaced anywhere (no
feed join, no board) until the model is validated against real pt-BR results-release language and
its cost is measured. Every value is labelled `is_inference: True`.

**Honesty on the corpus.** The tone inputs here are deterministic *fact-paraphrases of the stored
numbers* (Basileia level + band), which validates the FinBERT WIRING end-to-end. The production tone
reads the actual **results-release / Pilar 3 TEXT** (ADR 022 Phase 6); swapping the corpus is a
change of `sentences_for`, not of this module's shape.

**Compute placement.** `score_fn` is injected so the module is pure/testable. The real FinBERT is
`finbert_score_fn()` (lazy transformers pipeline). In the cloud it runs scale-to-zero — a SageMaker
HuggingFace-DLC Batch Transform (or a container Lambda) as a second task on `OncaFinancialsPipeline`;
that packaging is the remaining infra step (needs docker/SageMaker, not this module).
"""
from __future__ import annotations

import json
from typing import Any, Callable

INDEX_KEY = "financial_tone/index.json"
MODEL = "lucas-leme/FinBERT-PT-BR"

ScoreFn = Callable[[str], dict[str, float]]


def sentences_for(rec: dict[str, Any]) -> list[str]:
    """Deterministic pt-BR statements of a solvency record's REAL figures (not scraped filings)."""
    out: list[str] = []
    bas = rec.get("indice_basileia")
    band = rec.get("band")
    cet1 = rec.get("capital_principal")
    if bas is not None:
        tail = f", classificado como {band}" if band else ""
        out.append(f"O Índice de Basileia da instituição é de {bas}%{tail}.")
    if cet1 is not None:
        out.append(f"O capital principal (CET1) é de {cet1}%.")
    return out


def _net_tone(sentences: list[str], score_fn: ScoreFn) -> float | None:
    nets = []
    for s in sentences:
        sc = score_fn(s) or {}
        nets.append(float(sc.get("POSITIVE", 0.0)) - float(sc.get("NEGATIVE", 0.0)))
    return round(sum(nets) / len(nets), 3) if nets else None


def build_tone(soundness_index: dict[str, Any], score_fn: ScoreFn,
               pilar3_corpus: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Roll the solvency store into a per-entity shadow tone store. When a `pilar3_corpus`
    (`{entity: [sentences]}`, ADR 022 Phase 6) has real risk-report prose for an entity, tone is
    read from THAT (`corpus="pilar3"`) — the non-circular signal; otherwise it falls back to the
    deterministic solvency-fact paraphrases (`corpus="solvency_facts"`)."""
    pilar3_corpus = pilar3_corpus or {}
    records: dict[str, dict[str, Any]] = {}
    for eid, rec in ((soundness_index or {}).get("records") or {}).items():
        p3 = pilar3_corpus.get(eid)
        sents = p3 if p3 else sentences_for(rec)
        if not sents:
            continue
        records[eid] = {
            "entity": eid,
            "financial_tone_net": _net_tone(sents, score_fn),   # [-1, 1]
            "band": rec.get("band"),
            "base_date": rec.get("base_date"),
            "n_sentences": len(sents),
            "corpus": "pilar3" if p3 else "solvency_facts",
            "is_inference": True,
        }
    return {"as_of": (soundness_index or {}).get("as_of"),
            "base_date": (soundness_index or {}).get("base_date"),
            "model": MODEL, "shadow": True, "count": len(records),
            "n_pilar3": sum(1 for r in records.values() if r["corpus"] == "pilar3"),
            "records": records}


def tone_by_entity(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{entity_id: {financial_tone_net, band, base_date}} — the projection a future (non-shadow)
    feed join would consume. Unused while shadow."""
    return {eid: {"financial_tone_net": r.get("financial_tone_net"), "band": r.get("band"),
                  "base_date": r.get("base_date")}
            for eid, r in ((index or {}).get("records") or {}).items()
            if r.get("financial_tone_net") is not None}


def finbert_score_fn(model_id: str = MODEL) -> ScoreFn:
    """Lazy FinBERT-PT-BR scorer (needs transformers+torch). Returns {LABEL: prob}."""
    from transformers import pipeline

    clf = pipeline("text-classification", model=model_id, top_k=None)

    def _score(text: str) -> dict[str, float]:
        return {d["label"]: float(d["score"]) for d in clf(text)[0]}

    return _score


def sagemaker_score_fn(endpoint_name: str, *, client: Any | None = None) -> ScoreFn:
    """Scale-to-zero cloud scorer — invoke a SageMaker (HuggingFace-DLC, serverless) endpoint
    running FinBERT-PT-BR. The HF text-classification container returns a list of
    ``[{"label","score"}, ...]`` (top_k=None); we fold it to ``{LABEL: prob}``."""
    import json as _json

    import boto3

    rt = client or boto3.client("sagemaker-runtime")

    def _score(text: str) -> dict[str, float]:
        resp = rt.invoke_endpoint(
            EndpointName=endpoint_name, ContentType="application/json",
            Body=_json.dumps({"inputs": text, "parameters": {"top_k": None}}).encode("utf-8"),
        )
        out = _json.loads(resp["Body"].read())
        preds = out[0] if out and isinstance(out[0], list) else out
        return {p["label"]: float(p["score"]) for p in preds}

    return _score


def run(bucket: str | None = None, *, score_fn: ScoreFn | None = None, s3: Any | None = None,
        soundness_key: str = "soundness/index.json") -> dict[str, Any]:
    """Read the solvency store → build the SHADOW tone store → publish. `score_fn` defaults to a
    SageMaker endpoint (env ONCA_FINBERT_ENDPOINT); with no scorer available it no-ops so the last
    shadow store stands (never fabricates tone)."""
    import json as _json
    import os

    import boto3

    s3 = s3 or boto3.client("s3")
    if score_fn is None:
        ep = os.environ.get("ONCA_FINBERT_ENDPOINT")
        if not ep:
            return {"status": "noop", "reason": "no ONCA_FINBERT_ENDPOINT configured"}
        score_fn = sagemaker_score_fn(ep)
    try:
        soundness = _json.loads(s3.get_object(Bucket=bucket, Key=soundness_key)["Body"].read())
    except Exception as exc:  # pragma: no cover
        return {"status": "error", "reason": f"soundness store unreadable: {exc}"}
    # Phase 6: prefer real Pilar 3 risk-report prose per entity, else solvency-fact fallback.
    pilar3_corpus: dict[str, list[str]] = {}
    try:
        from src.ingest import pilar3
        pilar3_corpus = pilar3.corpus_by_entity(pilar3.load_index(bucket, s3=s3))
    except Exception:  # pragma: no cover - best-effort
        pilar3_corpus = {}
    idx = build_tone(soundness, score_fn, pilar3_corpus=pilar3_corpus)
    if bucket:
        publish(idx, bucket, s3=s3)
    return {"status": "ok", "count": idx["count"], "n_pilar3": idx.get("n_pilar3", 0), "shadow": True}


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    """OncaFinancialsPipeline ToneTask (ADR 022 Phase 5) — SHADOW, scale-to-zero via SageMaker."""
    import json as _json
    import os

    return {"statusCode": 200,
            "body": _json.dumps(run(os.environ.get("ONCA_DIGESTS_BUCKET")), ensure_ascii=False)}


# --- durable shadow store I/O (mirrors bcb_soundness) ----------------------------------
def load_index(bucket: str, *, s3: Any | None = None) -> dict[str, Any]:
    import boto3

    s3 = s3 or boto3.client("s3")
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=INDEX_KEY)["Body"].read())
    except Exception:  # pragma: no cover - first run
        return {}


def publish(index: dict[str, Any], bucket: str, *, s3: Any | None = None) -> str:
    import boto3

    s3 = s3 or boto3.client("s3")
    s3.put_object(Bucket=bucket, Key=INDEX_KEY,
                  Body=json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8"),
                  ContentType="application/json")
    return f"s3://{bucket}/{INDEX_KEY}"
