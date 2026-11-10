"""#377: the label-only radar source line reaches the curated framework feed rows."""
import json

from src.dashboard import feed_builder
from src.synth import ansoff, four_corners


class _S3:
    def __init__(self, objs):
        self.objs = objs

    def get_object(self, Bucket, Key):
        import io
        if Key not in self.objs:
            raise KeyError(Key)
        return {"Body": io.BytesIO(json.dumps(self.objs[Key]).encode())}


def test_store_keys_match_framework_modules():
    assert feed_builder.RADAR_PROPOSAL_STORES == {
        "ansoff": ansoff.ANSOFF_PROPOSALS_KEY, "four_corners": four_corners.FOUR_CORNERS_PROPOSALS_KEY}


def test_radar_by_proposal_is_label_only():
    doc = {"proposals": [{"id": "p1", "radar_evidence": [{"id": "r1", "label": "L", "title": "x", "url": "u"}]},
                         {"id": "p2", "radar_evidence": [{"label": "no id"}]}, {"id": "p3"}]}
    assert feed_builder._radar_by_proposal(doc) == {"p1": [{"id": "r1", "label": "L"}]}
    assert feed_builder._radar_by_proposal(None) == {}


def test_load_fw_curated_joins_radar_line_for_pre_fix_bullets(monkeypatch):
    curated = {"bullets": [
        {"id": "a1", "framework": "ansoff", "entity": "itau", "dimension": "product_dev", "text": "t",
         "evidence": ["n1", "r1"]},
        {"id": "a2", "framework": "ansoff", "entity": "itau", "dimension": "penetration", "text": "u",
         "evidence": ["n2"], "radar_evidence": [{"id": "r9", "label": "kept"}]},
        {"id": "b1", "framework": "bcg", "entity": "itau", "dimension": "star", "text": "v"}]}
    objs = {"swot/curated.json": curated,
            "ansoff/proposals.json": {"proposals": [{"id": "a1", "radar_evidence": [{"id": "r1", "label": "Itaú — lançamento"}]}]}}
    monkeypatch.setattr(feed_builder.boto3, "client", lambda *_a, **_k: _S3(objs))
    rows = feed_builder._load_fw_curated("b", "ansoff")["itau"]
    assert rows[0]["radar_evidence"] == [{"id": "r1", "label": "Itaú — lançamento"}] and rows[0]["n_evidence"] == 2
    assert rows[1]["radar_evidence"] == [{"id": "r9", "label": "kept"}]
    bcg = feed_builder._load_fw_curated("b", "bcg")["itau"]        # no proposals store read for BCG
    assert "radar_evidence" not in bcg[0] and bcg[0]["n_evidence"] == 0
