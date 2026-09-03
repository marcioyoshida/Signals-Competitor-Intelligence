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
        # Modern narratives carry threat_factors; presence keeps the score as-is
        # (only factor-less legacy narratives are recomputed on read).
        "threat_factors": {"signal": 0.6, "magnitude": 0.0, "novelty": 1.0, "breadth": 0.4},
        "is_alert": is_alert,
        "lenses": lenses or ["ofertas", "pix"],
        "narrative": f"{entity} did something on {date}.",
        "citations": citations or [{"url": "https://dados.cvm.gov.br/x"}],
        "source_ids": ["cvm:1"],
        "kind": "entity_fusion",
        "mode": "llm",
    }


def test_feed_json_serializes_decimal_from_registry_esg():
    # #30: ESG weight arrives as DynamoDB Decimal; feed.json must serialize it.
    from decimal import Decimal
    feed = feed_builder.build_feed(
        [],
        entity_attrs={"itau": {"label": "Itaú", "ticker": "ITUB4",
                               "esg": {"ise_b3": True, "ise_b3_weight_pct": Decimal("2.809")}}},
    )
    s = json.dumps(feed, ensure_ascii=False, default=feed_builder._json_default)
    assert '"ise_b3_weight_pct": 2.809' in s


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


def test_entity_momentum_weighted_count_of_expansion_lenses():
    # ADR 015 §3: momentum is a weighted COUNT of expansion-lens narratives.
    # entrants + ofertas weigh 2; funds/inf_diario/pix weigh 1; a card counts once
    # at its max expansion weight; non-expansion lenses (regulatory/news) add 0.
    narratives = [
        _narr("a", "itau", "2026-08-12", 0.5, lenses=["entrants"]),          # +2
        _narr("b", "itau", "2026-08-12", 0.5, lenses=["funds", "pix"]),      # +1 (max of 1,1)
        _narr("c", "itau", "2026-08-13", 0.5, lenses=["ofertas", "funds"]),  # +2 (max of 2,1)
        _narr("d", "itau", "2026-08-13", 0.5, lenses=["regulatory"]),        # +0 (not expansion)
    ]
    feed = feed_builder.build_feed(narratives)
    itau = next(e for e in feed["entities"] if e["entity"] == "itau")
    assert itau["momentum"] == 5
    assert isinstance(itau["momentum"], int)


def test_entity_momentum_defaults_zero_without_expansion_lenses():
    feed = feed_builder.build_feed(
        [_narr("a", "itau", "2026-08-12", 0.5, lenses=["regulatory", "news"])]
    )
    itau = next(e for e in feed["entities"] if e["entity"] == "itau")
    assert itau["momentum"] == 0


def test_entity_market_share_pct_resolves_and_is_null_for_unknown():
    # ADR 015 §3: market_share_pct joins the resolved IF.data store by entity_id;
    # an entity with no resolved IF.data row emits null (never an invented number).
    narratives = [
        _narr("a", "itau", "2026-08-12", 0.5),
        _narr("b", "nubank", "2026-08-12", 0.5),
    ]
    feed = feed_builder.build_feed(narratives, market_share={"itau": 18.7})
    itau = next(e for e in feed["entities"] if e["entity"] == "itau")
    nubank = next(e for e in feed["entities"] if e["entity"] == "nubank")
    assert itau["market_share_pct"] == 18.7
    assert nubank["market_share_pct"] is None
    # default (no store passed): every entity is null, key always present
    plain = feed_builder.build_feed(narratives)
    assert all(e["market_share_pct"] is None for e in plain["entities"])
    assert all("momentum" in e for e in plain["entities"])


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


