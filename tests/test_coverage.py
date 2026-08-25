import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import coverage as cov


# --- normalization / dedup ------------------------------------------------
def test_normalize_and_gap_id_dedup():
    a = "Quais empresas estão em Recuperação Judicial?"
    b = "quais empresas estao em recuperacao judicial"
    assert cov.normalize_q(a) == cov.normalize_q(b)
    assert cov.gap_id(a) == cov.gap_id(b)


# --- triage ---------------------------------------------------------------
def _resolver(mapping):
    return lambda item: mapping.get(cov.normalize_q(item.get("title", "")), [])


def test_triage_ingestion_gap_for_compliance():
    t = cov.triage("quais entidades têm certificação ISO 27001?")
    assert t["class"] == "ingestion_gap" and t["auto_fixable"] is False


def test_triage_curation_gap_when_entity_known_and_attr():
    q = "o Itaú é público ou privado?"
    r = _resolver({cov.normalize_q(q): ["itau"]})
    t = cov.triage(q, resolver=r)
    assert t["class"] == "curation_gap" and t["auto_fixable"] is True
    assert t["entities"] == ["itau"]


def test_triage_discovery_gap_unknown_entity():
    q = "o que houve com a Havan?"
    t = cov.triage(q, resolver=lambda i: [], known_entity_ids=set())
    assert t["class"] == "discovery_gap"


# --- store merge ----------------------------------------------------------
def test_merge_gap_dedup_and_recount():
    today = dt.date(2026, 8, 25)
    idx = cov.merge_gap(None, "Quais são estatais?", today=today)
    gid = cov.gap_id("Quais são estatais?")
    assert idx["records"][gid]["count"] == 1 and idx["records"][gid]["status"] == cov.STATUS_OPEN
    idx2 = cov.merge_gap(idx, "quais sao estatais", today=today)  # same normalized
    assert idx2["count"] == 1 and idx2["records"][gid]["count"] == 2


def test_resolved_gap_reopens_on_recurrence():
    today = dt.date(2026, 8, 25)
    idx = cov.merge_gap(None, "x?", today=today)
    gid = cov.gap_id("x?")
    idx["records"][gid]["status"] = cov.STATUS_RESOLVED
    idx2 = cov.merge_gap(idx, "x?", today=today)
    assert idx2["records"][gid]["status"] == cov.STATUS_OPEN


# --- fake S3 --------------------------------------------------------------
class FakeS3:
    def __init__(self): self.store = {}
    def get_object(self, Bucket, Key):
        if Key not in self.store: raise KeyError(Key)
        return {"Body": _B(self.store[Key])}
    def put_object(self, Bucket, Key, Body, **kw): self.store[Key] = Body
class _B:
    def __init__(self, b): self._b = b
    def read(self): return self._b


def test_record_persists():
    s3 = FakeS3()
    cov.record("quais são estatais?", "b", s3=s3, today=dt.date(2026, 8, 25))
    saved = json.loads(s3.store[cov.INDEX_KEY])
    assert saved["count"] == 1


# --- remediation driver ---------------------------------------------------
def test_remediate_autofix_then_resolve():
    s3 = FakeS3()
    q = "o Itaú é público ou privado?"
    cov.record(q, "b", s3=s3, today=dt.date(2026, 8, 25))
    fixes = {"ran": 0}
    def fixer():
        fixes["ran"] += 1; return {"ownership": 1}
    # verifier says the agent can now answer -> gap resolves, no issue opened
    out = cov.remediate(
        "b", resolver=_resolver({cov.normalize_q(q): ["itau"]}),
        verifier=lambda x: True, autofixer=fixer,
        issuer=lambda g, t: (_ for _ in ()).throw(AssertionError("should not open issue")),
        s3=s3, today=dt.date(2026, 8, 25))
    assert out["auto_fixed"] == 1 and out["resolved"] == 1 and fixes["ran"] == 1
    saved = json.loads(s3.store[cov.INDEX_KEY])
    assert saved["records"][cov.gap_id(q)]["status"] == cov.STATUS_RESOLVED


