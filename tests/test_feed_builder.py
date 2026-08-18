import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import feed_builder


def _narr(id, entity, date, score, *, is_alert=False, lenses=None, citations=None):
    return {
        "id": id,
        "entity": entity,
        "entities": [entity] if entity else [],
        "as_of": date,
        "threat_score": score,
        "is_alert": is_alert,
        "lenses": lenses or ["ofertas", "pix"],
        "narrative": f"{entity} did something on {date}.",
        "citations": citations or [{"url": "https://dados.cvm.gov.br/x"}],
        "source_ids": ["cvm:1"],
        "kind": "entity_fusion",
        "mode": "llm",
    }


def test_build_feed_sorts_and_rolls_up():
    narratives = [
        _narr("a", "itau", "2026-08-12", 0.4),
        _narr("b", "nubank", "2026-08-13", 0.9, is_alert=True),
        _narr("c", "itau", "2026-08-13", 0.7),
    ]
    feed = feed_builder.build_feed(narratives, generated_at="2026-08-14T00:00:00+00:00")

    assert feed["as_of"] == "2026-08-13"
    assert feed["dates"] == ["2026-08-12", "2026-08-13"]
    # newest date first, then score desc within date
    assert [x["id"] for x in feed["feed"]] == ["b", "c", "a"]
    # KPIs scoped to latest date
    assert feed["kpis"]["narratives_latest"] == 2
    assert feed["kpis"]["alerts_latest"] == 1
    assert feed["kpis"]["entities_tracked"] == 2
    assert feed["kpis"]["narratives_total"] == 3


def test_build_feed_separates_run_date_from_data_date():
    # A lagging source (as_of stuck at 08-13) surfaced on later run days must
    # spread across the run-date timeline, while "dados de" stays the data date.
    narratives = [
        {**_narr("a", "itau", "2026-08-13", 0.4), "run_date": "2026-08-16"},
        {**_narr("b", "itau", "2026-08-13", 0.7), "run_date": "2026-08-17"},
    ]
    feed = feed_builder.build_feed(narratives)
    # window/timeline keyed by run_date, not the stale data date
    assert feed["dates"] == ["2026-08-16", "2026-08-17"]
    assert feed["run_date"] == "2026-08-17"
    # "dados de" reflects the underlying source date
    assert feed["as_of"] == "2026-08-13"
    itau = next(e for e in feed["entities"] if e["entity"] == "itau")
    assert [t["date"] for t in itau["timeline"]] == ["2026-08-16", "2026-08-17"]
    # KPIs scope to the latest run day
    assert feed["kpis"]["narratives_latest"] == 1


def test_build_feed_carries_reviews_and_defaults_empty():
    # reviews (ADR step 5) ride along on the feed for the read-only surface
    proposals = [{"review_id": "group_merge:a_b", "kind": "group_merge",
                  "member_label": "Fintech A", "leader_label": "BrandCo"}]
    feed = feed_builder.build_feed([_narr("a", "itau", "2026-08-18", 0.5)], reviews=proposals)
    assert feed["reviews"] == proposals
    # absent by default (never KeyErrors the frontend)
    assert feed_builder.build_feed([])["reviews"] == []


def test_build_feed_passes_run_at_time_suffix_through():
    # With multiple runs/day the card shows a time suffix; run_at must survive.
    narratives = [
        {**_narr("a", "itau", "2026-08-18", 0.5),
         "run_date": "2026-08-18", "run_at": "2026-08-18T12:30:04-03:00"},
    ]
    feed = feed_builder.build_feed(narratives)
    assert feed["feed"][0]["run_at"] == "2026-08-18T12:30:04-03:00"
    # Legacy narratives with no run_at degrade to an empty string, not a crash.
    legacy = feed_builder.build_feed([_narr("b", "itau", "2026-08-18", 0.5)])
    assert legacy["feed"][0]["run_at"] == ""