def test_scope_feed_to_modules_saas_boundary_and_group_optin():
    # issue #48: per-tenant scoped feed keeps depth + groups, folds in an in-scope
    # parent's group children (ADR-017 opt-in), fail-closed on empty modules.
    narratives = [
        _narr("bk", "itau", "2026-08-20", 0.7),        # banking (licensed)
        _narr("ag", "agroca", "2026-08-20", 0.5),      # agri-funds (NOT licensed)
        _narr("kn", "kinea", "2026-08-20", 0.4),       # itau's sub-entity (group child)
    ]
    imap = {"itau": ["banking"], "agroca": ["agri-funds"], "kinea": ["agri-funds"]}
    attrs = {"itau": {"industries": ["banking"]},
             "agroca": {"industries": ["agri-funds"]},
             "kinea": {"industries": ["agri-funds"], "parent": "itau"}}
    feed = feed_builder.build_feed(narratives, industry_map=imap, industry_meta={}, entity_attrs=attrs)

    scoped = feed_builder.scope_feed_to_modules(feed, ["banking"])
    ids = {c["id"] for c in scoped["feed"]}
    # itau (licensed) + kinea (its group child) are in; agroca (unlicensed, no parent) is out.
    assert ids == {"bk", "kn"} and scoped["scoped_modules"] == ["banking"]
    assert scoped["groups"] == {"itau": ["kinea"]}  # only in-scope parents' groups
    assert set(scoped["entity_attrs"]) == {"itau", "kinea"} and "agroca" not in scoped["entity_attrs"]
    # fail closed
    assert feed_builder.scope_feed_to_modules(feed, [])["feed"] == []


def test_build_feed_emits_corporate_groups_and_entry_collapses_them():
    # ADR 017 Phase 3: entity `parent` links roll up into feed.groups {parent: [children]}.
    narratives = [_narr("n1", "btg", "2026-08-20", 0.6),
                  _narr("n2", "btg-ceres", "2026-08-20", 0.4)]
    imap = {"btg": ["investment-banking"], "btg-ceres": ["agri-funds"]}
    attrs = {"btg": {"industries": ["investment-banking"]},
             "btg-ceres": {"industries": ["agri-funds"], "parent": "btg"}}
    feed = feed_builder.build_feed(
        narratives, industry_map=imap, industry_meta={}, entity_attrs=attrs)
    assert feed["groups"] == {"btg": ["btg-ceres"]}
    # a parent whose child isn't tracked is dropped; entry feed carries no groups.
    entry = feed_builder.derive_entry_feed(feed)
    assert entry["groups"] == {}
    # btg (tier-1) is out of entry; its sub-entity btg-ceres is in.
    assert "btg" not in entry["entity_attrs"] and "btg-ceres" in entry["entity_attrs"]


def test_derive_entry_feed_drops_deep_cards_keeps_shallow():
    # issue #44: Entry is shallow public-filing only — a deep/derived-axis card in an
    # entry industry must be dropped; a shallow fact card kept.
    shallow = _narr("s1", "agroca", "2026-08-20", 0.5)          # plain public-filing fact
    deep_rel = _narr("d1", "agroca", "2026-08-20", 0.6); deep_rel["relation"] = "convergence"
    deep_inf = _narr("d2", "agroca", "2026-08-20", 0.6); deep_inf["is_inference"] = True
    deep_axis = _narr("d3", "agroca", "2026-08-20", 0.6); deep_axis["axis"] = "predictive"
    imap = {"agroca": ["agri-funds"]}
    attrs = {"agroca": {"industries": ["agri-funds"]}}
    feed = feed_builder.build_feed(
        [shallow, deep_rel, deep_inf, deep_axis],
        industry_map=imap, industry_meta={}, entity_attrs=attrs)
    entry = feed_builder.derive_entry_feed(feed)
    assert {c["id"] for c in entry["feed"]} == {"s1"}  # only the shallow fact survives
    # the full feed still carries all four (depth filter is entry-only).
    assert len(feed["feed"]) == 4


