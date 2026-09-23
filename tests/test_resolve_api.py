import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import resolve_api  # noqa: E402


class FakeConfigTable:
    def __init__(self, configs: dict[str, dict]):
        self._configs = configs

    def get_item(self, Key):
        item = self._configs.get(Key["tenant_id"])
        return {"Item": item} if item else {}


class FakeQuotaTable:
    """Enough of DynamoDB's `update_item` to exercise the ADD-based counters
    resolve_api.py uses — no real DynamoDB, just tracks call_count ints and
    entity_ids sets keyed by pk, matching the two UpdateExpressions the
    module actually sends."""

    def __init__(self):
        self.items: dict[str, dict] = {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ReturnValues=None):
        pk = Key["pk"]
        item = self.items.setdefault(pk, {"pk": pk})
        if ":one" in ExpressionAttributeValues:
            item["call_count"] = item.get("call_count", 0) + ExpressionAttributeValues[":one"]
        if ":e" in ExpressionAttributeValues:
            item["entity_ids"] = (item.get("entity_ids") or set()) | ExpressionAttributeValues[":e"]
        item.setdefault("ttl", ExpressionAttributeValues[":ttl"])
        return {"Attributes": dict(item)}


def _event(*, tenant_id="tier1", caller_arn=None, body='{"cnpj_root": "12345678"}', method="POST"):
    return {
        "requestContext": {
            "http": {"method": method},
            "authorizer": {"iam": {"userArn": caller_arn}} if caller_arn else {},
        },
        "queryStringParameters": {"tenant_id": tenant_id} if tenant_id else None,
        "body": body,
    }


VALID_CALLER_ARN = "arn:aws:sts::123456789012:assumed-role/OncaResolveCallerRole/tenant-synth-session"
REGISTERED_ARN = "arn:aws:iam::123456789012:role/OncaResolveCallerRole"

MARKETPLACE_CONFIG = {
    "tier1": {
        "tenant_id": "tier1", "tier": "sovereign", "modules": ["banking"],
        "plane": "marketplace", "resolve_caller_role_arn": REGISTERED_ARN,
    }
}


# --- auth --------------------------------------------------------------------

def test_rejects_non_post_methods():
    resp = resolve_api.handle(_event(method="GET"))
    assert resp["statusCode"] == 405


def test_requires_tenant_id_query_param():
    resp = resolve_api.handle(_event(tenant_id=None, caller_arn=VALID_CALLER_ARN))
    assert resp["statusCode"] == 403
    assert "tenant_id" in resp["body"]


def test_requires_a_verified_caller_identity():
    resp = resolve_api.handle(_event(caller_arn=None))
    assert resp["statusCode"] == 403
    assert "verified caller identity" in resp["body"]


def test_rejects_an_unknown_tenant():
    resp = resolve_api.handle(
        _event(tenant_id="ghost", caller_arn=VALID_CALLER_ARN),
        config_table=FakeConfigTable({}),
    )
    assert resp["statusCode"] == 403
    assert "unknown tenant" in resp["body"]


def test_rejects_a_saas_plane_tenant():
    configs = {"s1": {"tenant_id": "s1", "tier": "saas", "modules": [], "plane": "saas",
                       "resolve_caller_role_arn": None}}
    resp = resolve_api.handle(
        _event(tenant_id="s1", caller_arn=VALID_CALLER_ARN),
        config_table=FakeConfigTable(configs),
    )
    assert resp["statusCode"] == 403
    assert "marketplace plane" in resp["body"]


def test_rejects_a_caller_from_a_different_account_or_role(monkeypatch):
    monkeypatch.setattr(resolve_api.resolver, "resolve_known_id", lambda **kw: {"entity_id": "x"})
    wrong_role = "arn:aws:sts::123456789012:assumed-role/SomeOtherRole/session"
    resp = resolve_api.handle(
        _event(caller_arn=wrong_role),
        config_table=FakeConfigTable(MARKETPLACE_CONFIG),
        quota_table=FakeQuotaTable(),
    )
    assert resp["statusCode"] == 403
    assert "does not match" in resp["body"]

    wrong_account = "arn:aws:sts::999999999999:assumed-role/OncaResolveCallerRole/session"
    resp2 = resolve_api.handle(
        _event(caller_arn=wrong_account),
        config_table=FakeConfigTable(MARKETPLACE_CONFIG),
        quota_table=FakeQuotaTable(),
    )
    assert resp2["statusCode"] == 403


def test_accepts_any_session_name_for_the_registered_role(monkeypatch):
    monkeypatch.setattr(resolve_api.resolver, "resolve_known_id", lambda **kw: {"entity_id": "acme"})
    other_session = "arn:aws:sts::123456789012:assumed-role/OncaResolveCallerRole/different-session-id"
    resp = resolve_api.handle(
        _event(caller_arn=other_session),
        config_table=FakeConfigTable(MARKETPLACE_CONFIG),
        quota_table=FakeQuotaTable(),
    )
    assert resp["statusCode"] == 200


# --- request validation --------------------------------------------------------

def test_rejects_zero_identifiers(monkeypatch):
    resp = resolve_api.handle(
        _event(caller_arn=VALID_CALLER_ARN, body="{}"),
        config_table=FakeConfigTable(MARKETPLACE_CONFIG),
        quota_table=FakeQuotaTable(),
    )
    assert resp["statusCode"] == 400


def test_rejects_more_than_one_identifier_no_batch():
    resp = resolve_api.handle(
        _event(caller_arn=VALID_CALLER_ARN, body='{"cnpj_root": "1", "ticker": "ABC"}'),
        config_table=FakeConfigTable(MARKETPLACE_CONFIG),
        quota_table=FakeQuotaTable(),
    )
    assert resp["statusCode"] == 400


