"""SURF-9 (#89): shared, env-overridable framework evidence cap (raised 12 -> 20)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import framework_common, porter


def test_evidence_cap_default_and_env(monkeypatch):
    monkeypatch.delenv("ONCA_FRAMEWORK_EVIDENCE_CAP", raising=False)
    assert framework_common.evidence_cap() == 20
    monkeypatch.setenv("ONCA_FRAMEWORK_EVIDENCE_CAP", "35")
    assert framework_common.evidence_cap() == 35
    monkeypatch.setenv("ONCA_FRAMEWORK_EVIDENCE_CAP", "junk")
    assert framework_common.evidence_cap() == 20  # bad value -> default


def test_collect_evidence_uses_shared_cap(monkeypatch):
    monkeypatch.delenv("ONCA_FRAMEWORK_EVIDENCE_CAP", raising=False)
    narrs = [{"id": f"n{i}", "threat_score": 0.5, "narrative": f"claim distinct number {i} about the market"}
             for i in range(40)]
    out = porter._collect_evidence_ids(narrs)
    assert len(out) == 20  # new default cap
    assert len(porter._collect_evidence_ids(narrs, max_claims=5)) == 5  # explicit override honored
