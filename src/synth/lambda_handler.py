"""Stage B Lambda — digest → fused candidates → cited narratives."""
from __future__ import annotations

import json
import os
from typing import Any

from src.synth import candidates, digest_io, retrieve, synthesize


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Produce flagged narratives with citation guardrails.

    Digest-first. Uses items + context samples so seeded digests still fuse.
    KB Retrieve and Bedrock Converse are optional and degrade gracefully.
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
            "candidate_count": 0,
            "narrative_count": 0,
            "narratives": [],
            "keys": [],
            "fusion": {},
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

    fusion = {
        "entity_fusion": sum(1 for c in cands if c.get("kind") == "entity_fusion"),
        "regulatory_fusion": sum(1 for c in cands if c.get("kind") == "regulatory_fusion"),
        "alerts": sum(1 for c in cands if c.get("is_alert")),
        "multi_lens": sum(1 for c in cands if len(c.get("lenses") or []) >= 2),
    }
    status = "ok" if narratives else "ok_empty"
    payload = {
        "status": status,
        "candidate_count": len(cands),
        "narrative_count": len(narratives),
        "narratives": narratives,
        "keys": keys,
        "fusion": fusion,
        "source": "stage_b_synth",
    }
    return {
        "statusCode": 200,
        "body": json.dumps(payload, ensure_ascii=False, indent=2),
    }


if __name__ == "__main__":
    import sys

    os.environ.setdefault("ONCA_SYNTH_USE_LLM", "false")
    os.environ.setdefault("ONCA_SYNTH_USE_KB", "false")

    # python -m src.synth.lambda_handler /path/to/digest.json
    # python -m src.synth.lambda_handler --s3   (needs AWS creds + bucket env)
    if len(sys.argv) > 1 and sys.argv[1] == "--s3":
        os.environ.setdefault("ONCA_DIGESTS_BUCKET", "onca-digests-668449743071")
        print(lambda_handler({}, None)["body"])
    elif len(sys.argv) > 1:
        digest = json.loads(open(sys.argv[1], encoding="utf-8").read())
        # disable S3 narrative writes for local file runs unless bucket set
        print(lambda_handler({"digest": digest}, None)["body"])
    else:
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
                            "is_new": True,
                        }
                    ],
                    "context": [],
                },
                "sec_filings": {
                    "items": [],
                    "context": [
                        {
                            "id": "sec:nu:1",
                            "ticker": "NU",
                            "form": "6-K",
                            "company": "Nu Holdings Ltd.",
                            "filed": "2026-07-10",
                            "url": "https://www.sec.gov/Archives/edgar/data/nu/x.htm",
                            "source": "SEC-EDGAR",
                            "is_new": False,
                        }
                    ],
                },
                "ofertas": {
                    "items": [],
                    "context": [
                        {
                            "id": "cvm-oferta:r160:1",
                            "issuer": "Demo fund vehicle",
                            "security": "Debêntures",
                            "leader": "BTG PACTUAL",
                            "url": "https://dados.cvm.gov.br/dataset/oferta-distrib",
                            "source": "CVM-Ofertas",
                        }
                    ],
                },
                "market": {
                    "items": [
                        {"institution": "ITAU", "value": 1.0, "share_pct": 14.8}
                    ]
                },
            }
        }
        print(lambda_handler(fixture, None)["body"])