def test_derive_entry_feed_scopes_to_entry_industries_only():
    # itau (banking, higher tier), agroca (agri-funds, entry), betco (betting, entry),
    # dualco (agri-funds + banking — a multi-industry entity that touches entry).
    narratives = [
        _narr("n1", "itau", "2026-08-20", 0.9, is_alert=True),
        _narr("n2", "agroca", "2026-08-20", 0.5, is_alert=True),
        _narr("n3", "betco", "2026-08-20", 0.4),
        _narr("n4", "dualco", "2026-08-20", 0.6),
    ]
    imap = {"itau": ["banking"], "agroca": ["agri-funds"],
            "betco": ["betting"], "dualco": ["agri-funds", "banking"]}
    attrs = {k: {"industries": v} for k, v in imap.items()}
    meta = {s: {"display_name": s} for s in ["banking", "agri-funds", "betting"]}
    feed = feed_builder.build_feed(
        narratives, industry_map=imap, industry_meta=meta, entity_attrs=attrs
    )
    # ADR 017: each card carries its denormalized industries (union of its entities').
    by_id = {c["id"]: c for c in feed["feed"]}
    assert by_id["n1"]["industries"] == ["banking"]
    assert by_id["n4"]["industries"] == ["agri-funds", "banking"]  # dualco is multi-industry

    entry = feed_builder.derive_entry_feed(feed)

    assert entry["tier"] == "entry"
    # a multi-industry card's entry copy is scrubbed to the entry industries only.
    assert next(c for c in entry["feed"] if c["id"] == "n4")["industries"] == ["agri-funds"]
    # itau's banking card is gone; the three entry-touching entities remain.
    ids = {c["id"] for c in entry["feed"]}
    assert ids == {"n2", "n3", "n4"}
    ents = {e["entity"] for e in entry["entities"]}
    assert ents == {"agroca", "betco", "dualco"} and "itau" not in ents
    # the multi-industry entity shows ONLY its entry industry — no "banking" chip leaks.
    dual = next(e for e in entry["entities"] if e["entity"] == "dualco")
    assert dual["industries"] == ["agri-funds"]
    assert entry["entity_attrs"]["dualco"]["industries"] == ["agri-funds"]
    assert "banking" not in {o["slug"] for o in entry["industry_options"]}
    # KPIs recomputed for the slice.
    assert entry["kpis"]["narratives_total"] == 3
    assert entry["kpis"]["entities_tracked"] == 3


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


def test_industry_volume_covered_low_and_gap():
    # itau -> banking, nubank -> fintech; insurance has an entity but no narrative.
    imap = {"itau": ["banking"], "nubank": ["fintech"], "acme": ["insurance"]}
    meta = {
        "banking": {"display_name": "Banking", "tier": "premium"},
        "fintech": {"display_name": "Fintech", "tier": "entry"},
        "insurance": {"display_name": "Insurance", "tier": "mid"},
    }
    narratives = [
        _narr("a", "itau", "2026-08-12", 0.4),
        _narr("b", "itau", "2026-08-13", 0.7, is_alert=True),
        _narr("c", "itau", "2026-08-13", 0.9),
        _narr("d", "nubank", "2026-08-13", 0.5),  # fintech: single narrative
    ]
    feed = feed_builder.build_feed(narratives, industry_map=imap, industry_meta=meta)
    by = {i["slug"]: i for i in feed["industries"]}

    assert by["banking"]["narratives"] == 3 and by["banking"]["covered"]
    assert by["banking"]["alerts"] == 1
    assert by["banking"]["narratives_latest"] == 2  # latest date = 2026-08-13
    assert not by["banking"]["low_volume"]

    assert by["fintech"]["narratives"] == 1 and by["fintech"]["low_volume"]
    assert by["fintech"]["covered"] and not by["fintech"]["coverage_gap"]

    # insurance: an entity is tracked but nothing surfaced -> coverage gap
    assert by["insurance"]["entities"] == 1
    assert by["insurance"]["narratives"] == 0
    assert by["insurance"]["coverage_gap"] and not by["insurance"]["covered"]

    # taxonomy exposed for the review-queue pick control
    assert {"slug": "insurance", "display_name": "Insurance"} in feed["industry_options"]


def test_industry_volume_counts_each_narrative_once_per_module():
    # a narrative naming two banking entities counts once for banking.
    imap = {"itau": ["banking"], "bb": ["banking"]}
    meta = {"banking": {"display_name": "Banking", "tier": "premium"}}
    n = _narr("a", "itau", "2026-08-13", 0.5)
    n["entities"] = ["itau", "bb"]
    feed = feed_builder.build_feed([n], industry_map=imap, industry_meta=meta)
    banking = feed["industries"][0]
    assert banking["narratives"] == 1
    assert banking["active_entities"] == 2


