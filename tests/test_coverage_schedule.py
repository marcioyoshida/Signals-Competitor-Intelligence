"""#295: the weekly coverage-gap pass: a capped Nova Lite re-ask, no public issue."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.synth import coverage as cov
from tests.test_coverage import FakeS3

D = dt.date(2026, 11, 4)


def _store(s3, *gaps):
    for q, reason, status in gaps:
        cov.record(q, "b", s3=s3, reason=reason, today=dt.date(2026, 10, 1))
        if status != cov.STATUS_OPEN:
            idx = json.loads(s3.store[cov.INDEX_KEY])
            idx["records"][cov.gap_id(q)]["status"] = status
            s3.store[cov.INDEX_KEY] = json.dumps(idx)


def _saved(s3):
    return json.loads(s3.store[cov.INDEX_KEY])["records"]


def test_pass_triages_verifies_and_resolves_what_now_grounds():
    s3 = FakeS3()
    _store(s3, ("quais bancos têm certificação ISO?", "no-grounding", cov.STATUS_OPEN),
           ("o que mudou no Pix?", "no-grounding", cov.STATUS_PROPOSED),
           ("velha pergunta resolvida?", "no-grounding", cov.STATUS_RESOLVED))
    asked, closed = [], []
    def verify(q, scope):
        asked.append(q)
        return "Pix" in q
    out = cov.scheduled_pass("b", verifier=verify, autofixer=lambda: pytest.fail("no auto-fixable gap"),
                             closer=lambda u: closed.append(u) or True, s3=s3, today=D)
    assert out == {"triaged": 2, "auto_fixable": 0, "verified": 2, "resolved": 1, "autofix": None}
    assert "velha pergunta resolvida?" not in asked                 # resolved gaps aren't re-asked
    r = _saved(s3)
    pix, iso = r[cov.gap_id("o que mudou no Pix?")], r[cov.gap_id("quais bancos têm certificação ISO?")]
    assert pix["status"] == cov.STATUS_RESOLVED and pix["resolved_by"] == "scheduled"
    assert pix["resolved_at"] == "2026-11-04" and pix["issue_closed"] is False and closed == []
    assert iso["status"] == cov.STATUS_OPEN and iso["verified_at"] == "2026-11-04"
    assert iso["triage"]["class"] == "ingestion_gap" and iso["triaged_at"] == "2026-11-04"


def test_verify_is_capped_and_rotates_least_recently_verified_first():
    s3 = FakeS3()
    qs = [f"pergunta numero {i} sobre credito?" for i in range(5)]
    _store(s3, *[(q, "no-grounding", cov.STATUS_OPEN) for q in qs])
    asked = []
    v = lambda q, s: asked.append(q) or False
    cov.scheduled_pass("b", verifier=v, max_verify=3, s3=s3, today=D)
    first = list(asked)
    assert len(first) == 3
    asked.clear()
    cov.scheduled_pass("b", verifier=v, max_verify=3, s3=s3, today=D + dt.timedelta(days=7))
    assert set(asked[:2]) == set(qs) - set(first)                  # the two not asked last week go first
    assert len(asked) == 3
    assert cov.SCHEDULED_VERIFY_MAX == 15


def test_a_resolved_issue_is_closed_when_it_has_one():
    s3 = FakeS3()
    _store(s3, ("o que mudou no Pix?", "no-grounding", cov.STATUS_PROPOSED))
    idx = json.loads(s3.store[cov.INDEX_KEY])
    idx["records"][cov.gap_id("o que mudou no Pix?")]["issue_url"] = "https://github.com/o/r/issues/31"
    s3.store[cov.INDEX_KEY] = json.dumps(idx)
    closed = []
    cov.scheduled_pass("b", verifier=lambda q, s: True, closer=lambda u: closed.append(u) or True, s3=s3, today=D)
    assert closed == ["https://github.com/o/r/issues/31"]
    assert _saved(s3)[cov.gap_id("o que mudou no Pix?")]["issue_closed"] is True


def test_backfills_run_once_only_when_a_gap_is_auto_fixable():
    s3 = FakeS3()
    _store(s3, ("o Itaú é público ou privado?", "no-grounding", cov.STATUS_OPEN),
           ("o Bradesco é público ou privado?", "no-grounding", cov.STATUS_OPEN))
    ran = []
    out = cov.scheduled_pass("b", resolver=lambda item: ["itau"], verifier=None,
                             autofixer=lambda: ran.append(1) or {"ownership": 0}, s3=s3, today=D)
    assert ran == [1] and out["auto_fixable"] == 2 and out["autofix"] == {"ownership": 0}
    assert out["verified"] == 0


def test_feed_verifier_uses_feed_cards_only_and_the_given_model():
    feed = {"feed": [{"id": "c1", "entity": "nubank", "entity_label": "Nubank", "lenses": ["pix"],
                      "narrative": "O Nubank lançou o Pix Automático para clientes pessoa física.",
                      "date": "2026-10-30", "citations": [{"url": "https://example.org/a"}]}],
            "entities": [{"entity": "nubank", "label": "Nubank"}]}
    calls = []
    def model(user, **kw):
        calls.append(user)
        return "O Nubank lançou o Pix Automático [c1]."
    v = cov.feed_verifier(feed, converser=model)
    assert v("O que o Nubank fez no Pix Automático?", None) is True and len(calls) == 1
    assert cov.feed_verifier(feed, converser=lambda *a, **k: None)("O que o Nubank fez no Pix?", None) is False


def test_feed_verifier_defaults_to_the_tagged_bedrock_converse(monkeypatch):
    from src.dashboard import agent_ask
    from src.synth import bedrock_llm
    got = {}
    monkeypatch.setattr(agent_ask, "answer", lambda q, **kw: got.update(kw) or {"grounded": False})
    cov.feed_verifier({"feed": []})("x?", None)
    assert got["converser"] is bedrock_llm.converse and got["kb_retrieve"] is None


def test_open_issue_never_puts_the_question_in_the_public_issue(monkeypatch):
    import subprocess
    seen = []
    class R:
        def __init__(self, out=""): self.returncode, self.stdout, self.stderr = 0, out, ""
    def run(args, **kw):
        seen.append(args)
        return R("[]") if args[:3] == ["gh", "issue", "list"] else R("https://github.com/o/r/issues/9")
    monkeypatch.setattr(subprocess, "run", run)
    q = "Qual o faturamento do cliente Banco Segredo S.A. em 2026?"
    gap = {"id": cov.gap_id(q), "question": q, "count": 2, "last_seen": "2026-11-01"}
    url = cov.open_issue(gap, cov.triage(q))
    assert url.endswith("/issues/9")
    created = next(a for a in seen if a[:3] == ["gh", "issue", "create"])
    text = " ".join(created)
    assert "Segredo" not in text and "faturamento" not in text
    assert gap["id"] in text


def test_gaps_api_runs_the_pass_only_on_the_exact_scheduled_event(monkeypatch):
    from src.dashboard import gaps_api
    ran = []
    monkeypatch.setattr(gaps_api, "run_scheduled", lambda: ran.append(1) or {"status": "ok"})
    assert gaps_api.lambda_handler({"coverage_loop": "scheduled"}, None) == {"status": "ok"}
    assert ran == [1]
    # a web request carrying the same payload in its body goes through auth, not the pass
    monkeypatch.setenv("ONCA_ORIGIN_SECRET", "s")
    r = gaps_api.lambda_handler({"headers": {}, "body": json.dumps({"coverage_loop": "scheduled"})}, None)
    assert r["statusCode"] == 403 and ran == [1]
    r = gaps_api.lambda_handler({"coverage_loop": "scheduled", "headers": {}}, None)
    assert r["statusCode"] == 403 and ran == [1]


def test_run_scheduled_wires_the_resolver_and_verifier(monkeypatch):
    from src.dashboard import agent_ask, gaps_api
    from src.synth import entities
    monkeypatch.setenv("ONCA_DIGESTS_BUCKET", "b")
    monkeypatch.setenv("ONCA_SITE_BUCKET", "site")
    monkeypatch.setattr(agent_ask, "_load_feed", lambda site: {"feed": []})
    got = {}
    def fake_pass(bucket, *, resolver, verifier, feed):
        got.update(bucket=bucket, resolver=resolver, verifier=verifier, feed=feed)
        return {"triaged": 0}
    monkeypatch.setattr(cov, "scheduled_pass", fake_pass)
    assert gaps_api.run_scheduled() == {"status": "ok", "triaged": 0}
    assert got["bucket"] == "b" and got["resolver"] is entities.resolve_entities
    assert callable(got["verifier"])


def test_cdk_schedules_the_weekly_pass_on_the_gaps_lambda():
    src = (ROOT / "infra/app.py").read_text()
    blk = src[src.index('"OncaCoverageGapWeekly"'):][:600]
    assert 'week_day="WED"' in blk and 'hour="10"' in blk
    assert "gaps_fn" in blk and '{"coverage_loop": "scheduled"}' in blk
    from src.dashboard import gaps_api
    assert gaps_api.SCHEDULED_EVENT == {"coverage_loop": "scheduled"}
    fn = src[src.index('"OncaGapsApi"'):][:600]
    assert "Duration.seconds(300)" in fn
