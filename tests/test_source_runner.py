"""ADR 019 Phase 2 — the registry-driven source runner in lambda_port."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import lambda_port as lp
from src.ingest import registry as reg


def _spec(**kw):
    return reg.SourceSpec("t", "news", **kw)


def test_lens_section_uses_spec_limits_and_extra():
    records = [{"id": i} for i in range(20)]
    new = [{"id": i} for i in range(5)]
    sec = lp._lens_section(records, new, _spec(items_limit=3, context_limit=4), governance_count=2)
    assert sec["count"] == 20 and sec["new_count"] == 5 and sec["governance_count"] == 2
    assert len(sec["items"]) == 3 and len(sec["context"]) == 4
    assert all(r["is_new"] for r in sec["items"])          # _tag_new marks delta rows


def test_source_enabled_default_and_env(monkeypatch):
    monkeypatch.delenv("ONCA_T", raising=False)
    assert lp._source_enabled(_spec(default_on=True)) is True
    assert lp._source_enabled(_spec(default_on=False)) is False
    monkeypatch.setenv("ONCA_T", "false")
    assert lp._source_enabled(_spec(default_on=True)) is False   # env overrides default-on
    monkeypatch.setenv("ONCA_CUSTOM", "true")
    assert lp._source_enabled(_spec(default_on=False, env_flag="ONCA_CUSTOM")) is True


def test_gated_source_disabled_is_noop(monkeypatch):
    called = {"n": 0}
    recs, new = lp._gated_source(
        _spec(default_on=False), deadline=time.monotonic() + 100, per_source=90,
        fetch=lambda: called.__setitem__("n", called["n"] + 1) or [{"id": 1}])
    assert (recs, new) == ([], []) and called["n"] == 0     # fetch never ran


def test_gated_source_runs_fetch_delta_store(monkeypatch):
    monkeypatch.setattr(lp, "_new_since_last_run",
                        lambda key, docs, seed_if_empty=False: docs[:1])
    stored = {}
    recs, new = lp._gated_source(
        _spec(default_on=True, state_key="tk"), deadline=time.monotonic() + 100, per_source=90,
        fetch=lambda: [{"id": 1}, {"id": 2}],
        store=lambda r: stored.update(n=len(r)))
    assert [r["id"] for r in recs] == [1, 2] and [r["id"] for r in new] == [1]
    assert stored == {"n": 2}                               # store got the full records


def test_gated_source_swallows_fetch_error(monkeypatch):
    def boom():
        raise RuntimeError("upstream down")
    recs, new = lp._gated_source(
        _spec(default_on=True), deadline=time.monotonic() + 100, per_source=90, fetch=boom)
    assert (recs, new) == ([], [])                          # degrades, never raises
