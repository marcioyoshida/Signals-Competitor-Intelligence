import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import cohort

AS_OF = "2026-08-23"


def _d(days_ago):
    return (dt.date.fromisoformat(AS_OF) - dt.timedelta(days=days_ago)).isoformat()


def _card(entity, threat, days_ago, *, axis=None, cohort_slug=None, direction=None,
          tier=None):
    n = {
        "entity": entity,
        "narrative": f"{entity} activity",
        "threat_score": threat,
        "run_date": _d(days_ago),
        "lenses": ["market"],
        "citations": [{"url": f"https://ex/{entity}"}],
    }
    if axis:
        n["axis"] = axis
        n["mode"] = "derived"
        n["is_inference"] = True
    if cohort_slug:
        n["cohort"] = cohort_slug
    if direction:
        n["direction"] = direction
    if tier is not None:
        n["move_tier"] = tier
    return n


def _imap(members, slug="banking"):
    return {m: [slug] for m in members}


def _heating_window():
    members = ["a", "b", "c", "d"]
    narrs = []
    # baseline: 8 cool observations spread across days 8..28 ago
    for i, days in enumerate([28, 25, 22, 19, 16, 13, 11, 9]):
        narrs.append(_card(members[i % 4], 0.20, days))
    # recent: 4 hot observations in the last 5 days
    for i, days in enumerate([5, 4, 2, 1]):
        narrs.append(_card(members[i % 4], 0.85, days))
    return narrs, _imap(members)


def test_nominate_flags_heating_cohort():
    narrs, imap = _heating_window()
    cands = cohort.nominate(narrs, imap, as_of=AS_OF)
    assert len(cands) == 1
    c = cands[0]
    assert c["cohort"] == "banking" and c["direction"] == "heating"
    assert c["cohort_z"] > 1.5 and c["recent_temp"] > c["baseline_temp"]
    assert c["members"] == 4
    assert cohort.swot_hint(c) == {
        "dimension": "T", "sign": "-", "cohort": "banking",
        "entities": c["recent_members"]}


def test_small_cohort_not_compared():
    members = ["a", "b"]
    narrs = [_card(members[i % 2], 0.8, d) for i, d in enumerate([1, 2, 3, 10, 12, 14, 16])]
    assert cohort.nominate(narrs, _imap(members), as_of=AS_OF) == []


def test_insufficient_baseline_not_compared():
    members = ["a", "b", "c"]
    # only recent obs, no baseline
    narrs = [_card(members[i % 3], 0.8, d) for i, d in enumerate([1, 2, 3, 4])]
    assert cohort.nominate(narrs, _imap(members), as_of=AS_OF) == []


def test_cooling_maps_to_opportunity():
    members = ["a", "b", "c", "d"]
    narrs = []
    for i, days in enumerate([28, 25, 22, 19, 16, 13, 11, 9]):
        narrs.append(_card(members[i % 4], 0.85, days))   # hot baseline
    for i, days in enumerate([5, 4, 2, 1]):
        narrs.append(_card(members[i % 4], 0.10, days))   # cool recent
    c = cohort.nominate(narrs, _imap(members), as_of=AS_OF)[0]
    assert c["direction"] == "cooling"
    assert cohort.swot_hint(c)["dimension"] == "O" and cohort.swot_hint(c)["sign"] == "+"


def test_build_narrative_is_labeled_and_capped():
    narrs, imap = _heating_window()
    cand = cohort.nominate(narrs, imap, as_of=AS_OF)[0]
    n = cohort.build_narrative(cand)
    assert n["axis"] == "cohort" and n["subject_type"] == "set"
    assert n["is_inference"] and n["mode"] == "derived" and n["entity"] is None
    assert n["is_alert"] is False and n["threat_score"] <= 0.55
    assert "Movimento de cohort" in n["narrative"]
    assert n["swot_hint"]["dimension"] == "T"


def test_emit_on_change_suppresses_recent_same_tier():
    narrs, imap = _heating_window()
    cand = cohort.nominate(narrs, imap, as_of=AS_OF)[0]
    prior = _card("a", 0.0, 1, axis="cohort", cohort_slug="banking",
                  direction="heating", tier=cand["move_tier"])
    got = [c["cohort"] for c in cohort.nominate(narrs + [prior], imap, as_of=AS_OF)]
    assert "banking" not in got
