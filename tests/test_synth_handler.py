import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import lambda_handler


def test_handler_produces_cited_narratives_without_bedrock(monkeypatch):
    monkeypatch.setenv("ONCA_SYNTH_USE_LLM", "false")
    monkeypatch.setenv("ONCA_SYNTH_USE_KB", "false")
    # No S3 writes in unit test
    monkeypatch.setattr(lambda_handler.digest_io, "write_narrative", lambda *a, **k: None)

    event = {
        "digest": {
            "regulatory": {
                "items": [
                    {
                        "id": "bcb:demo",
                        "doc_type": "Resolução",
                        "number": "99",
                        "subject": "Demo Pix rule",
                        "url": "https://www.bcb.gov.br/demo/99",
                        "kind": "regulatory",
                        "source": "BCB",
                    }
                ]
            }
        }
    }
    resp = lambda_handler.lambda_handler(event, None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["status"] == "ok"
    assert body["narrative_count"] >= 1
    narr = body["narratives"][0]
    assert narr["citations"]
    assert narr["narrative"]
    assert narr.get("threat_score_note") == "estimated_heuristic"


def test_handler_no_digest(monkeypatch):
    monkeypatch.setattr(lambda_handler.digest_io, "load_digest_from_event", lambda e: None)
    resp = lambda_handler.lambda_handler({}, None)
    body = json.loads(resp["body"])
    assert body["status"] == "no_digest"
    assert body["narrative_count"] == 0
