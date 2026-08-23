import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import run_trigger


class _ConflictException(Exception):
    pass


class _ResourceNotFoundException(Exception):
    pass


class _FakeScheduler:
    def __init__(self, existing=None, conflict=False):
        self.existing = existing
        self.conflict = conflict
        self.created = []
        self.updated = []

        class _Exc:
            ConflictException = _ConflictException
            ResourceNotFoundException = _ResourceNotFoundException

        self.exceptions = _Exc()

    def create_schedule(self, **kw):
        if self.conflict:
            raise _ConflictException()
        self.created.append(kw)

    def update_schedule(self, **kw):
        self.updated.append(kw)

    def get_schedule(self, Name):
        if self.existing is None:
            raise _ResourceNotFoundException()
        return self.existing


@pytest.fixture
def _env(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", "sec")
    monkeypatch.setenv("ONCA_PIPELINE_ARN", "arn:aws:states:us-east-1:1:stateMachine:OncaPipeline")
    monkeypatch.setenv("ONCA_SCHEDULER_ROLE_ARN", "arn:aws:iam::1:role/sched")
    monkeypatch.setenv("ONCA_SCHEDULE_NAME", "onca-adhoc-run")


class _FakeSfn:
    """Minimal Step Functions client for the GET execution-status path."""
    def __init__(self, status=None, started=None, step=None):
        self.status = status
        self.started = started
        self.step = step

    def list_executions(self, stateMachineArn, maxResults=1):
        return {"executions": ([{"executionArn": "arn:exec"}] if self.status else [])}

    def describe_execution(self, executionArn):
        return {"status": self.status, "startDate": self.started}

    def get_execution_history(self, executionArn, reverseOrder=True, maxResults=50):
        if not self.step:
            return {"events": []}
        return {"events": [{"eventDetails": {"taskStateEnteredEventDetails": {"name": self.step}}}]}


def _install(monkeypatch, fake, sfn=None):
    import types

    def client(svc):
        return sfn if (svc == "stepfunctions" and sfn is not None) else fake

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=client))


def _event(method="POST", secret="sec"):
    return {
        "headers": {"X-Onca-Origin": secret},
        "requestContext": {"http": {"method": method}},
    }


def test_next_top_of_hour_rounds_up():
    now = dt.datetime(2026, 8, 21, 17, 35, 12, tzinfo=run_trigger._BRT)
    assert run_trigger.next_top_of_hour(now) == "2026-08-21T18:00:00"
    # exactly on the hour still advances to the next
    on = dt.datetime(2026, 8, 21, 17, 0, 0, tzinfo=run_trigger._BRT)
    assert run_trigger.next_top_of_hour(on) == "2026-08-21T18:00:00"


def test_forbidden_without_origin_secret(_env, monkeypatch):
    _install(monkeypatch, _FakeScheduler())
    out = run_trigger.lambda_handler(_event(secret="wrong"), None)
    assert out["statusCode"] == 403


def test_post_creates_schedule(_env, monkeypatch):
    fake = _FakeScheduler()
    _install(monkeypatch, fake)
    out = run_trigger.lambda_handler(_event(), None)
    body = json.loads(out["body"])
    assert out["statusCode"] == 200 and body["status"] == "scheduled"
    assert body["created"] is True and body["debounced"] is False
    assert len(fake.created) == 1 and not fake.updated
    sched = fake.created[0]
    assert sched["Name"] == "onca-adhoc-run"
    assert sched["ScheduleExpression"].startswith("at(")
    assert sched["ActionAfterCompletion"] == "DELETE"
    assert sched["Target"]["Arn"].endswith("OncaPipeline")


def test_post_debounces_on_conflict(_env, monkeypatch):
    fake = _FakeScheduler(conflict=True)
    _install(monkeypatch, fake)
    out = run_trigger.lambda_handler(_event(), None)
    body = json.loads(out["body"])
    assert body["status"] == "scheduled" and body["created"] is False and body["debounced"] is True
    assert len(fake.updated) == 1 and not fake.created


def test_get_reports_pending_state(_env, monkeypatch):
    fake = _FakeScheduler(existing={"ScheduleExpression": "at(2026-08-21T18:00:00)",
                                    "ScheduleExpressionTimezone": "America/Sao_Paulo"})
    _install(monkeypatch, fake)
    out = run_trigger.lambda_handler(_event(method="GET"), None)
    body = json.loads(out["body"])
    assert body["pending"] is True and body["at"].startswith("at(")


def test_get_reports_none_when_absent(_env, monkeypatch):
    _install(monkeypatch, _FakeScheduler(existing=None))
    out = run_trigger.lambda_handler(_event(method="GET"), None)
    assert json.loads(out["body"])["pending"] is False


def test_get_running_execution_serializes_datetime(_env, monkeypatch):
    # regression: describe_execution returns a datetime startDate — the JSON response
    # must not blow up (it did: "Object of type datetime is not JSON serializable").
    sfn = _FakeSfn(status="RUNNING",
                   started=dt.datetime(2026, 8, 23, 14, 0, tzinfo=dt.timezone.utc),
                   step="SwotSeedTask")
    _install(monkeypatch, _FakeScheduler(existing=None), sfn=sfn)
    out = run_trigger.lambda_handler(_event(method="GET"), None)
    assert out["statusCode"] == 200
    body = json.loads(out["body"])                      # would raise if not serializable
    assert body["execution"]["status"] == "RUNNING"
    assert isinstance(body["execution"]["started_at"], str)
    assert body["execution"]["current_step"] == "SwotSeedTask"


def test_get_finished_execution_omits_step(_env, monkeypatch):
    sfn = _FakeSfn(status="SUCCEEDED",
                   started=dt.datetime(2026, 8, 23, 14, 0, tzinfo=dt.timezone.utc))
    _install(monkeypatch, _FakeScheduler(existing=None), sfn=sfn)
    body = json.loads(run_trigger.lambda_handler(_event(method="GET"), None)["body"])
    assert body["execution"]["status"] == "SUCCEEDED"
    assert "current_step" not in body["execution"]      # progress only while RUNNING


def test_missing_config_returns_500(monkeypatch):
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", "sec")
    monkeypatch.delenv("ONCA_PIPELINE_ARN", raising=False)
    monkeypatch.delenv("ONCA_SCHEDULER_ROLE_ARN", raising=False)
    _install(monkeypatch, _FakeScheduler())
    out = run_trigger.lambda_handler(_event(), None)
    assert out["statusCode"] == 500
