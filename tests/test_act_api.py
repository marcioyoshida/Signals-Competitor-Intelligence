"""ADR 020 Phase 1 — `/api/act` write contract: authz, catalog, propose-vs-apply,
idempotency, and journaling. No officers yet (Phase 2)."""
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3

from src.dashboard import act_api
from src.synth import entity_registry as er


def _event(body, headers=None, b64=False, method="POST", claims=None):
    ev = {
        "body": body if isinstance(body, str) else json.dumps(body),
        "headers": headers or {},
        "isBase64Encoded": b64,
        "requestContext": {"http": {"method": method}},
    }
    if claims is not None:
        ev["requestContext"]["authorizer"] = {"jwt": {"claims": claims}}
    return ev


def _no_journal(monkeypatch):
    """Journal + idempotency store are best-effort DB writes; stub them off by default."""
    monkeypatch.setattr(er, "_log", lambda *a, **k: None)
    monkeypatch.setattr(act_api, "_get_act", lambda *a, **k: None)
    monkeypatch.setattr(act_api, "_put_act", lambda *a, **k: None)


# ---- edge / authorization ----------------------------------------------------------
def test_rejects_direct_call_without_origin_secret(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", "s3cr3t")
    resp = act_api.lambda_handler(_event({"intent": "trigger_run"}), None)
    assert resp["statusCode"] == 403


def test_get_advertises_catalog(monkeypatch):
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    resp = act_api.lambda_handler(_event("{}", method="GET"), None)
    assert resp["statusCode"] == 200
    cat = json.loads(resp["body"])["catalog"]
    assert cat["trigger_run"] == "apply"
    assert cat["propose_registry_change"] == "propose"


def test_operator_no_jwt_is_elevated(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    monkeypatch.setattr(er, "resolve_review", lambda rid, dec, payload=None: None)
    resp = act_api.lambda_handler(_event({"intent": "resolve_review",
                                          "args": {"review_id": "x", "decision": "approved"}}), None)
    # reaches the handler (409 noop), i.e. was authorized as operator
    assert resp["statusCode"] == 409


def test_non_elevated_jwt_is_forbidden(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    resp = act_api.lambda_handler(_event(
        {"intent": "trigger_run"},
        claims={"sub": "u1", "custom:tenant": "acme", "custom:tier": "entry"}), None)
    assert resp["statusCode"] == 403


def test_elevated_jwt_is_authorized(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    monkeypatch.setattr(er, "revert_entity_since", lambda eid, ts, **k: ["industries"])
    resp = act_api.lambda_handler(_event(
        {"intent": "revert_entity", "args": {"entity_id": "btg", "since_ts": "2026-01-01"}},
        claims={"sub": "u1", "custom:tier": "sovereign"}), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["actor"] == "u1"


# ---- catalog / dispatch ------------------------------------------------------------
def test_unknown_intent_400(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    resp = act_api.lambda_handler(_event({"intent": "delete_everything"}), None)
    assert resp["statusCode"] == 400
    assert "catalog" in json.loads(resp["body"])


def test_invalid_json_400(monkeypatch):
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    assert act_api.lambda_handler(_event("not json"), None)["statusCode"] == 400


def test_trigger_run_applies(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    monkeypatch.setenv("ONCA_PIPELINE_ARN", "arn:sfn:pipeline")
    monkeypatch.setenv("ONCA_SCHEDULER_ROLE_ARN", "arn:iam:role")

    class _Sch:
        class exceptions:
            class ConflictException(Exception):
                pass

        def create_schedule(self, **kw):
            return {}

    monkeypatch.setattr(boto3, "client", lambda *a, **k: _Sch())
    resp = act_api.lambda_handler(_event({"intent": "trigger_run"}), None)
    assert resp["statusCode"] == 200
    b = json.loads(resp["body"])
    assert b["outcome"] == "applied" and b["execution_class"] == "apply"


def test_trigger_run_unconfigured_blocked(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    monkeypatch.delenv("ONCA_PIPELINE_ARN", raising=False)
    monkeypatch.delenv("ONCA_SCHEDULER_ROLE_ARN", raising=False)
    resp = act_api.lambda_handler(_event({"intent": "trigger_run"}), None)
    assert resp["statusCode"] == 500
    assert json.loads(resp["body"])["outcome"] == "blocked"


def test_resolve_review_applies(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    seen = {}
    monkeypatch.setattr(er, "resolve_review",
                        lambda rid, dec, payload=None: seen.update(rid=rid, dec=dec, p=payload)
                        or {"kind": "discovery", "status": dec})
    resp = act_api.lambda_handler(_event(
        {"intent": "resolve_review",
         "args": {"review_id": "discovery:zignet", "decision": "approved",
                  "industries": ["fintech"]}}), None)
    assert resp["statusCode"] == 200
    assert seen["rid"] == "discovery:zignet" and seen["p"] == {"industries": ["fintech"]}
    assert json.loads(resp["body"])["outcome"] == "applied"


def test_resolve_review_bad_args_400(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    resp = act_api.lambda_handler(_event({"intent": "resolve_review",
                                          "args": {"review_id": "x", "decision": "maybe"}}), None)
    assert resp["statusCode"] == 400


def test_rollback_field_applies(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    monkeypatch.setattr(er, "rollback_field", lambda eid, f, ts, **k: True)
    resp = act_api.lambda_handler(_event(
        {"intent": "rollback_field",
         "args": {"entity_id": "btg", "field": "industries", "before_ts": "2026-01-01"}}), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["outcome"] == "applied"


def test_rollback_unsupported_field_400(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)

    def _raise(eid, f, ts, **k):
        raise ValueError("rollback unsupported for 'aliases'")

    monkeypatch.setattr(er, "rollback_field", _raise)
    resp = act_api.lambda_handler(_event(
        {"intent": "rollback_field",
         "args": {"entity_id": "btg", "field": "aliases", "before_ts": "2026-01-01"}}), None)
    assert resp["statusCode"] == 400


def test_propose_registry_change_is_proposed_not_applied(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    calls = {}
    monkeypatch.setattr(er, "propose_review",
                        lambda **kw: calls.update(kw) or "REVIEW#act_registry:btg:industries")
    resp = act_api.lambda_handler(_event(
        {"intent": "propose_registry_change",
         "args": {"entity_id": "btg", "field": "industries", "value": ["banking"],
                  "reason": "misclassified"}}), None)
    assert resp["statusCode"] == 202
    b = json.loads(resp["body"])
    assert b["outcome"] == "proposed" and b["execution_class"] == "propose"
    assert calls["kind"] == "act_registry"


# ---- idempotency + journaling ------------------------------------------------------
def test_idempotent_replay_returns_stored_result(monkeypatch):
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    monkeypatch.setattr(er, "_log", lambda *a, **k: None)
    store: dict[str, dict] = {}
    monkeypatch.setattr(act_api, "_get_act", lambda k, table=None: store.get(k))
    monkeypatch.setattr(act_api, "_put_act",
                        lambda k, status, r, table=None: store.__setitem__(k, {"status": status, "result": r}))
    calls = {"n": 0}

    def _revert(eid, ts, **k):
        calls["n"] += 1
        return ["parent"]

    monkeypatch.setattr(er, "revert_entity_since", _revert)
    body = {"intent": "revert_entity",
            "args": {"entity_id": "btg", "since_ts": "2026-01-01"},
            "idempotency_key": "req-42"}
    r1 = act_api.lambda_handler(_event(body), None)
    r2 = act_api.lambda_handler(_event(body), None)
    assert r1["statusCode"] == 200 and r2["statusCode"] == 200
    assert calls["n"] == 1  # handler ran exactly once
    assert json.loads(r2["body"]).get("idempotent_replay") is True


def test_journals_every_call(monkeypatch):
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    monkeypatch.setattr(act_api, "_get_act", lambda *a, **k: None)
    monkeypatch.setattr(act_api, "_put_act", lambda *a, **k: None)
    logged = []
    monkeypatch.setattr(er, "_log", lambda subj, action, src, detail: logged.append((subj, action, src, detail)))
    monkeypatch.setattr(er, "rollback_field", lambda eid, f, ts, **k: True)
    act_api.lambda_handler(_event(
        {"intent": "rollback_field",
         "args": {"entity_id": "btg", "field": "parent", "before_ts": "2026-01-01"}}), None)
    assert logged and logged[0][0] == "btg"
    assert logged[0][1] == "act:rollback_field"
    assert logged[0][3]["outcome"] == "applied"


def test_base64_body(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    monkeypatch.setattr(er, "rollback_field", lambda eid, f, ts, **k: True)
    raw = base64.b64encode(json.dumps(
        {"intent": "rollback_field",
         "args": {"entity_id": "btg", "field": "parent", "before_ts": "2026-01-01"}}).encode()).decode()
    resp = act_api.lambda_handler(_event(raw, b64=True), None)
    assert resp["statusCode"] == 200


# ---- ADR 020 Phases 2–3: officers, scoping, hand-off, new actions ------------------
def test_get_advertises_officer_roster(monkeypatch):
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    resp = act_api.lambda_handler(_event("{}", method="GET"), None)
    roles = {o["role"] for o in json.loads(resp["body"])["officers"]}
    assert roles == {"strategic", "regulator", "compliance", "product"}


def test_officer_may_emit_its_own_action(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    monkeypatch.setattr(er, "propose_review", lambda **kw: "REVIEW#belief_bullet:btg")
    resp = act_api.lambda_handler(_event(
        {"intent": "curate_belief", "officer": "strategic",
         "args": {"entity_id": "btg", "bullet": "avança em atacado", "axis": "strength"}}), None)
    assert resp["statusCode"] == 202
    b = json.loads(resp["body"])
    assert b["outcome"] == "proposed" and b["officer"] == "strategic"
    assert "handoff" not in b


def test_officer_out_of_catalog_shared_action_is_rejected(monkeypatch):
    # curate_belief is exclusively strategic's; the regulator cannot emit it and it is
    # NOT handed off to strategic (that IS a hand-off — tested below); here we assert the
    # regulator emitting a compliance-only action hands off, but its OWN non-owned reject.
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    # trigger_run is shared (strategic+regulator) but NOT in compliance's catalog and has
    # no single owner → compliance emitting it is rejected (no owner to hand off to).
    resp = act_api.lambda_handler(_event(
        {"intent": "trigger_run", "officer": "compliance"}), None)
    assert resp["statusCode"] == 403
    assert json.loads(resp["body"])["error"] == "intent not in officer catalog"


def test_hand_off_routes_to_the_owning_officer(monkeypatch):
    # The Regulator asks to roll back — an exclusively-Compliance action → handed off.
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    monkeypatch.setattr(er, "rollback_field", lambda eid, f, ts, **k: True)
    resp = act_api.lambda_handler(_event(
        {"intent": "rollback_field", "officer": "regulator",
         "args": {"entity_id": "btg", "field": "industries", "before_ts": "2026-01-01"}}), None)
    assert resp["statusCode"] == 200
    b = json.loads(resp["body"])
    assert b["officer"] == "compliance"
    assert b["handoff"] == {"from": "regulator", "to": "compliance"}


def test_auto_route_picks_owner_when_no_officer(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    monkeypatch.setattr(er, "propose_review", lambda **kw: "REVIEW#vertical_proposal:x")
    resp = act_api.lambda_handler(_event(
        {"intent": "propose_vertical", "args": {"name": "Câmbio", "rationale": "demanda"}}), None)
    assert resp["statusCode"] == 202
    assert json.loads(resp["body"])["officer"] == "product"


def test_unknown_officer_400(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    resp = act_api.lambda_handler(_event(
        {"intent": "trigger_run", "officer": "nobody"}), None)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"] == "unknown officer"


def test_flag_entity_proposes(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    calls = {}
    monkeypatch.setattr(er, "propose_review", lambda **kw: calls.update(kw) or "REVIEW#compliance_flag:x")
    resp = act_api.lambda_handler(_event(
        {"intent": "flag_entity", "officer": "compliance",
         "args": {"entity_id": "x", "reason": "consta CEIS"}}), None)
    assert resp["statusCode"] == 202
    assert calls["kind"] == "compliance_flag"


def test_open_watch_applies(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    puts = []

    class _T:
        def put_item(self, Item):
            puts.append(Item)

    monkeypatch.setattr(er, "_table", lambda table=None: _T())
    resp = act_api.lambda_handler(_event(
        {"intent": "open_watch", "officer": "regulator",
         "args": {"target": "Resolução CVM 175", "kind": "instrument"}}), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["outcome"] == "applied"
    assert puts and puts[0]["type"] == "watch"


def test_record_decision_applies_for_any_officer(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    from src.synth import decision_log
    monkeypatch.setattr(decision_log, "record_decision",
                        lambda **kw: {"decision_id": "d1", "verdict": kw["verdict"],
                                      "officer": kw.get("officer"), "industry": kw.get("industry")})
    resp = act_api.lambda_handler(_event(
        {"intent": "record_decision", "officer": "cso",
         "args": {"officer": "cso", "recommendation": "Abrir watch em Itaú",
                  "verdict": "aprovado", "industry": "banking", "action_ref": "open_watch"}}), None)
    assert resp["statusCode"] == 200
    b = json.loads(resp["body"])
    assert b["outcome"] == "applied" and b["decision_id"] == "d1" and b["officer"] == "cso"


def test_record_decision_bad_verdict_400(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    resp = act_api.lambda_handler(_event(
        {"intent": "record_decision",
         "args": {"officer": "cso", "recommendation": "x", "verdict": "talvez"}}), None)
    assert resp["statusCode"] == 400


def test_set_outcome_applies(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    from src.synth import decision_log
    monkeypatch.setattr(decision_log, "set_outcome",
                        lambda did, outcome, **k: {"outcome": outcome} if did == "d1" else None)
    ok = act_api.lambda_handler(_event(
        {"intent": "set_outcome", "args": {"decision_id": "d1", "outcome": "favoravel"}}), None)
    ob = json.loads(ok["body"])
    assert ok["statusCode"] == 200 and ob["outcome"] == "applied" and ob["decision_outcome"] == "favoravel"
    missing = act_api.lambda_handler(_event(
        {"intent": "set_outcome", "args": {"decision_id": "nope", "outcome": "favoravel"}}), None)
    assert missing["statusCode"] == 404


def test_run_integrity_audit_reads_only(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    monkeypatch.delenv("ONCA_SITE_BUCKET", raising=False)
    monkeypatch.setattr(er, "list_entities", lambda *a, **k: [])
    from src.synth import integrity
    monkeypatch.setattr(integrity, "audit",
                        lambda feed, ents: {"total": 0, "counts": {}, "findings": []})
    resp = act_api.lambda_handler(_event(
        {"intent": "run_integrity_audit", "officer": "compliance"}), None)
    assert resp["statusCode"] == 200
    b = json.loads(resp["body"])
    assert b["outcome"] == "applied" and b["total"] == 0
