import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import lambda_handler


def test_handler_produces_cited_narratives_without_bedrock(monkeypatch):
    monkeypatch.setenv("ONCA_SYNTH_USE_LLM", "false")
    monkeypatch.setenv("ONCA_SYNTH_USE_KB", "false")
    # The handler returns a COMPACT payload (counts + S3 keys, no inline
    # narratives — see the Step Functions size-limit note), so capture the
    # written narrative to assert on its content.
    written = []
    monkeypatch.setattr(
        lambda_handler.digest_io,
        "write_narrative",
        lambda n, **k: written.append(n) or f"narratives/x/{n.get('id')}.json",
    )

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
                        "is_new": True,
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
    assert body["keys"]  # narratives persisted to S3, not echoed inline
    narr = written[0]
    assert narr["citations"]
    assert narr["narrative"]
    assert narr.get("threat_score_note") == "estimated_v1"


def test_handler_context_only_digest_fuses_entities(monkeypatch):
    monkeypatch.setenv("ONCA_SYNTH_USE_LLM", "false")
    monkeypatch.setenv("ONCA_SYNTH_USE_KB", "false")
    # Opt out of emit-on-change: this test exercises the context-fusion path.
    # (By default the handler suppresses steady-state, change-less digests.)
    monkeypatch.setenv("ONCA_SYNTH_CHANGE_ONLY", "false")
    # Isolate fusion from the score threshold — steady-state context scores low
    # under the real model, which is correct but not what this test checks.
    monkeypatch.setenv("ONCA_SYNTH_MIN_SCORE", "0.0")
    written = []
    monkeypatch.setattr(
        lambda_handler.digest_io,
        "write_narrative",
        lambda n, **k: written.append(n.get("id")) or f"narratives/x/{n.get('id')}.json",
    )
    event = {
        "digest": {
            "regulatory": {"items": [], "context": [], "count": 49, "new_count": 0},
            "sec_filings": {
                "items": [],
                "context": [
                    {
                        "id": "sec:nu",
                        "ticker": "NU",
                        "form": "6-K",
                        "company": "Nu Holdings Ltd.",
                        "url": "https://www.sec.gov/nu",
                        "source": "SEC-EDGAR",
                    }
                ],
            },
            "ofertas": {
                "items": [],
                "context": [
                    {
                        "id": "of:1",
                        "issuer": "Issuer SA",
                        "leader": "BTG PACTUAL",
                        "security": "Debêntures",
                        "url": "https://dados.cvm.gov.br/of",
                    }
                ],
            },
            "market": {
                "items": [{"institution": "ITAU", "share_pct": 14.8, "value": 1e12}]
            },
            "inf_diario_moves": {
                "items": [],
                "context": [
                    {
                        "id": "inf:1",
                        "fund_name": "ITAÚ RF MASTER",
                        "admin": "ITAU UNIBANCO S.A.",
                        "pl": 1e11,
                        "url": "https://dados.cvm.gov.br/dataset/fi-doc-inf_diario",
                    }
                ],
            },
            "pix_moves": {"items": [], "context": []},
            "juros_moves": {"items": [], "context": []},
            "new_entrants": {"items": [], "context": []},
            "competitor": {"items": [], "context": []},
        }
    }
    body = json.loads(lambda_handler.lambda_handler(event, None)["body"])
    assert body["status"] == "ok"
    assert body["narrative_count"] >= 1
    assert body["fusion"]["entity_fusion"] >= 1 or body["fusion"]["multi_lens"] >= 1
    assert written


def test_handler_suppresses_changeless_digest_by_default(monkeypatch):
    """Emit-on-change default: a steady-state, change-less digest -> no narratives."""
    monkeypatch.setenv("ONCA_SYNTH_USE_LLM", "false")
    monkeypatch.setenv("ONCA_SYNTH_USE_KB", "false")
    monkeypatch.delenv("ONCA_SYNTH_CHANGE_ONLY", raising=False)  # default (on)
    event = {
        "digest": {
            "ofertas": {"items": [], "context": [
                {"id": "of:1", "issuer": "Issuer SA", "leader": "BTG PACTUAL",
                 "security": "Debêntures", "url": "https://dados.cvm.gov.br/of"}]},
            "inf_diario_moves": {"items": [], "context": [
                {"id": "inf:1", "fund_name": "BTG RF", "admin": "BTG PACTUAL",
                 "pl": 1e11, "url": "https://dados.cvm.gov.br/i"}]},
        }
    }
    body = json.loads(lambda_handler.lambda_handler(event, None)["body"])
    assert body["narrative_count"] == 0
    assert body["status"] == "ok_empty"


def test_handler_commits_news_seen_after_consuming(monkeypatch):
    """Deferred news-commit (#23): synth marks the fetched news ids seen only
    after it has consumed the digest."""
    monkeypatch.setenv("ONCA_SYNTH_USE_LLM", "false")
    monkeypatch.setenv("ONCA_SYNTH_USE_KB", "false")
    monkeypatch.setattr(
        lambda_handler.digest_io, "write_narrative", lambda n, **k: None
    )
    committed = {}
    import src.diff.engine as engine

    monkeypatch.setattr(engine, "DynamoDbState", lambda *a, **k: None)
    monkeypatch.setattr(
        engine, "commit_seen", lambda source, ids, **k: committed.update({source: list(ids)})
    )
    event = {
        "digest": {
            "regulatory": {"items": []},
            "news": {"items": [], "context": [], "fetched_ids": ["news:1", "news:2"]},
        }
    }
    lambda_handler.lambda_handler(event, None)
    assert committed == {"trade_press": ["news:1", "news:2"]}


def test_handler_no_news_ids_commits_nothing(monkeypatch):
    """No fetched_ids in the slice -> nothing to commit, no crash."""
    monkeypatch.setenv("ONCA_SYNTH_USE_LLM", "false")
    monkeypatch.setenv("ONCA_SYNTH_USE_KB", "false")
    monkeypatch.setattr(lambda_handler.digest_io, "write_narrative", lambda n, **k: None)
    calls = []
    import src.diff.engine as engine

    monkeypatch.setattr(engine, "commit_seen", lambda *a, **k: calls.append(a))
    body = json.loads(
        lambda_handler.lambda_handler({"digest": {"regulatory": {"items": []}}}, None)["body"]
    )
    assert calls == []
    assert body["fusion"]["news_committed"] == 0


def test_handler_no_digest(monkeypatch):
    monkeypatch.setattr(lambda_handler.digest_io, "load_digest_from_event", lambda e: None)
    resp = lambda_handler.lambda_handler({}, None)
    body = json.loads(resp["body"])
    assert body["status"] == "no_digest"
    assert body["narrative_count"] == 0