def test_remediate_opens_issue_for_ingestion_gap():
    s3 = FakeS3()
    q = "quais têm certificação ISO?"
    cov.record(q, "b", s3=s3, today=dt.date(2026, 8, 25))
    opened = []
    out = cov.remediate(
        "b", resolver=lambda i: [], verifier=lambda x: False,
        autofixer=lambda: {}, issuer=lambda g, t: opened.append(g["id"]) or "http://issue/1",
        s3=s3, today=dt.date(2026, 8, 25))
    assert out["proposed"] == 1 and opened
    saved = json.loads(s3.store[cov.INDEX_KEY])
    rec = saved["records"][cov.gap_id(q)]
    assert rec["status"] == cov.STATUS_PROPOSED and rec["issue_url"] == "http://issue/1"


def test_auto_codegen_is_off():
    # the loop must never autonomously write+deploy new ingestion code
    assert cov.AUTO_CODEGEN is False


def test_close_issue_noop_without_token():
    assert cov.close_issue("https://github.com/o/r/issues/5", token=None) is False
    assert cov.close_issue(None, token="x") is False
    assert cov.close_issue("not-a-github-url", token="x") is False


def test_remediate_closes_issue_on_resolve():
    s3 = FakeS3()
    q = "o Itaú é público?"
    cov.record(q, "b", s3=s3, today=dt.date(2026, 8, 25))
    gid = cov.gap_id(q)
    idx = json.loads(s3.store[cov.INDEX_KEY]); idx["records"][gid]["issue_url"] = "http://gh/1"
    s3.store[cov.INDEX_KEY] = json.dumps(idx)
    closed = []
    cov.remediate(
        "b", resolver=_resolver({cov.normalize_q(q): ["itau"]}),
        verifier=lambda x: True, autofixer=lambda: {}, issuer=lambda g, t: "u",
        closer=lambda url: closed.append(url) or True, only_id=gid,
        s3=s3, today=dt.date(2026, 8, 25))
    saved = json.loads(s3.store[cov.INDEX_KEY])
    assert saved["records"][gid]["issue_closed"] is True and closed == ["http://gh/1"]


def test_remediate_only_id_touches_one_gap():
    s3 = FakeS3()
    cov.record("o Itaú é público?", "b", s3=s3, today=dt.date(2026, 8, 25))
    cov.record("quais têm ISO?", "b", s3=s3, today=dt.date(2026, 8, 25))
    target = cov.gap_id("o Itaú é público?")
    out = cov.remediate(
        "b", resolver=_resolver({cov.normalize_q("o Itaú é público?"): ["itau"]}),
        verifier=lambda x: True, autofixer=lambda: {"ownership": 1},
        issuer=lambda g, t: "u", only_id=target, s3=s3, today=dt.date(2026, 8, 25))
    assert out["triaged"] == 1  # only the targeted gap was processed
    saved = json.loads(s3.store[cov.INDEX_KEY])
    assert saved["records"][target]["status"] == cov.STATUS_RESOLVED
    assert saved["records"][cov.gap_id("quais têm ISO?")]["status"] == cov.STATUS_OPEN


# --- gaps API -------------------------------------------------------------
def test_gaps_api_forbids_without_origin_secret(monkeypatch):
    from src.dashboard import gaps_api
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", "s")
    r = gaps_api.lambda_handler({"headers": {}, "body": "{}"}, None)
    assert r["statusCode"] == 403


def test_gaps_api_remediate_requires_id(monkeypatch):
    from src.dashboard import gaps_api
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    monkeypatch.setenv("ONCA_DIGESTS_BUCKET", "b")
    r = gaps_api.lambda_handler(
        {"rawPath": "/api/gaps/remediate", "requestContext": {"http": {"method": "POST"}},
         "body": json.dumps({})}, None)
    assert r["statusCode"] == 400