def test_build_macro_selic_decision_and_focus_shift():
    import datetime as dt
    from src.dashboard.feed_builder import build_macro
    macro = {
        "selic": {"current": 14.0, "as_of": "2026-08-19",
                  "last_decision": {"date": dt.date.today().isoformat(),
                                    "previous": 14.25, "value": 14.0,
                                    "direction": "baixa", "bps": -25}},
        "focus": [
            {"indicator": "IPCA", "ref_year": 2026, "median": 5.05, "prev_median": 4.90, "delta": 0.15, "date": "2026-08-18"},
            {"indicator": "Selic", "ref_year": 2026, "median": 14.0, "prev_median": 14.0, "delta": 0.0, "date": "2026-08-18"},
        ],
    }
    out = build_macro(macro)
    assert out["selic"]["current"] == 14.0
    kinds = [c["kind"] for c in out["cards"]]
    assert "selic" in kinds                     # a decision card
    selic_card = next(c for c in out["cards"] if c["kind"] == "selic")
    assert selic_card["is_alert"] is True       # decision dated today -> recent
    assert "reduziu" in selic_card["detail"]
    # IPCA shifted 0.15 (>=0.05) -> a focus card; Selic flat -> none
    focus_titles = [c["title"] for c in out["cards"] if c["kind"] == "focus"]
    assert any("IPCA" in t for t in focus_titles)
    assert not any("Selic" in t for t in focus_titles)


def test_entities_carry_their_industry_slugs():
    # The fused coverage panel groups the entity monitor by industry, so each
    # entity must expose the industry slugs it belongs to.
    imap = {"itau": ["banking"], "nubank": ["fintech", "banking"]}
    meta = {"banking": {"display_name": "Banking", "tier": "premium"},
            "fintech": {"display_name": "Fintech", "tier": "entry"}}
    narratives = [
        _narr("a", "itau", "2026-08-13", 0.5),
        _narr("b", "nubank", "2026-08-13", 0.7),
    ]
    feed = feed_builder.build_feed(narratives, industry_map=imap, industry_meta=meta)
    by = {e["entity"]: e for e in feed["entities"]}
    assert by["itau"]["industries"] == ["banking"]
    assert by["nubank"]["industries"] == ["banking", "fintech"]  # sorted


def test_entities_industries_empty_without_map():
    feed = feed_builder.build_feed([_narr("a", "itau", "2026-08-13", 0.5)])
    assert feed["entities"][0]["industries"] == []


def test_legacy_scores_recomputed_new_scores_kept():
    # A legacy narrative (no threat_factors, stale saturated score) is recomputed
    # through the current model; a modern narrative (with factors) is left as-is.
    legacy = {"id": "L", "entity": "itau", "entities": ["itau"], "as_of": "2026-08-13",
              "run_date": "2026-08-13", "threat_score": 1.0, "is_alert": True,
              "lenses": ["entrants", "ofertas", "pix", "inf_diario", "market"]}
    modern = {**_narr("M", "bb", "2026-08-13", 0.62), "threat_factors": {"signal": 0.88}}
    feed = feed_builder.build_feed([legacy, modern])
    by = {f["id"]: f for f in feed["feed"]}
    assert by["L"]["threat_score"] < 0.8          # de-saturated from 1.0
    assert by["L"]["threat_factors"]              # now populated
    assert by["M"]["threat_score"] == 0.62        # modern score untouched


def test_build_macro_empty_is_safe():
    from src.dashboard.feed_builder import build_macro
    out = build_macro(None)
    assert out == {"selic": None, "focus": [], "cards": []}


def _thread(incident_id, entity, date, *, latest_dev_id, score=0.6):
    return {
        "id": f"threaded-{incident_id}", "kind": "threaded", "axis": "threaded",
        "subject_type": "incident", "entity": entity, "entities": [entity],
        "is_inference": True, "is_alert": False, "threat_score": score,
        "run_date": date, "as_of": date, "narrative": "Fio de incidente…",
        "lenses": ["news"], "citations": [], "source_ids": ["news:x"],
        "n_developments": 2, "status": "open",
        "latest_dev_id": latest_dev_id, "latest_dev_date": date,
    }


def test_thread_dropped_when_latest_dev_already_standalone():
    # the daily fusion card IS in the feed; the thread's latest dev re-shows it -> drop
    narratives = [_narr("cand-ent-bradesco", "bradesco", "2026-08-23", 0.6)]
    th = _thread("bradesco--restructuring", "bradesco", "2026-08-23",
                 latest_dev_id="cand-ent-bradesco")
    feed = feed_builder.build_feed(narratives, thread_cards=[th])
    ids = [x["id"] for x in feed["feed"]]
    assert "cand-ent-bradesco" in ids
    assert "threaded-bradesco--restructuring" not in ids   # suppressed as duplicate


