import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import feature_store as fs
from src.synth import silence


def _narr(entity, run_date, score=0.5, **extra):
    n = {
        "entity": entity,
        "entity_label": entity.title(),
        "run_date": run_date,
        "threat_score": score,
        "lenses": ["regulatory"],
    }
    n.update(extra)
    return n


def _silence_card(entity, run_date, tier):
    # A prior silence narrative as it would be re-read from narratives/.
    return {"entity": entity, "run_date": run_date, "axis": "silence", "silence_tier": tier}


def test_quiet_regular_entity_is_nominated():
    # itau: regular 3-day cadence, then goes dark for 3 weeks.
    narrs = [
        _narr("itau", "2026-06-01"),
        _narr("itau", "2026-06-04"),
        _narr("itau", "2026-06-07"),
        _narr("itau", "2026-06-10", 0.6),
    ]
    feats = fs.build_features(narrs, as_of="2026-07-01")
    cands = silence.nominate(feats, recent_narratives=narrs, as_of="2026-07-01")

    assert [c["entity"] for c in cands] == ["itau"]
    c = cands[0]
    assert c["days_since_last"] == 21  # 2026-07-01 - 2026-06-10
    assert c["mean_gap_days"] == 3.0
    assert c["cadence_regular"] is True
    assert c["silence_tier"] == 3  # ratio 7 -> tier 3


def test_recently_active_entity_not_flagged():
    narrs = [
        _narr("nubank", "2026-06-22"),
        _narr("nubank", "2026-06-25"),
        _narr("nubank", "2026-06-28"),
        _narr("nubank", "2026-07-01"),  # active on as_of
    ]
    feats = fs.build_features(narrs, as_of="2026-07-01")
    cands = silence.nominate(feats, recent_narratives=narrs, as_of="2026-07-01")
    assert cands == []


def test_insufficient_history_skipped():
    # Only two active days -> no established cadence, never "silent".
    narrs = [_narr("stone", "2026-06-01"), _narr("stone", "2026-06-05")]
    feats = fs.build_features(narrs, as_of="2026-07-01")
    cands = silence.nominate(feats, recent_narratives=narrs, as_of="2026-07-01")
    assert cands == []


def test_cooldown_suppresses_but_escalation_refires():
    narrs = [
        _narr("btg", "2026-06-01"),
        _narr("btg", "2026-06-04"),
        _narr("btg", "2026-06-07"),
        _narr("btg", "2026-06-10"),
    ]
    feats = fs.build_features(narrs, as_of="2026-07-01")  # 21 days quiet -> tier 3

    # Prior silence 2 days ago at the SAME tier -> suppressed (within cooldown).
    with_prev_same = narrs + [_silence_card("btg", "2026-06-29", tier=3)]
    assert silence.nominate(feats, recent_narratives=with_prev_same, as_of="2026-07-01") == []

    # Prior silence 2 days ago at a LOWER tier -> escalation re-fires.
    with_prev_lower = narrs + [_silence_card("btg", "2026-06-29", tier=1)]
    cands = silence.nominate(feats, recent_narratives=with_prev_lower, as_of="2026-07-01")
    assert [c["entity"] for c in cands] == ["btg"]

    # A silence card is not activity: freshness still counts 2026-06-10, not 06-29.
    assert cands[0]["last_seen"] == "2026-06-10"


def test_freshness_recompute_beats_stale_snapshot():
    # Features snapshot says itau last active 06-10 (silent), but recent activity
    # shows a fresh 06-30 signal the pre-synth snapshot missed -> not flagged.
    narrs = [
        _narr("itau", "2026-06-01"),
        _narr("itau", "2026-06-04"),
        _narr("itau", "2026-06-07"),
        _narr("itau", "2026-06-10"),
    ]
    feats = fs.build_features(narrs, as_of="2026-07-01")
    recent = narrs + [_narr("itau", "2026-06-30")]
    cands = silence.nominate(feats, recent_narratives=recent, as_of="2026-07-01")
    assert cands == []


def test_build_narrative_is_labeled_inference_without_citations():
    cand = {
        "entity": "itau",
        "label": "Itaú",
        "days_since_last": 21,
        "last_seen": "2026-06-10",
        "mean_gap_days": 3.0,
        "active_days": 4,
        "cadence_regular": True,
        "score_mean": 0.55,
        "industries": ["banking"],
        "ratio": 7.0,
        "silence_tier": 3,
    }
    card = silence.build_narrative(cand)
    assert card["id"] == "silence-itau"
    assert card["axis"] == "silence" and card["subject_type"] == "entity"
    assert card["is_inference"] is True and card["mode"] == "derived"
    assert card["is_alert"] is False
    assert card["citations"] == [] and card["source_ids"] == []
    assert 0 < card["threat_score"] <= silence.MAX_SILENCE_SCORE
    assert "http" not in card["narrative"]  # silence asserts an absence, no URL
    assert "21 dias" in card["narrative"] and "Itaú" in card["narrative"]


def test_retract_same_day_removes_only_recovered_cards():
    # itau recovered today (has a stale same-day silence card); btg still silent.
    deleted, heads = [], set()

    class FakeS3:
        def head_object(self, Bucket, Key):
            if Key not in store:
                raise KeyError(Key)
            return {}

        def delete_object(self, Bucket, Key):
            deleted.append(Key)
            store.discard(Key)

    store = {
        "narratives/2026-07-01/silence-itau.json",
        "narratives/2026-07-01/silence-btg.json",
    }
    out = silence._retract_same_day(
        "onca-digests", FakeS3(), "2026-07-01", recovered={"itau", "nubank"}
    )
    # itau's card retracted; nubank had none; btg untouched (still silent).
    assert out == ["itau"]
    assert deleted == ["narratives/2026-07-01/silence-itau.json"]
    assert "narratives/2026-07-01/silence-btg.json" in store


def test_feature_store_excludes_silence_from_activity():
    # A silence card mixed into history must not register as activity.
    activity = [_narr("itau", "2026-06-10")]
    silence_card = {
        "entity": "itau", "run_date": "2026-06-30", "axis": "silence",
        "threat_score": 0.5, "is_inference": True, "mode": "derived",
    }
    feats = fs.build_features(activity + [silence_card], as_of="2026-07-01")
    itau = {e["entity"]: e for e in feats["entities"]}["itau"]
    assert itau["active_days"] == 1  # silence card ignored
    assert itau["last_seen"] == "2026-06-10"
