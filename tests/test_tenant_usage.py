"""#288 — "Uso por cliente": per-tenant adoption rollup (operator only). Fixture tenants only."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import feed_builder  # noqa: E402
from src.synth import tenant_usage as tu  # noqa: E402

NOW = dt.datetime(2026, 10, 18, 12, 0, tzinfo=dt.timezone.utc)


def ago(days, hours=0):
    return (NOW - dt.timedelta(days=days, hours=hours)).isoformat()


def ev(tenant, actor, kind, action="open", days=0, device="desktop"):
    return {"tenant": tenant, "actor": actor, "kind": kind, "action": action,
            "device": device, "created_at": ago(days)}


TENANTS = [
    {"tenant_id": "t-trial-quiet", "tier": "entry", "billing_managed": True,
     "trial_until": (NOW + dt.timedelta(days=9)).isoformat(), "trial_modules": ["agri-funds"], "modules": []},
    {"tenant_id": "t-trial-busy", "tier": "entry", "billing_managed": True,
     "trial_until": (NOW + dt.timedelta(days=2)).isoformat(), "trial_modules": ["betting"], "modules": []},
    {"tenant_id": "t-paid", "tier": "entry", "billing_managed": True, "modules": [],
     "subs": {"betting": {"status": "active", "tier": "entry"}}},
    {"tenant_id": "t-saas-never", "tier": "saas", "modules": ["banking"]},
    {"tenant_id": "qa-internal-entry", "tier": "entry", "modules": ["agri-funds"]},
]
EVENTS = [
    ev("t-trial-busy", "u1", "session", days=0, device="phone"),
    ev("t-trial-busy", "u1", "signin", "password", days=0),
    ev("t-trial-busy", "u1", "drawer", days=0),
    ev("t-trial-busy", "u1", "drawer", days=1),
    ev("t-trial-busy", "u2", "session", days=10),
    ev("t-trial-busy", "u1", "ask", days=0), ev("t-trial-busy", "u1", "ask", days=0),
    ev("t-trial-busy", "u2", "ask", days=20),
    ev("t-trial-busy", "u1", "report", "sent", days=1),
    ev("t-paid", "u3", "session", days=9),
    ev("qa-internal-entry", "qa", "session", days=0),
    ev("t-paid", "operator", "session", days=0),
    {"tenant": None, "actor": "x", "kind": "session", "action": "open", "created_at": ago(1)},
]


def _roll():
    return tu.build(EVENTS, TENANTS, push_by_tenant={"t-trial-busy": 1}, now=NOW)


def test_internal_traffic_is_excluded_and_counted():
    r = _roll()
    assert r["internal_events_excluded"] == 2 and r["internal_tenants_excluded"] == 1
    assert "qa-internal-entry" not in {t["tenant"] for t in r["tenants"]}


def test_risk_order_quiet_trial_first():
    order = [t["tenant"] for t in _roll()["tenants"]]
    assert order == ["t-trial-quiet", "t-paid", "t-trial-busy", "t-saas-never"]


def test_a_tenant_with_no_client_events_is_a_row_marked_no_use():
    rows = {t["tenant"]: t for t in _roll()["tenants"]}
    q, n = rows["t-trial-quiet"], rows["t-saas-never"]
    assert q["no_use"] and q["quiet"] and q["idle_days"] == 5        # trial started 5 days ago
    assert n["no_use"] and not n["quiet"] and n["users"] == {"7": 0, "28": 0}
    assert n["plan"]["state"] == "none" and n["plan"]["modules"] == ["banking"]


def test_busy_trial_counts():
    t = {t["tenant"]: t for t in _roll()["tenants"]}["t-trial-busy"]
    assert t["plan"]["state"] == "trial" and t["plan"]["trial_days_left"] == 2
    assert t["users"] == {"7": 1, "28": 2} and t["sessions"] == {"7": 1, "28": 2}
    assert t["devices"] == {"phone": 1, "desktop": 1}
    assert t["features"][0] == {"kind": "ask", "label": "chat", "count": 3}
    assert {"kind": "drawer", "label": "fontes do cartão", "count": 2} in t["features"]
    assert t["chat"] == {"7": 2, "28": 3, "peak_user_day": 2, "cap": tu.CHAT_CAP}
    assert t["reports_28"] == 1 and t["push_users"] == 1 and t["signin"] == {"password": 1}
    assert not t["quiet"] and t["idle_days"] == 0


def test_paid_tenant_quiet_after_seven_days():
    t = {t["tenant"]: t for t in _roll()["tenants"]}["t-paid"]
    assert t["plan"]["state"] == "active" and t["idle_days"] == 9 and t["quiet"]


def test_users_are_short_hashes_never_the_sub():
    r = _roll()
    blob = repr(r)
    for raw in ("'u1'", "'u2'", "'u3'"):
        assert raw not in blob
    busy = {t["tenant"]: t for t in r["tenants"]}["t-trial-busy"]
    assert [u["user"] for u in busy["user_list"]] == [tu.user_hash("u1"), tu.user_hash("u2")]
    assert all(len(u["user"]) == 8 for u in busy["user_list"])


def test_events_without_a_tenant_are_counted_not_rowed():
    r = _roll()
    assert r["events_without_tenant"] == 1 and "" not in {t["tenant"] for t in r["tenants"]}


def test_not_a_success_kpi():
    assert "não entram no ETS" in _roll()["note"]


def test_tenant_usage_is_operator_only_in_every_derived_feed():
    feed = {"feed": [], "entities": [], "industries": [], "tenant_usage": {"tenants": [{"tenant": "x"}]}}
    assert feed_builder.scope_feed_to_modules(feed, ["banking"]).get("tenant_usage") == {}
    assert feed_builder.derive_entry_feed(feed, industries=("betting",)).get("tenant_usage") == {}
    assert not feed_builder.derive_sample_feed(feed).get("tenant_usage")


def test_push_stats_count_distinct_users_per_tenant():
    from src.dashboard import push

    class T:
        def scan(self, **kw):
            return {"Items": [{"pk": "SUB#a", "tenant": "t1", "user": "u1"}, {"pk": "SUB#b", "tenant": "t1", "user": "u1"},
                              {"pk": "SUB#c", "tenant": "t1", "user": "u2"}, {"pk": "SUB#d", "tenant": "t2", "user": "u9"}]}
    s = push.stats(table=T())
    assert s["subscriptions"] == 4 and s["users_by_tenant"] == {"t1": 2, "t2": 1}


def test_list_tenant_items_pages():
    from src.dashboard import tenant_config

    class T:
        def __init__(self): self.calls = 0
        def scan(self, **kw):
            self.calls += 1
            return ({"Items": [{"tenant_id": "a"}], "LastEvaluatedKey": {"tenant_id": "a"}} if self.calls == 1
                    else {"Items": [{"tenant_id": "b"}]})
    assert [i["tenant_id"] for i in tenant_config.list_tenant_items(table=T())] == ["a", "b"]


def test_admin_section_renders_from_the_operator_feed():
    html = (Path(__file__).resolve().parents[1] / "src/dashboard/site/v2/admin/index.html").read_text(encoding="utf-8")
    assert 'id="tenantUsage"' in html and "renderTenantUsage();" in html
    assert "DATA.tenant_usage" in html
    assert ".tu > summary { list-style: none; cursor: pointer; min-height: 44px;" in html   # touch target
    assert "sem uso" in html and "internos (QA/operador) excluídos" in html
    body = html[html.index("function renderTenantUsage()"):html.index("// #167: push delivery")]
    assert "email" not in body and "actor" not in body       # users only as hashes