def test_thread_kept_when_latest_dev_not_in_feed():
    # latest dev card is NOT independently in the feed (e.g. outside window) -> keep
    narratives = [_narr("cand-ent-bradesco", "bradesco", "2026-08-10", 0.6)]
    th = _thread("bradesco--restructuring", "bradesco", "2026-08-23",
                 latest_dev_id="cand-ent-bradesco")  # date 23 not present for this id
    feed = feed_builder.build_feed(narratives, thread_cards=[th])
    assert "threaded-bradesco--restructuring" in [x["id"] for x in feed["feed"]]


def test_instrument_card_keeps_self_declared_industries():
    # #51 nexus: a regulatory instrument card (no entity) carries its affected cohort;
    # the ADR-017 entity-denorm must NOT wipe it off every industry tab.
    reg_card = {
        "id": "reg-lifecycle-res-cmn-5304", "kind": "regulatory_lifecycle",
        "axis": "regulatory_lifecycle", "subject_type": "instrument",
        "instrument": "res-cmn-5304", "instrument_label": "Resolução CMN 5304",
        "industries": ["banking", "investment-banking"],
        "entity": None, "entities": [], "run_date": "2026-08-28", "as_of": "2026-08-28",
        "lenses": ["regulatory"], "narrative": "Ciclo regulatório: Resolução CMN 5304 ...",
        "citations": [{"url": "https://www.bcb.gov.br/x?numero=5304"}],
    }
    narratives = [_narr("n1", "itau", "2026-08-28", 0.6)]
    feed = feed_builder.build_feed(
        narratives, thread_cards=[reg_card],
        industry_map={"itau": ["banking"]},
        entity_attrs={"itau": {"industries": ["banking"]}},
    )
    card = next(c for c in feed["feed"] if c["id"] == "reg-lifecycle-res-cmn-5304")
    assert card["industries"] == ["banking", "investment-banking"]


def test_regulatory_coverage_scan_present_in_full_feed_but_stripped_when_scoped():
    # #2: the CVM/BCB coverage scan is an operator artifact — in the full feed, not the
    # per-tenant or entry slices.
    narratives = [_narr("n1", "itau", "2026-09-02", 0.6)]
    feed = feed_builder.build_feed(
        narratives, industry_map={"itau": ["banking"]},
        entity_attrs={"itau": {"industries": ["banking"]}})
    rc = feed.get("regulatory_coverage") or {}
    assert rc.get("summary", {}).get("segments", 0) >= 10
    assert rc["summary"]["by_regulator"].get("BCB") and rc["summary"]["by_regulator"].get("CVM")
    assert not feed_builder.scope_feed_to_modules(feed, ["banking"]).get("regulatory_coverage")
    assert not feed_builder.derive_entry_feed(feed).get("regulatory_coverage")


def test_instrument_cards_dedup_to_latest_but_entity_timelines_keep_all():
    # same instrument id emitted on two dates -> only the latest survives (no stale copy);
    # an entity id across two dates is a timeline -> both kept.
    def _inst(date, cites):
        return {"id": "regulatory-res-cmn-5304", "kind": "regulatory_lifecycle",
                "axis": "regulatory", "subject_type": "instrument", "entity": None,
                "entities": [], "industries": ["banking"], "run_date": date, "as_of": date,
                "lenses": ["regulatory"], "narrative": f"x {date}",
                "citations": cites, "threat_factors": {"days_to_deadline": None}}
    narratives = [
        _inst("2026-08-31", [{"url": "https://bcb/x?numero=5337"}]),   # stale, wrong link
        _inst("2026-09-02", [{"url": "https://bcb/x?numero=5304"}]),   # fresh
        _narr("cand-ent-itau", "itau", "2026-09-01", 0.6),
        _narr("cand-ent-itau", "itau", "2026-09-02", 0.7),             # next day's event
    ]
    feed = feed_builder.build_feed(
        narratives, industry_map={"itau": ["banking"]},
        entity_attrs={"itau": {"industries": ["banking"]}})
    reg = [c for c in feed["feed"] if c["id"] == "regulatory-res-cmn-5304"]
    assert len(reg) == 1 and reg[0]["date"] == "2026-09-02"
    assert len([c for c in feed["feed"] if c["id"] == "cand-ent-itau"]) == 2