def test_rejects_invalid_json_body():
    resp = resolve_api.handle(
        _event(caller_arn=VALID_CALLER_ARN, body="not json"),
        config_table=FakeConfigTable(MARKETPLACE_CONFIG),
        quota_table=FakeQuotaTable(),
    )
    assert resp["statusCode"] == 400


# --- the resolve contract itself -----------------------------------------------

def test_hit_returns_the_exact_contract_shape(monkeypatch):
    monkeypatch.setattr(
        resolve_api.resolver, "resolve_known_id",
        lambda **kw: {"entity_id": "acme", "display_name": "Acme SA",
                      "canonical_id": "acme", "industries": ["banking"], "confidence": 1.0},
    )
    resp = resolve_api.handle(
        _event(caller_arn=VALID_CALLER_ARN),
        config_table=FakeConfigTable(MARKETPLACE_CONFIG),
        quota_table=FakeQuotaTable(),
    )
    assert resp["statusCode"] == 200
    import json
    assert json.loads(resp["body"]) == {
        "entity_id": "acme", "display_name": "Acme SA",
        "canonical_id": "acme", "industries": ["banking"], "confidence": 1.0,
    }


def test_miss_returns_404_never_a_fuzzy_guess(monkeypatch):
    monkeypatch.setattr(resolve_api.resolver, "resolve_known_id", lambda **kw: None)
    resp = resolve_api.handle(
        _event(caller_arn=VALID_CALLER_ARN),
        config_table=FakeConfigTable(MARKETPLACE_CONFIG),
        quota_table=FakeQuotaTable(),
    )
    assert resp["statusCode"] == 404


def test_resolve_identifiers_are_passed_through_to_resolve_known_id(monkeypatch):
    seen = {}

    def fake_resolve(**kw):
        seen.update(kw)
        return {"entity_id": "x"}

    monkeypatch.setattr(resolve_api.resolver, "resolve_known_id", fake_resolve)
    resolve_api.handle(
        _event(caller_arn=VALID_CALLER_ARN, body='{"ticker": "PETR4"}'),
        config_table=FakeConfigTable(MARKETPLACE_CONFIG),
        quota_table=FakeQuotaTable(),
    )
    assert seen == {"ticker": "PETR4"}


# --- rate limiting ---------------------------------------------------------

def test_rate_limit_blocks_after_the_per_minute_cap(monkeypatch):
    monkeypatch.setattr(resolve_api.resolver, "resolve_known_id", lambda **kw: {"entity_id": "x"})
    quota = FakeQuotaTable()
    resp = None
    for _ in range(resolve_api.RATE_LIMIT_PER_MINUTE + 1):
        resp = resolve_api.handle(
            _event(caller_arn=VALID_CALLER_ARN),
            config_table=FakeConfigTable(MARKETPLACE_CONFIG),
            quota_table=quota,
        )
    assert resp["statusCode"] == 429


def test_rate_limit_is_per_tenant_not_global(monkeypatch):
    monkeypatch.setattr(resolve_api.resolver, "resolve_known_id", lambda **kw: {"entity_id": "x"})
    quota = FakeQuotaTable()
    other_arn = "arn:aws:sts::123456789012:assumed-role/OncaResolveCallerRoleTwo/session"
    configs = dict(MARKETPLACE_CONFIG)
    configs["tier2"] = {
        "tenant_id": "tier2", "tier": "sovereign", "modules": ["banking"],
        "plane": "marketplace",
        "resolve_caller_role_arn": "arn:aws:iam::123456789012:role/OncaResolveCallerRoleTwo",
    }
    for _ in range(resolve_api.RATE_LIMIT_PER_MINUTE + 1):
        resolve_api.handle(
            _event(caller_arn=VALID_CALLER_ARN),
            config_table=FakeConfigTable(configs), quota_table=quota,
        )
    resp = resolve_api.handle(
        _event(tenant_id="tier2", caller_arn=other_arn),
        config_table=FakeConfigTable(configs), quota_table=quota,
    )
    assert resp["statusCode"] == 200


def test_a_quota_table_outage_fails_open_not_closed():
    class BrokenTable:
        def update_item(self, **kw):
            raise RuntimeError("dynamodb unavailable")

    assert resolve_api._rate_limit_ok("tier1", table=BrokenTable()) is True


# --- breadth canary (never blocks) ---------------------------------------------

def test_breadth_tracking_never_blocks_a_response(monkeypatch):
    monkeypatch.setattr(resolve_api.resolver, "resolve_known_id", lambda **kw: {"entity_id": "x"})
    quota = FakeQuotaTable()
    resp = None
    for _ in range(resolve_api.BREADTH_ANOMALY_THRESHOLD + 5):
        quota.items.clear()  # avoid tripping the per-minute rate limit across iterations
        resp = resolve_api.handle(
            _event(caller_arn=VALID_CALLER_ARN),
            config_table=FakeConfigTable(MARKETPLACE_CONFIG), quota_table=quota,
        )
    assert resp["statusCode"] == 200


def test_breadth_canary_prints_exactly_once_at_the_threshold(monkeypatch, capsys):
    monkeypatch.setattr(resolve_api.resolver, "resolve_known_id", lambda **kw: {"entity_id": "x"})
    quota = FakeQuotaTable()
    for i in range(resolve_api.BREADTH_ANOMALY_THRESHOLD + 2):
        resolve_api._record_breadth("tier1", f"entity-{i}", table=quota)
    out = capsys.readouterr().out
    assert out.count("CANARY resolve_api") == 1
