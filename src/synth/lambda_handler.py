"""Stage B Lambda — digest → fused candidates → cited narratives."""
from __future__ import annotations

import json
import os
from typing import Any

from src.synth import candidates, digest_io, synthesize


def _commit_news_seen(digest: dict[str, Any]) -> int:
    """Second phase of the deferred news diff (issue #23): mark the fetched news
    ids seen now that synth has consumed them. Returns the number committed; 0
    when there is nothing to commit or the state store is unavailable. Never
    raises — an un-committed slice safely re-surfaces on the next run."""
    news = digest.get("news") if isinstance(digest, dict) else None
    ids = news.get("fetched_ids") if isinstance(news, dict) else None
    if not ids:
        return 0
    try:
        from src.diff.engine import DynamoDbState, commit_seen

        # Must use the DynamoDB-backed state (same as the ingest branch): the
        # JsonState default writes a local file, which is read-only on Lambda.
        commit_seen("trade_press", ids, state=DynamoDbState("trade_press"))
        return len(ids)
    except Exception as exc:  # pragma: no cover - best-effort; safe to re-surface
        print(f"Warning: news seen-commit failed (items will re-surface): {exc}")
        return 0


def _update_distress(digest: dict[str, Any]) -> dict[str, Any]:
    """Fold RJ/falência headlines from this run's news into the durable distress
    store (option A). Best-effort — a failure never breaks synthesis."""
    bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not bucket:
        return {"new_events": 0, "records": 0}
    try:
        from src.synth import distress

        # Mines the trusted CVM Fato Relevante stream (regulatory-grade) AND news
        # (reported/corroborated), grading each record's confidence by source.
        return distress.update_from_digest(digest, bucket)
    except Exception as exc:  # pragma: no cover - best-effort; never crash synth
        print(f"Warning: distress update skipped: {exc}")
        return {"new_events": 0, "records": 0}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Produce flagged narratives with citation guardrails.

    Digest-first. Uses items + context samples so seeded digests still fuse.
    KB Retrieve and Bedrock Converse are optional and degrade gracefully.
    """
    event = event or {}
    max_cand = int(os.environ.get("ONCA_SYNTH_MAX_CANDIDATES", "10"))
    use_llm = os.environ.get("ONCA_SYNTH_USE_LLM", "false").lower() in (
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

    # NOTE: KB Retrieve (semantic neighbours) was deliberately removed from the
    # fusion path. Merging retrieved docs into `sources` made the LLM weave
    # unrelated background into the narrative and cite it as if correlated — a
    # false "nexus" (e.g. a Nomad points promo "linked" to an Open Finance
    # manual). Grounding must use the entity's *own* signals, not neighbours; if
    # reintroduced, KB context must stay out of sources/citations/source_ids.
    for cand in cands:
        result = synthesize.synthesize_candidate(cand, use_llm=use_llm)
        if not result:
            continue
        key = digest_io.write_narrative(result)
        if key:
            keys.append(key)
            result["s3_key"] = key
        narratives.append(result)

    # Deferred news-commit (issue #23): the news branch computed its fresh set
    # WITHOUT marking anything seen. Now that synth has consumed the slice, mark
    # exactly those fetched ids seen so they don't re-fire next run. Doing it here
    # (not at fetch time) means a fetch-only run or a synth that never reached
    # this point leaves the news un-burned, to re-surface next run — so an entity
    # with real, multi-outlet news never looks falsely silent. Best-effort: a
    # commit failure just means the items re-surface (safe), never a crash.
    committed = _commit_news_seen(digest)

    # Entity-tagged distress store (option A, 2026-08-25): mine the news slice for
    # RJ/falência headlines and fold them into a durable distress/index.json keyed
    # by (entity, kind). News is the only channel that names the company (DataJud
    # is party-scrubbed) and is otherwise ephemeral, so persist it here. Uses the
    # FULL fetched news (not just the consumed candidates) so an RJ event that
    # didn't clear the fusion floor is still recorded. Best-effort.
    distress_summary = _update_distress(digest)

    fusion = {
        "entity_fusion": sum(1 for c in cands if c.get("kind") == "entity_fusion"),
        "regulatory_fusion": sum(1 for c in cands if c.get("kind") == "regulatory_fusion"),
        "alerts": sum(1 for c in cands if c.get("is_alert")),
        "multi_lens": sum(1 for c in cands if len(c.get("lenses") or []) >= 2),
        "min_lenses": int(os.environ.get("ONCA_SYNTH_MIN_LENSES", "2")),
        "min_score": float(os.environ.get("ONCA_SYNTH_MIN_SCORE", "0.45")),
        "news_committed": committed,
        "distress": distress_summary,
    }
    status = "ok" if narratives else "ok_empty"
    # Return a COMPACT result: narratives are persisted to S3 (``keys``) and the
    # feed builder reads them from there — echoing the full list inline as well
    # blows the Step Functions 256 KB task-result limit once a run synthesises
    # more than a handful (States.DataLimitExceeded). Keep counts + keys only.
    payload = {
        "status": status,
        "candidate_count": len(cands),
        "narrative_count": len(narratives),
        "keys": keys,
        "fusion": fusion,
        "as_of": next((n.get("as_of") for n in narratives if n.get("as_of")), None),
        "source": "stage_b_synth",
    }
    return {
        "statusCode": 200,
        "body": json.dumps(payload, ensure_ascii=False),
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