def test_build_feed_entity_timeline_peak_and_count():
    narratives = [
        _narr("a", "itau", "2026-08-12", 0.4),
        _narr("c", "itau", "2026-08-13", 0.7),
    ]
    feed = feed_builder.build_feed(narratives)
    itau = next(e for e in feed["entities"] if e["entity"] == "itau")
    assert itau["label"] == "Itaú"
    assert itau["peak_score"] == 0.7
    assert itau["total"] == 2
    assert [t["date"] for t in itau["timeline"]] == ["2026-08-12", "2026-08-13"]
    assert itau["timeline"][-1]["max_score"] == 0.7


def test_build_feed_counts_distinct_sources_from_citations():
    narratives = [
        _narr("a", "itau", "2026-08-13", 0.5, citations=[
            {"url": "https://dados.cvm.gov.br/x"},
            {"id": "bcb-auth:1", "source": "BCB-Autorizacoes"},
        ]),
        _narr("b", "nubank", "2026-08-13", 0.5, citations=[
            {"url": "https://dados.cvm.gov.br/y"},  # same host as itau's
        ]),
    ]
    feed = feed_builder.build_feed(narratives)
    # hosts: dados.cvm.gov.br (shared) + explicit BCB-Autorizacoes = 2
    assert feed["kpis"]["sources"] == 2


def test_build_feed_handles_empty():
    feed = feed_builder.build_feed([])
    assert feed["as_of"] is None
    assert feed["feed"] == []
    assert feed["entities"] == []
    assert feed["kpis"]["narratives_total"] == 0


def test_build_feed_tolerates_bad_scores_and_junk():
    narratives = [
        {"id": "x", "entity": "itau", "as_of": "2026-08-13", "threat_score": None},
        "not-a-dict",
        {"id": "y", "entity": "stone", "as_of": "2026-08-13", "threat_score": "0.8"},
    ]
    feed = feed_builder.build_feed(narratives)
    assert feed["kpis"]["narratives_total"] == 2
    scores = {x["id"]: x["threat_score"] for x in feed["feed"]}
    assert scores["x"] == 0.0
    assert scores["y"] == 0.8


class _FakeS3:
    """Minimal S3 stub: paginator over list_objects_v2 + get_object."""

    def __init__(self, objects):
        self._objects = objects  # {key: bytes}

    def get_paginator(self, name):
        objs = self._objects

        class _P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in objs if k.startswith(Prefix)]}

        return _P()

    def get_object(self, Bucket, Key):
        import io

        return {"Body": io.BytesIO(self._objects[Key])}


def test_load_recent_narratives_filters_by_window(monkeypatch):
    import datetime as dt

    class _FixedDate(dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 8, 14)

    monkeypatch.setattr(feed_builder.dt, "date", _FixedDate)

    old = json.dumps(_narr("old", "itau", "2026-07-01", 0.5)).encode()
    fresh = json.dumps(_narr("fresh", "itau", "2026-08-13", 0.9)).encode()
    s3 = _FakeS3(
        {
            "narratives/2026-07-01/old.json": old,
            "narratives/2026-08-13/fresh.json": fresh,
            "narratives/2026-08-13/notjson.txt": b"ignore",
        }
    )
    loaded = feed_builder.load_recent_narratives("bucket", window_days=14, s3=s3)
    ids = {n["id"] for n in loaded}
    assert ids == {"fresh"}  # old is outside the 14-day window; .txt skipped


def test_display_label_falls_back_to_kind_when_entity_less():
    assert feed_builder.display_label("itau", "entity_fusion") == "Itaú"
    assert feed_builder.display_label(None, "regulatory_fusion") == "Regulatório"
    assert feed_builder.display_label(None, "competitor:funds") == "Sinal de concorrente"
    assert feed_builder.display_label(None, None) == "Sinal de mercado"


def test_build_feed_labels_entity_less_narratives():
    feed = feed_builder.build_feed([
        {"id": "r", "entity": None, "kind": "regulatory_fusion",
         "as_of": "2026-08-13", "threat_score": 0.9},
    ])
    assert feed["feed"][0]["entity_label"] == "Regulatório"
