"""ADR 014 step 2: the widened, targeted safe-autofix set."""
import datetime as dt
import inspect
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.synth import coverage as cov
from tests.test_coverage import FakeS3
from tests.test_ingestion_evidence import FEED


def test_fixes_follow_the_evidence():
    # a live source that misses the entity, on a family with a bounded fix
    t = cov.triage("o Nubank faz parte do ISE B3?", resolver=lambda i: ["nubank"], feed=FEED)
    assert t["class"] == "coverage_gap" and t["fixes"] == ["esg_ise_b3"] and t["auto_fixable"]
    feed = {**FEED, "entity_attrs": {**FEED["entity_attrs"], "xp": {"label": "XP"}}}
    t = cov.triage("a XP é autorizada pelo BCB?", resolver=lambda i: ["xp"], feed=feed)
    assert t["fixes"] == ["certifications"]
    # a curation gap keeps the trio
    t = cov.triage("o Itaú é público ou privado?", resolver=lambda i: ["itau"], feed=FEED)
    assert t["class"] == "curation_gap" and t["fixes"] == ["ownership", "tickers", "curation"]
    # a retrieval gap about a known entity: the news_term fix
    t = cov.triage("o que o Itaú anunciou ontem?", resolver=lambda i: ["itau"], feed=FEED)
    assert t["class"] == "retrieval_gap" and t["fixes"] == ["curation"]


def test_no_fix_where_the_loop_may_not_act_alone():
    for q, ents in (("quais bancos têm certificação ISO 27001?", []),        # needs a new source
                    ("quantos funcionários tem o Nubank?", ["nubank"]),      # needs a new source
                    ("qual o índice de Basileia do Nubank?", ["nubank"]),    # coverage, no bounded fix
                    ("o que mudou no Pix?", [])):                            # retrieval, no entity
        t = cov.triage(q, resolver=lambda i, e=ents: e, feed=FEED)
        assert t["fixes"] == [] and t["auto_fixable"] is False, q


def test_every_fix_is_a_registry_backfill_that_never_sees_the_question():
    for name, (desc, fn) in cov.AUTOFIXES.items():
        assert desc and list(inspect.signature(fn).parameters) == ["reg"], name
    assert set(cov.CURATION_FIXES) <= set(cov.AUTOFIXES)
    for fam, fixes in cov.FAMILY_FIXES.items():
        assert set(fixes) <= set(cov.AUTOFIXES), fam


def test_safe_autofix_runs_only_the_named_fixes_and_isolates_failures(monkeypatch):
    from src.synth import entity_registry as reg
    calls = []
    monkeypatch.setattr(reg, "backfill_ownership", lambda: calls.append("own") or [("a", "x")])
    monkeypatch.setattr(reg, "backfill_tickers", lambda: calls.append("tick") or [])
    monkeypatch.setattr(reg, "backfill_curation", lambda: calls.append("cur") or 0)
    def boom():
        raise RuntimeError("ddb down")
    monkeypatch.setattr(reg, "backfill_certifications", boom)
    out = cov.safe_autofix(["certifications", "ownership", "nope"])
    assert calls == ["own"] and out["ownership"] == 1
    assert out["certifications"].startswith("error: ddb down") and out["nope"] == "unknown fix"
    calls.clear()
    assert cov.safe_autofix() == {"ownership": 1, "tickers": 0, "curation": 0}   # the legacy trio
    assert calls == ["own", "tick", "cur"]


def test_the_esg_fix_fetches_the_portfolio_then_applies_it(monkeypatch):
    from src.ingest import esg_ise_b3
    from src.synth import entity_registry as reg
    pf = {"cycle": "2026-2027", "constituents": []}
    monkeypatch.setattr(esg_ise_b3, "fetch_portfolio", lambda: pf)
    got = []
    monkeypatch.setattr(reg, "backfill_esg_ise_b3", lambda p: got.append(p) or [("itau", {})])
    assert cov.safe_autofix(["esg_ise_b3"]) == {"esg_ise_b3": 1} and got == [pf]


def _store(s3, *qs):
    for q in qs:
        cov.record(q, "b", s3=s3, today=dt.date(2026, 10, 1))


def test_scheduled_pass_runs_the_union_once():
    s3 = FakeS3()
    _store(s3, "o Nubank faz parte do ISE B3?", "a Nubank está no índice ISE?",
           "o que o Itaú anunciou ontem?", "quais bancos têm certificação ISO 27001?")
    resolver = lambda item: ["itau"] if "Itaú" in item["title"] else ["nubank"]
    runs = []
    out = cov.scheduled_pass("b", resolver=resolver, feed=FEED, autofixer=lambda f: runs.append(list(f)) or {},
                             s3=s3, today=dt.date(2026, 11, 4))
    assert runs == [["esg_ise_b3", "curation"]] and out["auto_fixable"] == 3


def test_remediate_runs_each_fix_once_across_gaps():
    s3 = FakeS3()
    _store(s3, "o Nubank faz parte do ISE B3?", "a Nubank está no índice ISE?", "o que o Itaú anunciou ontem?")
    resolver = lambda item: ["itau"] if "Itaú" in item["title"] else ["nubank"]
    runs = []
    cov.remediate("b", resolver=resolver, feed=FEED, autofixer=lambda f: runs.append(list(f)) or {},
                  verifier=lambda q: False, issuer=lambda r, t: None, s3=s3, today=dt.date(2026, 11, 4))
    assert sorted(map(tuple, runs)) == [("curation",), ("esg_ise_b3",)]
