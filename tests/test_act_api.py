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


SECRET = "s3cr3t"


# WAF Phase 0: the origin gate is FAIL-CLOSED, so an unset ONCA_ORIGIN_SECRET
# now denies instead of disabling the check. Tests therefore set the secret and
# send the header CloudFront injects, rather than unsetting it to slip past.
def _event(body, headers=None, b64=False, method="POST", claims=None):
    ev = {
        "body": body if isinstance(body, str) else json.dumps(body),
        "headers": {"x-onca-origin": SECRET} if headers is None else headers,
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
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    resp = act_api.lambda_handler(_event({"intent": "trigger_run"}, headers={}), None)
    assert resp["statusCode"] == 403


def test_unset_origin_secret_denies_rather_than_disabling_the_gate(monkeypatch):
    """Fail-closed regression: dropping the env var must NOT publish the endpoint."""
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    resp = act_api.lambda_handler(_event({"intent": "trigger_run"}), None)
    assert resp["statusCode"] == 403


def test_get_advertises_catalog(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    resp = act_api.lambda_handler(_event("{}", method="GET"), None)
    assert resp["statusCode"] == 200
    cat = json.loads(resp["body"])["catalog"]
    assert cat["trigger_run"] == "apply"
    assert cat["propose_registry_change"] == "propose"


def test_operator_no_jwt_is_elevated(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    monkeypatch.setattr(er, "resolve_review", lambda rid, dec, payload=None: None)
    resp = act_api.lambda_handler(_event({"intent": "resolve_review",
                                          "args": {"review_id": "x", "decision": "approved"}}), None)
    # reaches the handler (409 noop), i.e. was authorized as operator
    assert resp["statusCode"] == 409


def test_non_elevated_jwt_is_forbidden(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    resp = act_api.lambda_handler(_event(
        {"intent": "trigger_run"},
        claims={"sub": "u1", "custom:tenant": "acme", "custom:tier": "entry"}), None)
    assert resp["statusCode"] == 403


def test_elevated_jwt_is_authorized(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    monkeypatch.setattr(er, "revert_entity_since", lambda eid, ts, **k: ["industries"])
    resp = act_api.lambda_handler(_event(
        {"intent": "revert_entity", "args": {"entity_id": "btg", "since_ts": "2026-01-01"}},
        claims={"sub": "u1", "cognito:groups": "operator"}), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["actor"] == "u1"


def test_sovereign_tier_alone_is_no_longer_elevated(monkeypatch):
    # ADR 016 addendum Decision 2: `tier` is a pricing/entitlement axis, not a
    # role grant — a tenant licensed at the `sovereign` tier but with no
    # elevated Cognito group must be refused, same as any other tenant.
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    resp = act_api.lambda_handler(_event(
        {"intent": "trigger_run"},
        claims={"sub": "u1", "custom:tier": "sovereign"}), None)
    assert resp["statusCode"] == 403


# ---- catalog / dispatch ------------------------------------------------------------
def test_unknown_intent_400(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    resp = act_api.lambda_handler(_event({"intent": "delete_everything"}), None)
    assert resp["statusCode"] == 400
    assert "catalog" in json.loads(resp["body"])


def test_invalid_json_400(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    assert act_api.lambda_handler(_event("not json"), None)["statusCode"] == 400


def test_trigger_run_applies(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
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
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    monkeypatch.delenv("ONCA_PIPELINE_ARN", raising=False)
    monkeypatch.delenv("ONCA_SCHEDULER_ROLE_ARN", raising=False)
    resp = act_api.lambda_handler(_event({"intent": "trigger_run"}), None)
    assert resp["statusCode"] == 500
    assert json.loads(resp["body"])["outcome"] == "blocked"


def test_resolve_review_applies(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
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
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    resp = act_api.lambda_handler(_event({"intent": "resolve_review",
                                          "args": {"review_id": "x", "decision": "maybe"}}), None)
    assert resp["statusCode"] == 400


def test_rollback_field_applies(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    monkeypatch.setattr(er, "rollback_field", lambda eid, f, ts, **k: True)
    resp = act_api.lambda_handler(_event(
        {"intent": "rollback_field",
         "args": {"entity_id": "btg", "field": "industries", "before_ts": "2026-01-01"}}), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["outcome"] == "applied"


def test_rollback_unsupported_field_400(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)

    def _raise(eid, f, ts, **k):
        raise ValueError("rollback unsupported for 'aliases'")

    monkeypatch.setattr(er, "rollback_field", _raise)
    resp = act_api.lambda_handler(_event(
        {"intent": "rollback_field",
         "args": {"entity_id": "btg", "field": "aliases", "before_ts": "2026-01-01"}}), None)
    assert resp["statusCode"] == 400


def test_propose_registry_change_is_proposed_not_applied(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
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
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
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
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
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
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    monkeypatch.setattr(er, "rollback_field", lambda eid, f, ts, **k: True)
    raw = base64.b64encode(json.dumps(
        {"intent": "rollback_field",
         "args": {"entity_id": "btg", "field": "parent", "before_ts": "2026-01-01"}}).encode()).decode()
    resp = act_api.lambda_handler(_event(raw, b64=True), None)
    assert resp["statusCode"] == 200


# ---- ADR 020 Phases 2–3: officers, scoping, hand-off, new actions ------------------
def test_get_advertises_officer_roster(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    resp = act_api.lambda_handler(_event("{}", method="GET"), None)
    roles = {o["role"] for o in json.loads(resp["body"])["officers"]}
    assert roles == {"strategic", "regulator", "compliance", "product"}


def test_officer_may_emit_its_own_action(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
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
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    # trigger_run is shared (strategic+regulator) but NOT in compliance's catalog and has
    # no single owner → compliance emitting it is rejected (no owner to hand off to).
    resp = act_api.lambda_handler(_event(
        {"intent": "trigger_run", "officer": "compliance"}), None)
    assert resp["statusCode"] == 403
    assert json.loads(resp["body"])["error"] == "intent not in officer catalog"


def test_hand_off_routes_to_the_owning_officer(monkeypatch):
    # The Regulator asks to roll back — an exclusively-Compliance action → handed off.
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
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
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    monkeypatch.setattr(er, "propose_review", lambda **kw: "REVIEW#vertical_proposal:x")
    resp = act_api.lambda_handler(_event(
        {"intent": "propose_vertical", "args": {"name": "Câmbio", "rationale": "demanda"}}), None)
    assert resp["statusCode"] == 202
    assert json.loads(resp["body"])["officer"] == "product"


def test_unknown_officer_400(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    resp = act_api.lambda_handler(_event(
        {"intent": "trigger_run", "officer": "nobody"}), None)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"] == "unknown officer"


def test_flag_entity_proposes(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    calls = {}
    monkeypatch.setattr(er, "propose_review", lambda **kw: calls.update(kw) or "REVIEW#compliance_flag:x")
    resp = act_api.lambda_handler(_event(
        {"intent": "flag_entity", "officer": "compliance",
         "args": {"entity_id": "x", "reason": "consta CEIS"}}), None)
    assert resp["statusCode"] == 202
    assert calls["kind"] == "compliance_flag"


def test_open_watch_applies(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
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
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
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
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    resp = act_api.lambda_handler(_event(
        {"intent": "record_decision",
         "args": {"officer": "cso", "recommendation": "x", "verdict": "talvez"}}), None)
    assert resp["statusCode"] == 400


def test_set_outcome_applies(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
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


def test_record_engagement_applies_and_is_not_journaled(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    monkeypatch.setattr(act_api, "_get_act", lambda *a, **k: None)
    monkeypatch.setattr(act_api, "_put_act", lambda *a, **k: None)
    logged = []
    monkeypatch.setattr(er, "_log", lambda *a, **k: logged.append(a))
    from src.synth import engagement_log
    monkeypatch.setattr(engagement_log, "record_engagement",
                        lambda **kw: {"engagement_id": "e1", "kind": kw["kind"], "action": kw.get("action") or "expand"})
    resp = act_api.lambda_handler(_event(
        {"intent": "record_engagement", "officer": "cso",
         "args": {"kind": "headline", "action": "expand", "card_id": "n1", "entity": "itau",
                  "officer": "cso", "sector": "banking"}}), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["outcome"] == "applied"
    assert logged == []  # telemetry is NOT written to the curation journal
    assert "record_engagement" in act_api._NO_JOURNAL


def test_set_board_adoption_applies(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    from src.synth import decision_log
    monkeypatch.setattr(decision_log, "set_board_adoption",
                        lambda did, adopted, **k: {"board_adopted": adopted} if did == "d1" else None)
    ok = act_api.lambda_handler(_event(
        {"intent": "set_board_adoption", "args": {"decision_id": "d1", "adopted": True}}), None)
    assert ok["statusCode"] == 200 and json.loads(ok["body"])["board_adopted"] is True
    missing = act_api.lambda_handler(_event(
        {"intent": "set_board_adoption", "args": {"decision_id": "nope"}}), None)
    assert missing["statusCode"] == 404


def test_set_tdr_baseline_applies_and_validates(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    from src.synth import decision_log
    monkeypatch.setattr(decision_log, "set_tdr_baseline",
                        lambda hours, **k: {"baseline_hours": float(hours), "set_at": "2026-09-06T00:00:00+00:00"})
    ok = act_api.lambda_handler(_event(
        {"intent": "set_tdr_baseline", "officer": "cso", "args": {"hours": 16}}), None)
    assert ok["statusCode"] == 200 and json.loads(ok["body"])["baseline_hours"] == 16.0
    assert "set_tdr_baseline" in act_api.catalog()

    def _raise(hours, **k):
        raise ValueError("baseline hours must be > 0")
    monkeypatch.setattr(decision_log, "set_tdr_baseline", _raise)
    bad = act_api.lambda_handler(_event(
        {"intent": "set_tdr_baseline", "officer": "cso", "args": {"hours": 0}}), None)
    assert bad["statusCode"] == 400


def test_append_reference_applies(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    from src.synth import decision_log
    seen = {}
    monkeypatch.setattr(decision_log, "append_reference",
                        lambda did, url, officer=None: seen.update(did=did, url=url) or True)
    resp = act_api.lambda_handler(_event(
        {"intent": "append_reference", "officer": "cso",
         "args": {"decision_id": "d1", "url": "https://bcb.gov.br/x", "officer": "cso"}}), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["outcome"] == "applied"
    assert seen == {"did": "d1", "url": "https://bcb.gov.br/x"}


def test_append_reference_missing_decision_404(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    from src.synth import decision_log
    monkeypatch.setattr(decision_log, "append_reference", lambda *a, **k: False)
    resp = act_api.lambda_handler(_event(
        {"intent": "append_reference", "args": {"decision_id": "nope", "url": "https://x/1"}}), None)
    assert resp["statusCode"] == 404


def test_run_integrity_audit_reads_only(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
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


def test_action_links_back_to_decision(monkeypatch):
    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    from src.synth import decision_log
    linked = {}
    monkeypatch.setattr(decision_log, "link_action",
                        lambda did, **k: linked.update(did=did, intent=k.get("intent"), outcome=k.get("outcome")) or True)
    # resolve_review carrying a decision_id → closes the decision→action loop
    monkeypatch.setattr(er, "resolve_review", lambda rid, dec, payload=None: {"kind": "discovery", "status": dec})
    resp = act_api.lambda_handler(_event(
        {"intent": "resolve_review", "officer": "cpo",
         "args": {"review_id": "discovery:x", "decision": "approved", "decision_id": "d9"}}), None)
    assert resp["statusCode"] == 200
    assert linked == {"did": "d9", "intent": "resolve_review", "outcome": "applied"}
    # a decision-management intent must NOT self-link
    linked.clear()
    monkeypatch.setattr(decision_log, "set_board_adoption", lambda did, adopted, **k: {"board_adopted": adopted})
    act_api.lambda_handler(_event(
        {"intent": "set_board_adoption", "officer": "cco", "args": {"decision_id": "d9", "adopted": True}}), None)
    assert linked == {}  # excluded from self-referential linking


# ---- #159 CPO Product Radar subjects (curation admin) ------------------------------
NU_APP = "814456780"
NU_CH = "UCgsDX3hTwiPdtGHJjMFfDxg"


def _radar_registry(monkeypatch):
    """In-memory registry with two plain entities; journal/idempotency stubbed off."""
    from tests.test_entity_registry import FakeTable

    _no_journal(monkeypatch)
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    monkeypatch.delenv("ONCA_CURATION_ACTOR", raising=False)
    t = FakeTable()
    monkeypatch.setattr(er, "_table", lambda table=None: t if table is None else table)
    er.put_entity("nubank", "Nubank", ["Nubank"], table=t)
    er.put_entity("inter", "Banco Inter", ["Banco Inter"], table=t)
    return t


def _act(intent, args, claims=None, idem=None):
    body = {"intent": intent, "args": args}
    if idem:
        body["idempotency_key"] = idem
    resp = act_api.lambda_handler(_event(body, claims=claims), None)
    return resp["statusCode"], json.loads(resp["body"])


def _nu_args(**over):
    a = {"entity_id": "nubank", "name": "Nubank", "aliases": "Nubank, Nu Bank, NuConta",
         "search_query": "Nubank", "namesake_risk": "never match bare 'nu'",
         "apple_app_ids": NU_APP, "youtube_channels": NU_CH}
    a.update(over)
    return a


def test_radar_intents_are_in_the_catalog(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", SECRET)
    cat = json.loads(act_api.lambda_handler(_event("{}", method="GET"), None)["body"])["catalog"]
    for i in ("set_product_radar", "pause_product_radar", "resume_product_radar",
              "remove_product_radar", "list_product_radar"):
        assert cat[i] == "apply"


def test_set_product_radar_creates_curated_config_with_radar_shape(monkeypatch):
    _radar_registry(monkeypatch)
    code, body = _act("set_product_radar", _nu_args())
    assert code == 200 and body["outcome"] == "applied" and body["created"] is True
    ent = er.get_entity("nubank")
    pr = ent["product_radar"]
    assert pr["aliases"] == ["Nubank", "Nu Bank", "NuConta"]
    # cpo_radar formats a['name'] and ch['handle'] — both must exist on form-added ids
    assert pr["apple_app_ids"] == [{"id": NU_APP, "name": "Nubank"}]
    assert pr["youtube_channels"] == [{"id": NU_CH, "handle": "", "title": "Nubank"}]
    assert pr["active"] is True
    assert ent["_prov"]["product_radar"]["source"] == "curated"      # a curator write
    subs = er.list_product_radar_subjects()
    assert [s["id"] for s in subs] == ["nubank"] and subs[0]["onca_entities"] == ["nubank"]


def test_set_product_radar_is_idempotent_noop_on_same_config(monkeypatch):
    _radar_registry(monkeypatch)
    assert _act("set_product_radar", _nu_args())[1]["outcome"] == "applied"
    code, body = _act("set_product_radar", _nu_args())
    assert code == 200 and body["outcome"] == "noop"


def test_set_product_radar_idempotency_key_replays_without_reapplying(monkeypatch):
    _radar_registry(monkeypatch)
    store = {}
    monkeypatch.setattr(act_api, "_get_act", lambda k, table=None: store.get(k))
    monkeypatch.setattr(act_api, "_put_act",
                        lambda k, s, r, table=None: store.__setitem__(k, {"status": s, "result": r}))
    calls = []
    real = er.set_product_radar
    monkeypatch.setattr(er, "set_product_radar", lambda *a, **k: calls.append(1) or real(*a, **k))
    c1, b1 = _act("set_product_radar", _nu_args(), idem="radar-k1")
    c2, b2 = _act("set_product_radar", _nu_args(), idem="radar-k1")
    assert c1 == c2 == 200 and b1["outcome"] == b2["outcome"] == "applied"
    assert b2["idempotent_replay"] is True and len(calls) == 1


def test_set_product_radar_without_app_or_channel_is_400(monkeypatch):
    _radar_registry(monkeypatch)
    code, body = _act("set_product_radar", _nu_args(apple_app_ids="", youtube_channels=[]))
    assert code == 400 and body["outcome"] == "blocked"
    assert "apple_app_ids or youtube_channels" in body["error"]
    assert not er.get_entity("nubank").get("product_radar")


def test_set_product_radar_rejects_malformed_ids_and_missing_entity(monkeypatch):
    _radar_registry(monkeypatch)
    assert _act("set_product_radar", _nu_args(apple_app_ids="nubank-app"))[0] == 400
    assert _act("set_product_radar", _nu_args(youtube_channels="@nubank"))[0] == 400
    assert _act("set_product_radar", _nu_args(entity_id=""))[0] == 400
    code, body = _act("set_product_radar", _nu_args(entity_id="ghost"))
    assert code == 404 and body["outcome"] == "blocked"


def test_set_product_radar_partial_edit_keeps_unsent_fields_and_names(monkeypatch):
    t = _radar_registry(monkeypatch)
    er.set_product_radar("nubank", {
        "name": "Nubank", "apple_app_ids": [{"id": NU_APP, "name": "Nubank: Conta"}],
        "youtube_channels": [{"id": NU_CH, "handle": "@nubank", "title": "Nubank"}],
        "related_entities": ["nu_holdings"], "active": False}, table=t)
    code, body = _act("set_product_radar", {"entity_id": "nubank", "search_query": "\"Nubank\"",
                                            "apple_app_ids": [NU_APP], "youtube_channels": NU_CH})
    assert code == 200 and body["outcome"] == "applied"
    pr = er.get_entity("nubank")["product_radar"]
    assert pr["search_query"] == "\"Nubank\""
    assert pr["apple_app_ids"] == [{"id": NU_APP, "name": "Nubank: Conta"}]          # name kept
    assert pr["youtube_channels"][0]["handle"] == "@nubank"                            # handle kept
    assert pr["related_entities"] == ["nu_holdings"] and pr["active"] is False         # not reset


def test_pause_keeps_config_and_resume_restores(monkeypatch):
    _radar_registry(monkeypatch)
    _act("set_product_radar", _nu_args())
    before = dict(er.get_entity("nubank")["product_radar"])
    code, body = _act("pause_product_radar", {"entity_id": "nubank"})
    assert code == 200 and body["outcome"] == "applied" and body["active"] is False
    paused = er.get_entity("nubank")["product_radar"]
    assert paused["active"] is False
    assert {k: v for k, v in paused.items() if k != "active"} == \
        {k: v for k, v in before.items() if k != "active"}                            # config kept
    assert er.list_product_radar_subjects() == []                                     # radar stops
    listed = _act("list_product_radar", {})[1]
    assert listed["count"] == 1 and listed["subjects"][0]["active"] is False          # admin sees it
    assert _act("pause_product_radar", {"entity_id": "nubank"})[1]["outcome"] == "noop"
    code, body = _act("resume_product_radar", {"entity_id": "nubank"})
    assert code == 200 and body["outcome"] == "applied"
    assert er.get_entity("nubank")["product_radar"] == before


def test_pause_without_config_404_and_remove_is_idempotent(monkeypatch):
    _radar_registry(monkeypatch)
    code, body = _act("pause_product_radar", {"entity_id": "inter"})
    assert code == 404 and body["outcome"] == "noop"
    _act("set_product_radar", _nu_args())
    code, body = _act("remove_product_radar", {"entity_id": "nubank"})
    assert code == 200 and body["outcome"] == "applied"
    assert not er.get_entity("nubank").get("product_radar")
    assert er.get_entity("nubank")["display_name"] == "Nubank"                      # entity intact
    assert _act("remove_product_radar", {"entity_id": "nubank"})[1]["outcome"] == "noop"
    assert _act("remove_product_radar", {"entity_id": "ghost"})[0] == 404


def test_radar_writes_require_elevated_group(monkeypatch):
    _radar_registry(monkeypatch)
    for intent, args in (("set_product_radar", _nu_args()), ("list_product_radar", {}),
                         ("pause_product_radar", {"entity_id": "nubank"}),
                         ("remove_product_radar", {"entity_id": "nubank"})):
        code, _ = _act(intent, args, claims={"sub": "u1", "custom:tier": "sovereign"})
        assert code == 403, intent
    assert not er.get_entity("nubank").get("product_radar")
    code, body = _act("set_product_radar", _nu_args(), claims={"sub": "u9", "cognito:groups": "operator"})
    assert code == 200 and body["actor"] == "u9"


def test_radar_write_is_journaled_under_the_caller(monkeypatch):
    _radar_registry(monkeypatch)
    rows = []
    monkeypatch.setattr(er, "_log", lambda eid, action, source, detail=None:
                        rows.append((eid, action, source, act_api.os.environ.get("ONCA_CURATION_ACTOR"))))
    _act("set_product_radar", _nu_args(), claims={"sub": "u9", "cognito:groups": "admin"})
    assert ("nubank", "set_product_radar", "curated", "u9") in rows                 # registry row
    assert any(r[0] == "nubank" and r[1] == "act:set_product_radar" for r in rows)   # act journal
    assert "ONCA_CURATION_ACTOR" not in act_api.os.environ                           # restored
    rows.clear()
    _act("list_product_radar", {})
    assert rows == []                                                                # reads unjournaled
