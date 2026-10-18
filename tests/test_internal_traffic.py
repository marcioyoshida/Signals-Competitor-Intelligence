"""Our own traffic (QA personas, operator, owner) never counts as client usage — one shared test
for the mobile-usage gate and the ETS Engagement component / attention lists."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import decision_metrics, engagement_log, internal_traffic, mobile_usage  # noqa: E402


def _ev(tenant=None, actor="sub-1", action="open", kind="hoje", entity=None):
    return {"type": "engagement", "tenant": tenant, "actor": actor, "action": action, "kind": kind,
            "entity": entity, "industries": ["banking"], "officer": "cso", "created_at": "2026-10-18T10:00:00+00:00"}


def test_who_is_internal():
    assert internal_traffic.is_internal(_ev("qa-internal-admin"))
    assert internal_traffic.is_internal(_ev("operator"))
    assert internal_traffic.is_internal(_ev("owner-all-sectors"))
    assert internal_traffic.is_internal(_ev(None, actor="operator"))       # opkey operator mode: no tenant
    assert not internal_traffic.is_internal(_ev("acme-bank"))
    assert not internal_traffic.is_internal(_ev(None, actor="sub-9"))
    assert mobile_usage.is_internal is internal_traffic.is_internal        # one test, shared


def test_engagement_rollup_drops_internal_traffic_and_counts_it():
    events = [_ev("qa-internal-admin", entity="itau")] * 400 + [_ev(None, actor="operator", entity="itau")] * 300 \
        + [_ev("acme-bank", action="expand", kind="headline", entity="nubank")] * 3
    roll = engagement_log.aggregate(events, labels={"nubank": "Nubank", "itau": "Itaú"})
    assert roll["internal_excluded"] == 700
    assert roll["n_events"] == 3 and roll["n_interest"] == 3
    assert [x["entity"] for x in roll["top_entities"]] == ["nubank"]        # automation never tops the list


def test_engagement_component_is_not_saturated_by_automation():
    automation = [_ev("qa-internal-admin")] * 5000 + [_ev(None, actor="operator")] * 2000
    m = decision_metrics.compute_metrics([], engagement=engagement_log.aggregate(automation))
    assert m["ets_components"]["engagement"] is None                       # was 10.0 on automation alone


def test_trust_metrics_ignore_internal_decisions_but_the_store_workflows_still_see_them():
    op = {"type": "decision", "actor": "operator", "tenant": None, "officer": "cso", "status": "pending",
          "created_at": "2026-09-01T10:00:00+00:00", "decision_id": "d1"}
    qa = dict(op, actor="sub-qa", tenant="qa-internal-admin", decision_id="d2")
    m = decision_metrics.compute_metrics([op, qa])
    assert m["ets"] is None and m["n_decisions"] == 0                       # "sem dados", not 0.0
    assert all(v is None or v == 0 for v in m["ets_components"].values()) and m["ets_components"]["influence"] is None
    assert m["internal_decisions_excluded"] == 2 and m["by_officer"] == {}
    client = dict(op, actor="sub-9", tenant="acme-bank", decision_id="d3")
    m = decision_metrics.compute_metrics([op, client])
    assert m["n_decisions"] == 1 and m["internal_decisions_excluded"] == 1