# --- #70: entity-less regulatory cards scoped to tenant read boundaries by domain ---

def _reg_card(id, domain, *, date="2026-09-02", axis="regulatory_lifecycle"):
    """A regulatory card as build_narrative / build_lifecycle_card emit it: no entity,
    an affected `domain`, is_inference. It carries NO `industries` key."""
    return {
        "id": id,
        "kind": "regulatory_lifecycle",
        "axis": axis,
        "subject_type": "instrument",
        "domain": domain,
        "entity": None,
        "entities": [],
        "is_inference": True,
        "threat_score": 0.4,
        "threat_factors": {"has_deadline": True},
        "lenses": ["regulatory"],
        "run_date": date,
        "as_of": date,
        "narrative": f"Radar regulatório — {domain}.",
        "citations": [{"url": "https://bcb/exibenormativo?numero=1"}],
        "source_ids": [],
        "mode": "derived",
    }


# Universe used for the scoping tests (mirrors entity_registry.INDUSTRIES subset).
_UNIVERSE_META = {
    s: {"display_name": s}
    for s in ["banking", "fintech", "acquiring", "insurance", "asset-management",
              "investment-banking", "wealth-management", "crypto"]
}


def test_regulatory_card_scoped_by_domain_survives_saas_boundary_70():
    # #70 regression: a Pagamentos/PIX rule (no entity) must reach the fintech/acquiring
    # tenants (empty Radar Regulatório before the fix) but NOT insurance/wealth.
    feed = feed_builder.build_feed(
        [_narr("bk", "itau", "2026-09-02", 0.6)],
        industry_map={"itau": ["banking"]},
        industry_meta=_UNIVERSE_META,
        entity_attrs={"itau": {"industries": ["banking"]}},
        thread_cards=[_reg_card("reg-pix", "Pagamentos / PIX")],
    )
    reg = next(c for c in feed["feed"] if c["id"] == "reg-pix")
    # domain -> licensed verticals (intersected with the universe), never all-industries.
    assert reg["industries"] == ["acquiring", "banking", "fintech"]
    assert reg["domain"] == "Pagamentos / PIX"  # carried through _project_item

    for mod in ("fintech", "acquiring", "banking"):
        ids = {c["id"] for c in feed_builder.scope_feed_to_modules(feed, [mod])["feed"]}
        assert "reg-pix" in ids, f"PIX rule should reach {mod}"
    for mod in ("insurance", "wealth-management"):
        ids = {c["id"] for c in feed_builder.scope_feed_to_modules(feed, [mod])["feed"]}
        assert "reg-pix" not in ids, f"PIX rule should NOT reach {mod}"


def test_sector_wide_regulatory_card_reaches_every_tenant_but_not_entry_70():
    # A catch-all "Setor financeiro" rule (unclassified) is recall-first: visible to every
    # licensed tenant. It still must NOT leak into the shallow Entry feed (#44 depth gate).
    feed = feed_builder.build_feed(
        [_narr("bk", "itau", "2026-09-02", 0.6)],
        industry_map={"itau": ["banking"]},
        industry_meta=_UNIVERSE_META,
        entity_attrs={"itau": {"industries": ["banking"]}},
        thread_cards=[_reg_card("reg-all", "Setor financeiro")],
    )
    reg = next(c for c in feed["feed"] if c["id"] == "reg-all")
    assert reg["industries"] == sorted(_UNIVERSE_META)  # sector-wide -> all industries

    for mod in ("fintech", "insurance", "wealth-management", "acquiring"):
        ids = {c["id"] for c in feed_builder.scope_feed_to_modules(feed, [mod])["feed"]}
        assert "reg-all" in ids, f"sector-wide rule should reach {mod}"
    # fail-closed: empty modules still yields no cards.
    assert feed_builder.scope_feed_to_modules(feed, [])["feed"] == []
    # Entry drops it despite spanning entry industries (crypto in universe): it is an
    # inference card, and Entry is shallow-public-filing only (#44).
    entry_ids = {c["id"] for c in feed_builder.derive_entry_feed(feed)["feed"]}
    assert "reg-all" not in entry_ids
