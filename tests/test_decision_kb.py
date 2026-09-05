"""ADR 021 §H/§F — decision→KB promotion + officer-reference seeding."""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import decision_kb


class _S3:
    def __init__(self, existing=None):
        self.objs = dict(existing or {})
        self.puts = []
    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objs[Key] = Body; self.puts.append(Key)
    def get_object(self, Bucket, Key):
        if Key not in self.objs:
            raise Exception("NoSuchKey")
        return {"Body": type("B", (), {"read": lambda s: self.objs[Key]})()}


def test_promote_only_closed_and_unpromoted():
    s3 = _S3(); marked = []
    ds = [
        {"decision_id": "d1", "officer": "cso", "verdict": "aprovado", "outcome": "favoravel", "created_at": "2026-09-05"},
        {"decision_id": "d2", "officer": "cro", "verdict": "aprovado", "outcome": "pendente"},   # not closed
        {"decision_id": "d3", "officer": "cco", "outcome": "favoravel", "kb_promoted": True},     # already promoted
    ]
    out = decision_kb.promote_decisions(ds, bucket="b", s3=s3, mark=lambda did: marked.append(did))
    assert out == ["d1"] and marked == ["d1"]
    assert "decisions/d1.txt" in s3.objs and "decisions/d1.txt.metadata.json" in s3.objs
    meta = json.loads(s3.objs["decisions/d1.txt.metadata.json"])
    assert meta["metadataAttributes"]["doc_type"] == "decision_precedent"
    assert meta["metadataAttributes"]["officer"] == "cso"


def test_decision_doc_includes_outcome_and_refs():
    doc = decision_kb.decision_doc({"officer": "cso", "recommendation": "Abrir watch", "verdict": "aprovado",
                                    "outcome": "favoravel", "references": [{"url": "https://x/1"}]})
    assert "aprovado" in doc and "favoravel" in doc and "https://x/1" in doc


def test_seed_reference_is_idempotent_by_hash():
    ref = {"cso": {"title": "Playbook", "sections": [{"h": "A", "items": ["x", "y"]}]}}
    s3 = _S3()
    first = decision_kb.seed_reference(ref, bucket="b", s3=s3)
    assert first == ["cso"] and "reference/cso.txt" in s3.objs
    # second run, unchanged content → skipped (hash marker matches)
    again = decision_kb.seed_reference(ref, bucket="b", s3=s3)
    assert again == []
