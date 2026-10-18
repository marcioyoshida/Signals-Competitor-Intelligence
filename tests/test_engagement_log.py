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


# --- #287: the interaction list, ids-only keys, per-kind counts --------------------------------

import pytest  # noqa: E402


@pytest.mark.parametrize("kind,action", [(k, a) for k, acts in el.KINDS.items() for a in sorted(acts)])
def test_every_listed_interaction_records(kind, action):
    it = el.record_engagement(kind=kind, action=action, actor="u1", key="cso", table=_T())
    assert (it["kind"], it["action"], it["key"]) == (kind, action, "cso")


@pytest.mark.parametrize("kind,action", [("headline", "open"), ("signin", "open"), ("question", "open"),
                                         ("source", "open"), ("drawer", "sent")])
def test_an_unlisted_pair_is_refused(kind, action):
    t = _T()
    with pytest.raises(ValueError):
        el.record_engagement(kind=kind, action=action, actor="u1", table=t)
    assert t.items == {}


@pytest.mark.parametrize("key", ["https://x.com/a", "O que mudou esta semana?", "a" * 49, "Bradesco SA", "<b>", ""])
def test_a_key_that_is_not_a_short_id_is_dropped(key):
    t = _T()
    el.record_engagement(kind="tile", action="open", actor="u1", key=key, table=t)
    assert "key" not in list(t.items.values())[0]


def test_ids_and_enums_survive_lowercased():
    for k in ("sector_events", "combo:bancos-digitais", "ex2", "SugestaO"):
        assert el.record_engagement(kind="tile", action="open", actor="u1", key=k, table=_T())["key"] == k.lower()


def test_empty_action_defaults_keep_the_old_callers():
    assert el.record_engagement(kind="headline", actor="u1", table=_T())["action"] == "expand"
    assert el.record_engagement(kind="session", actor="u1", table=_T())["action"] == "open"


def test_by_kind_counts_and_housekeeping_is_not_interest():
    evs = [{"kind": "drawer", "action": "open", "tenant": "acme"},
           {"kind": "drawer", "action": "open", "tenant": "acme"},
           {"kind": "source", "action": "follow", "tenant": "acme"},
           {"kind": "signin", "action": "password", "tenant": "acme"},
           {"kind": "upgrade", "action": "open", "tenant": "acme"},
           {"kind": "nav", "action": "open", "tenant": "acme"},
           {"kind": "report", "action": "open", "tenant": "acme"},
           {"kind": "drawer", "action": "open", "tenant": "qa-internal-entry"}]
    roll = el.aggregate(evs)
    assert roll["by_kind"] == {"drawer": {"open": 2}, "nav": {"open": 1}, "report": {"open": 1},
                               "signin": {"password": 1}, "source": {"follow": 1}, "upgrade": {"open": 1}}
    assert roll["n_interest"] == 3 and roll["internal_excluded"] == 1
