"""ADR 021 §E — engagement telemetry store + attention rollup."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import engagement_log as el


class _T:
    def __init__(self): self.items = {}
    def put_item(self, Item): self.items[Item["pk"]] = dict(Item)


def test_record_engagement_appends_tagged_event():
    t = _T()
    it = el.record_engagement(kind="headline", actor="op", officer="cso", sector="banking",
                              card_id="n1", entity="itau", action="expand", threat_score=0.8,
                              industries=["banking"], table=t)
    assert it["type"] == "engagement" and it["action"] == "expand" and it["entity"] == "itau"
    assert list(t.items)[0].startswith("ENGAGEMENT#")


def test_aggregate_attention_by_entity_and_sector():
    evs = [{"action": "expand", "entity": "itau", "officer": "cso", "industries": ["banking"]},
           {"action": "expand", "entity": "itau", "officer": "cso", "industries": ["banking"]},
           {"action": "expand", "entity": "nubank", "officer": "cso", "industries": ["fintech"]},
           {"action": "collapse", "entity": "itau", "officer": "cso", "industries": ["banking"]}]
    roll = el.aggregate(evs, labels={"itau": "Itaú", "nubank": "Nubank"})
    assert roll["n_events"] == 4 and roll["n_interest"] == 3  # collapse is neutral
    assert roll["top_entities"][0] == {"entity": "itau", "label": "Itaú", "hits": 2}
    assert {"sector": "banking", "hits": 2} in roll["top_sectors"]
    assert roll["actions"] == {"expand": 3, "collapse": 1}
