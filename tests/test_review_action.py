import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3

from src.dashboard import review_action as ra
from src.synth import curate
from src.synth import entity_registry as er


def _stub_s3(monkeypatch):
    """No real AWS: the handler builds an s3 client before delegating to curate.vet."""
    monkeypatch.setattr(boto3, "client", lambda *a, **k: object())


SECRET = "s3cr3t"


# WAF Phase 0: the origin gate is FAIL-CLOSED, so an unset ONCA_ORIGIN_SECRET
# now denies instead of disabling the check. Tests therefore set the secret and
# send the header CloudFront injects, rather than unsetting it to slip past.
def _event(body, headers=None, b64=False):
    return {"body": body,
            "headers": {"x-onca-origin": SECRET} if headers is None else headers,
            "isBase64Encoded": b64}


def test_rejects_direct_call_without_origin_secret(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    resp = ra.lambda_handler(
        _event(json.dumps({"review_id": "x", "decision": "approved"}), headers={}), None)
    assert resp["statusCode"] == 403


def test_unset_origin_secret_denies_rather_than_disabling_the_gate(monkeypatch):
    """Fail-closed regression: dropping the env var must NOT publish the endpoint."""
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    resp = ra.lambda_handler(_event(json.dumps({"review_id": "x", "decision": "approved"})), None)
    assert resp["statusCode"] == 403


def test_bad_input_returns_400(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    assert ra.lambda_handler(_event("not json"), None)["statusCode"] == 400
    assert ra.lambda_handler(_event(json.dumps({"review_id": "x", "decision": "maybe"})), None)["statusCode"] == 400
    assert ra.lambda_handler(_event(json.dumps({"decision": "approved"})), None)["statusCode"] == 400


def test_approve_calls_resolve_and_returns_status(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    monkeypatch.delenv("ONCA_FEED_BUILDER_NAME", raising=False)
    seen = {}
    monkeypatch.setattr(er, "resolve_review", lambda rid, dec, payload=None: seen.update(rid=rid, dec=dec, payload=payload) or {"status": dec})
    resp = ra.lambda_handler(_event(json.dumps({"review_id": "group_merge:a_b", "decision": "approved"})), None)
    assert resp["statusCode"] == 200
    assert seen == {"rid": "group_merge:a_b", "dec": "approved", "payload": None}
    assert json.loads(resp["body"])["status"] == "approved"


def test_already_decided_returns_409(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    monkeypatch.setattr(er, "resolve_review", lambda rid, dec, payload=None: None)
    resp = ra.lambda_handler(_event(json.dumps({"review_id": "x", "decision": "rejected"})), None)
    assert resp["statusCode"] == 409


def test_accepts_base64_body(monkeypatch):
    import base64
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    monkeypatch.delenv("ONCA_FEED_BUILDER_NAME", raising=False)
    monkeypatch.setattr(er, "resolve_review", lambda rid, dec, payload=None: {"status": dec})
    body = base64.b64encode(json.dumps({"review_id": "x", "decision": "approved"}).encode()).decode()
    resp = ra.lambda_handler(_event(body, b64=True), None)
    assert resp["statusCode"] == 200


def test_industry_approval_requires_industries(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    called = {"n": 0}
    monkeypatch.setattr(er, "resolve_review", lambda rid, dec, payload=None: called.update(n=called["n"] + 1) or {"status": dec})
    # approving an industry review with no picked module is rejected before the write
    resp = ra.lambda_handler(_event(json.dumps({"review_id": "industry:x", "decision": "approved", "kind": "industry"})), None)
    assert resp["statusCode"] == 400
    assert called["n"] == 0


def test_industry_approval_passes_payload(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    monkeypatch.delenv("ONCA_FEED_BUILDER_NAME", raising=False)
    seen = {}
    monkeypatch.setattr(er, "resolve_review", lambda rid, dec, payload=None: seen.update(payload=payload) or {"status": dec})
    resp = ra.lambda_handler(_event(json.dumps({
        "review_id": "industry:x", "decision": "approved", "kind": "industry",
        "industries": ["fintech", " "],
    })), None)
    assert resp["statusCode"] == 200
    assert seen["payload"] == {"industries": ["fintech"]}


# --- Phase C: proposal vetting branch --------------------------------------
def test_proposal_vetting_dispatches_to_curate(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    monkeypatch.delenv("ONCA_FEED_BUILDER_NAME", raising=False)
    monkeypatch.setenv("ONCA_DIGESTS_BUCKET", "onca-digests")
    _stub_s3(monkeypatch)
    seen = {}
    monkeypatch.setattr(curate, "vet", lambda bucket, pid, dec, *, queue, s3:
                        seen.update(bucket=bucket, pid=pid, dec=dec, queue=queue)
                        or {"status": dec, "proposal_id": pid})
    resp = ra.lambda_handler(_event(json.dumps({
        "proposal_id": "seed:nubank:S:abc", "queue": "swot", "decision": "approved"})), None)
    assert resp["statusCode"] == 200
    assert seen == {"bucket": "onca-digests", "pid": "seed:nubank:S:abc",
                    "dec": "approved", "queue": "swot"}
    assert json.loads(resp["body"])["status"] == "approved"


def test_proposal_vetting_bad_input(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    monkeypatch.setenv("ONCA_DIGESTS_BUCKET", "onca-digests")
    # missing queue
    assert ra.lambda_handler(_event(json.dumps({
        "proposal_id": "x", "decision": "approved"})), None)["statusCode"] == 400
    # bad decision
    assert ra.lambda_handler(_event(json.dumps({
        "proposal_id": "x", "queue": "swot", "decision": "maybe"})), None)["statusCode"] == 400


def test_proposal_vetting_noop_returns_409(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    monkeypatch.setenv("ONCA_DIGESTS_BUCKET", "onca-digests")
    _stub_s3(monkeypatch)
    monkeypatch.setattr(curate, "vet", lambda *a, **k: {"status": "noop", "detail": "missing"})
    resp = ra.lambda_handler(_event(json.dumps({
        "proposal_id": "ghost", "queue": "swot", "decision": "approved"})), None)
    assert resp["statusCode"] == 409


def test_proposal_vetting_honors_origin_secret(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", "s3cr3t")
    resp = ra.lambda_handler(_event(json.dumps({
        "proposal_id": "x", "queue": "swot", "decision": "approved"}), headers={}), None)
    assert resp["statusCode"] == 403
