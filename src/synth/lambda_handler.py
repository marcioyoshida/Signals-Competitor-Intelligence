"""Stage B Lambda — digest → candidates → (optional KB/LLM) → cited narratives."""
from __future__ import annotations

import json
import os
from typing import Any

from src.synth import candidates, digest_io, retrieve, synthesize


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Produce flagged narratives with citation guardrails.

    Always works digest-first. KB Retrieve and Bedrock Converse are optional
    and degrade gracefully when quotas/model access block them.
    """
    event = event or {}
    max_cand = int(os.environ.get("ONCA_SYNTH_MAX_CANDIDATES", "10"))
    use_llm = os.environ.get("ONCA_SYNTH_USE_LLM", "true").lower() in (
        "1",
        "true",
        "yes",
    )
    use_kb = os.environ.get("ONCA_SYNTH_USE_KB", "true").lower() in (
        "1",
        "true",
        "yes",
    )

    digest = digest_io.load_digest_from_event(event)
    if not digest:
        payload = {
            "status": "no_digest",
            "narrative_count": 0,
            "narratives": [],
            "keys": [],
        }
        return {"statusCode": 200, "body": json.dumps(payload, ensure_ascii=False)}

    cands = candidates.extract_candidates(digest, max_candidates=max_cand)
    narratives: list[dict[str, Any]] = []
    keys: list[str] = []

    for cand in cands:
        if use_kb:
            extras = retrieve.enrich_with_kb(cand)
            if extras:
                cand = {
                    **cand,
                    "sources": list(cand.get("sources") or []) + extras,
                    "related": list(cand.get("related") or []) + extras,
                }
        result = synthesize.synthesize_candidate(cand, use_llm=use_llm)
        if not result:
            continue
        key = digest_io.write_narrative(result)
        if key:
            keys.append(key)
            result["s3_key"] = key
        narratives.append(result)

    payload = {
        "status": "ok",
        "candidate_count": len(cands),
        "narrative_count": len(narratives),
        "narratives": narratives,
        "keys": keys,
        "source": "stage_b_synth",
    }
    return {
        "statusCode": 200,
        "body": json.dumps(payload, ensure_ascii=False, indent=2),
    }


if __name__ == "__main__":
    import os

    # Local dry-run with a tiny fixture (no S3/Bedrock required).
    fixture = {
        "digest": {
            "regulatory": {
                "items": [
                    {
                        "id": "bcb:demo",
                        "doc_type": "Resolução",
                        "number": "1",
                        "subject": "Demo Pix rule for payment institutions",
                        "url": "https://www.bcb.gov.br/demo/resolucao-1",
                        "kind": "regulatory",
                        "source": "BCB",
                    }
                ]
            },
            "ofertas": {
                "items": [
                    {
                        "id": "cvm-oferta:r160:1",
                        "issuer": "Demo IP S.A.",
                        "security": "Debêntures",
                        "leader": "BTG PACTUAL",
                        "url": "https://dados.cvm.gov.br/dataset/oferta-distrib",
                        "source": "CVM-Ofertas",
                    }
                ]
            },
        }
    }
    os.environ.setdefault("ONCA_SYNTH_USE_LLM", "false")
    os.environ.setdefault("ONCA_SYNTH_USE_KB", "false")
    print(lambda_handler(fixture, None)["body"])
