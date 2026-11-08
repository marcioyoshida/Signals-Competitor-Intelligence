"""#371–#375: CPO-radar (YouTube) evidence wired into Thematic, Predictive, Four Corners,
Comparative and Threads — confirmed events only, never a gate on its own, never in the
executive panels, and nothing but the radar id + Onça labels in what gets persisted."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import (ansoff, comparative, executive, four_corners, predictive,  # noqa: E402
                       radar_evidence, swot_store, thematic, threads)

AS_OF = "2026-11-08"
YT_ONLY_FIELDS = ("url", "sources", "reason", "channel", "title")


def _ev(entity, etype="launch", *, date="2026-11-05", conf="oficial", n_ch=1, eid=None,
        title="Pix parcelado no app", label=None):
    return {"id": eid or f"radar:{entity}-{etype}-{date}", "product": entity,
            "product_label": label or entity.title(), "entity": entity, "source": "youtube",
            "type": etype, "type_label": {"launch": "lançamento", "feature": "funcionalidade",
                                          "outage": "instabilidade", "complaint": "reclamação",
                                          "price": "preço/benefício"}[etype],
            "date": date, "last_seen": date, "title": title, "reason": "porque sim",
            "confidence": conf, "official": conf == "oficial", "n_sources": n_ch,
            "n_channels": n_ch, "url": "https://youtube.com/watch?v=x",
            "sources": [{"url": "https://youtube.com/watch?v=x", "title": "vídeo", "channel": "c"}]}


def _radar(events, products=None, alerts=None):
    ents = products if products is not None else sorted({e["entity"] for e in events if e["entity"]})
    return {"as_of": AS_OF, "events": events, "alerts": alerts or [],
            "products": [{"id": e, "name": e.title(), "entity": e} for e in ents]}


def _no_youtube_data(obj):
    """Every radar-derived object we persist: no YouTube title/url/channel/description."""
    blob = json.dumps(obj, ensure_ascii=False)
    for bad in ("youtube.com", "Pix parcelado", "porque sim", "vídeo"):
        assert bad not in blob, bad


# --- shared helper --------------------------------------------------------------------------
def test_confirmed_events_excludes_single_creator_and_unresolved():
    evs = [_ev("itau"), _ev("nubank", conf="corroborado", n_ch=3),
           _ev("inter", conf="criador único"), {**_ev("c6"), "entity": None}]
    got = radar_evidence.confirmed_events(_radar(evs))
    assert sorted(e["entity"] for e in got) == ["itau", "nubank"]


def test_label_only_claim_and_citation_carry_no_youtube_data():
    ev = _ev("itau", conf="corroborado", n_ch=7)
    lab = radar_evidence.label_only(ev)
    assert not set(lab) & set(YT_ONLY_FIELDS)
    assert radar_evidence.claim(ev) == "Itau — lançamento (YouTube, 7 canais, 05/11/2026)"
    _no_youtube_data([lab, radar_evidence.claim(ev), radar_evidence.citation(ev)])
    assert "Pix parcelado" in radar_evidence.prompt_claim(ev)  # transient prompt text only


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("ONCA_RADAR_EVIDENCE", "0")
    assert radar_evidence.load("bucket") == {}


# --- #371 Thematic --------------------------------------------------------------------------
def _crypto(entity, date="2026-11-06"):
    return {"id": f"n-{entity}-{date}", "entity": entity, "narrative": f"{entity} amplia oferta de cripto.",
            "threat_score": 0.5, "run_date": date, "lenses": ["market"],
            "citations": [{"url": f"https://ex/{entity}"}]}


def _crypto_ev(entity):
    return _ev(entity, "feature", title="suspensão de saques de criptomoedas", conf="corroborado", n_ch=5)


def test_thematic_radar_widens_entity_gate_but_never_counts_as_mentions():
    # 2 narrative entities, 4 mentions: below MIN_ENTITIES (3) without the radar.
    narr = [_crypto("mb"), _crypto("mb", "2026-11-05"), _crypto("foxbit"), _crypto("foxbit", "2026-11-04")]
    assert thematic.nominate(narr, as_of=AS_OF) == []
    cands = thematic.nominate(narr, as_of=AS_OF, radar_events=[_crypto_ev("nubank")])
    assert [c["theme"] for c in cands] == ["crypto"]
    c = cands[0]
    assert c["entity_count"] == 3 and c["mentions"] == 4
    assert c["radar_backed"] is True and c["radar_entities"] == ["nubank"]
    card = thematic.build_narrative(c)
    assert card["radar_backed"] is True and "nubank" in card["entities"]
    # narrative citations first, radar after; persisted card carries no YouTube data
    assert card["citations"][0]["url"] == "https://ex/mb"
    assert card["citations"][-1]["id"].startswith("radar:")
    _no_youtube_data([card["citations"][-1], card["narrative"], card["radar_entities"]])


def test_thematic_radar_alone_never_opens_or_passes_a_theme():
    evs = [_crypto_ev(e) for e in ("nubank", "itau", "mercado_pago", "picpay")]
    assert thematic.nominate([], as_of=AS_OF, radar_events=evs) == []
    # 1 narrative mention + 4 radar entities: MIN_MENTIONS (4) counts narratives only
    assert thematic.nominate([_crypto("mb")], as_of=AS_OF, radar_events=evs) == []


def test_thematic_not_radar_backed_when_narratives_pass_alone():
    narr = [_crypto(e) for e in ("mb", "foxbit", "binance")] + [_crypto("mb", "2026-11-05")]
    c = thematic.nominate(narr, as_of=AS_OF, radar_events=[_crypto_ev("nubank")])[0]
    assert c["radar_backed"] is False and c["entity_count"] == 4


def test_thematic_ignores_single_creator_and_stale_radar():
    narr = [_crypto("mb"), _crypto("mb", "2026-11-05"), _crypto("foxbit"), _crypto("foxbit", "2026-11-04")]
    stale = {**_crypto_ev("nubank"), "date": "2026-10-01"}
    evs = radar_evidence.confirmed_events(_radar([stale, {**_crypto_ev("itau"), "confidence": "criador único"}]))
    assert thematic.nominate(narr, as_of=AS_OF, radar_events=evs) == []


# --- executive / feed: radar-backed cards stay out of the panels ----------------------------
def test_executive_skips_radar_backed_cards():
    feed = {"feed": [{"id": "a", "threat_score": 0.9}, {"id": "b", "threat_score": 0.9, "radar_backed": True}]}
    assert [c["id"] for c in executive._cards(feed)] == ["a"]


def test_feed_projection_carries_radar_backed():
    from src.dashboard import feed_builder
    assert feed_builder._project_item({"id": "x", "radar_backed": True})["radar_backed"] is True
    assert feed_builder._project_item({"id": "y"})["radar_backed"] is False


# --- #375 Threads ---------------------------------------------------------------------------
def test_service_outage_taxonomy_is_specific():
    assert threads.event_types_of({"narrative": "App do Nubank fora do ar nesta manhã"}) == ["service_outage"]
    assert "service_outage" in threads.event_types_of({"narrative": "Instabilidade no Pix do Itaú"})
    assert threads.event_types_of({"narrative": "instabilidade financeira global"}) == []


def test_radar_only_outage_thread_forms_but_never_alerts():
    r = _radar([_ev("nubank", "outage", date="2026-11-06", conf="corroborado", n_ch=7, eid="radar:a"),
                _ev("nubank", "complaint", date="2026-11-07", conf="corroborado", n_ch=3, eid="radar:b"),
                _ev("itau", "outage", date="2026-11-07", conf="criador único", eid="radar:c")])
    th = threads.build_threads([], as_of=AS_OF, radar=r)
    assert list(th) == ["nubank--service_outage"]
    t = th["nubank--service_outage"]
    assert t["radar_backed"] is True and t["alert_peak"] == 0.0
    card = threads.build_card(t)
    assert card["is_alert"] is False and card["radar_backed"] is True
    _no_youtube_data([t, card])


def test_mixed_outage_thread_can_alert_and_is_not_radar_backed():
    narr = [{"id": "n1", "entity": "nubank", "narrative": "App do Nubank fora do ar; clientes sem acesso.",
             "threat_score": 0.8, "run_date": "2026-11-05", "lenses": ["news"], "citations": [{"url": "https://ex/1"}]},
            {"id": "n2", "entity": "nubank", "narrative": "Nubank: falha no aplicativo persiste.",
             "threat_score": 0.6, "run_date": "2026-11-06", "lenses": ["news"], "citations": [{"url": "https://ex/2"}]}]
    r = _radar([_ev("nubank", "outage", date="2026-11-06", conf="corroborado", n_ch=7)])
    t = threads.build_threads(narr, as_of=AS_OF, radar=r)["nubank--service_outage"]
    assert t["radar_backed"] is False and t["n_developments"] == 2
    assert any(c.get("id", "").startswith("radar:") for d in t["developments"] for c in d["citations"])
    assert threads.build_card(t)["is_alert"] is True


def test_appstore_alert_development_is_alertable():
    alert = {"id": "radar:as1", "entity": "inter", "product": "inter", "product_label": "Inter",
             "source": "appstore", "type": "outage", "type_label": "instabilidade",
             "date": "2026-11-07", "confidence": "baseline", "url": "https://apps.apple.com/x",
             "samples": ["texto de review"]}
    r = _radar([_ev("inter", "outage", date="2026-11-06", conf="corroborado", n_ch=4)], alerts=[alert])
    t = threads.build_threads([], as_of=AS_OF, radar=r)["inter--service_outage"]
    assert t["alert_peak"] == threads.RADAR_APPSTORE_SCORE
    assert threads.build_card(t)["is_alert"] is True
    assert "texto de review" not in json.dumps(t, ensure_ascii=False)


def test_threads_without_radar_unchanged():
    narr = [{"id": "x", "entity": "acme", "narrative": "Acme anuncia aquisição da rival.", "threat_score": 0.6,
             "run_date": d, "lenses": ["news"], "citations": []} for d in ("2026-11-05", "2026-11-06")]
    assert threads.build_threads(narr, as_of=AS_OF) == threads.build_threads(narr, as_of=AS_OF, radar=_radar([]))


# --- #373 Four Corners ----------------------------------------------------------------------
def _fc_narr(entity, n):
    return [{"id": f"n-{entity}-{i}", "entity": entity,
             "narrative": f"Narrative claim {i} about {entity} competitive behavior in the Brazilian market.",
             "axis": "behavioral", "lenses": ["fatos", "ofertas", "news", "market"],
             "threat_score": 0.5 + i * 0.05, "as_of": AS_OF} for i in range(n)]


def _draft_cites_radar(label, industries, swot_bullets, evidence):
    radar_i = [i for i, e in enumerate(evidence) if e["id"].startswith("radar:")]
    assert all("Pix parcelado" in evidence[i]["claim"] for i in radar_i)  # prompt sees the change
    return [{"corner": c, "text": f"{label} {c}.", "confidence": 0.8, "evidence": radar_i}
            for c in ("current_strategy", "capabilities", "drivers", "response_profile")]


def test_four_corners_radar_only_supports_strategy_and_capabilities(monkeypatch):
    monkeypatch.setenv("ONCA_FRAMEWORK_SIGNAL_GATE", "0")  # holds even with the gate off
    out = four_corners.analyze_corners({}, _fc_narr("itau", 3), {}, run_date=AS_OF,
                                       draft_fn=_draft_cites_radar, radar_events=[_ev("itau")])
    assert sorted(p["dimension"] for p in out) == ["capabilities", "current_strategy"]
    for p in out:
        assert p["evidence"] == ["radar:itau-launch-2026-11-05"]
        assert p["radar_evidence"] == [{"id": "radar:itau-launch-2026-11-05",
                                        "label": "Itau — lançamento (YouTube oficial, 05/11/2026)"}]
        _no_youtube_data(p)


def test_four_corners_radar_never_makes_an_entity_eligible():
    out = four_corners.analyze_corners({}, _fc_narr("itau", 2), {}, run_date=AS_OF,
                                       draft_fn=_draft_cites_radar,
                                       radar_events=[_ev("itau", date=d) for d in ("2026-11-01", "2026-11-02", "2026-11-03")])
    assert out == []


def test_four_corners_drops_single_creator_and_non_product_events():
    seen = {}

    def _spy(label, industries, swot_bullets, evidence):
        seen["ids"] = [e["id"] for e in evidence]
        return []
    four_corners.analyze_corners({}, _fc_narr("itau", 3), {}, run_date=AS_OF, draft_fn=_spy,
                                 radar_events=[_ev("itau", conf="criador único"), _ev("itau", "outage", eid="radar:o")])
    assert not [i for i in seen["ids"] if i.startswith("radar:")]


def test_radar_backed_cards_do_not_count_toward_framework_eligibility():
    narr = _fc_narr("itau", 2) + [{**_fc_narr("itau", 1)[0], "id": "comparative-cadence-itau",
                                   "radar_backed": True}]
    assert four_corners.analyze_corners({}, narr, {}, run_date=AS_OF, draft_fn=_draft_cites_radar) == []
    assert ansoff.classify_moves({}, narr, {}, run_date=AS_OF, draft_fn=lambda *a, **k: []) == []


# --- #374 Comparative product cadence -------------------------------------------------------
BANKS = ["itau", "nubank", "btg", "picpay", "santander", "bradesco", "inter"]
IMAP = {e: ["banking"] for e in BANKS}


def _cadence_radar(as_of, itau_n):
    evs = [_ev("itau", "feature", date=as_of, eid=f"radar:it{as_of}{i}") for i in range(itau_n)]
    evs += [_ev("nubank", "launch", date=as_of, eid=f"radar:nu{as_of}"),
            _ev("btg", "feature", date=as_of, eid=f"radar:bt{as_of}", conf="criador único")]
    return _radar(evs, products=BANKS)


def test_cadence_outlier_needs_stability_across_snapshots():
    now, before = _cadence_radar(AS_OF, 7), _cadence_radar("2026-11-01", 5)
    got = comparative.nominate_cadence(now, before, IMAP, as_of=AS_OF, prior_as_of="2026-11-01")
    assert [c["entity"] for c in got] == ["itau"]
    assert got[0]["count"] == 7 and got[0]["prior_count"] == 5
    # a one-week burst (not an outlier a week earlier) never emits
    assert comparative.nominate_cadence(now, _cadence_radar("2026-11-01", 1), IMAP,
                                        as_of=AS_OF, prior_as_of="2026-11-01") == []
    assert comparative.nominate_cadence(now, {}, IMAP, as_of=AS_OF, prior_as_of="2026-11-01") == []


def test_cadence_small_cohort_and_no_weakness():
    small = {e: ["banking"] for e in ("itau", "nubank")}
    assert comparative.cadence_outliers(_cadence_radar(AS_OF, 7), small, as_of=AS_OF) == {}
    out = comparative.cadence_outliers(_cadence_radar(AS_OF, 7), IMAP, as_of=AS_OF)
    assert set(out) == {"itau"}  # zero-event banks never become a Weakness


def test_cadence_card_is_label_only_radar_backed_strength_on_its_own_swot_claim():
    c = comparative.nominate_cadence(_cadence_radar(AS_OF, 7), _cadence_radar("2026-11-01", 5), IMAP,
                                     as_of=AS_OF, prior_as_of="2026-11-01")[0]
    card = comparative.build_cadence_narrative(c)
    assert card["id"] == "comparative-cadence-itau" and card["radar_backed"] is True
    assert card["swot_hint"]["dimension"] == "S" and card["swot_hint"]["basis"] == "product_cadence"
    _no_youtube_data(card)
    feeds = list(swot_store.feeds(card))
    assert feeds == [("itau", "S", "cadence:banking", feeds[0][3])]
    assert "ritmo acima dos pares" in swot_store._bullet_text("cadence:banking", "S", "Bancos")


def test_cadence_cards_do_not_touch_threat_peer_cooldown():
    cadence_card = {"axis": "comparative", "entity": "itau", "run_date": AS_OF, "peer_tier": 3,
                    "direction": "outperform", "signal": "product_cadence"}
    assert comparative.prior_outliers([cadence_card]) == {}
    assert "itau" in comparative.prior_outliers([cadence_card], cadence=True)


# --- #372 Predictive outcome labels ---------------------------------------------------------
def _ind(entity="itau"):
    return {"entity": entity, "label": entity.title(), "signal": "launch_precursor", "horizon_days": 30}


def test_outcome_rows_open_once_and_resolve_hit_on_later_radar_launch():
    log = predictive.update_outcomes({}, [_ind()], as_of="2026-11-01", radar={}, narratives=[], shadow=True)
    log = predictive.update_outcomes(log, [_ind()], as_of="2026-11-02", radar={}, narratives=[], shadow=True)
    assert [r["status"] for r in log["rows"]] == ["open"]
    r = _radar([_ev("itau", "launch", date="2026-11-05")])
    log = predictive.update_outcomes(log, [], as_of="2026-11-06", radar=r, narratives=[], shadow=True)
    row = log["rows"][0]
    assert row["status"] == "hit" and row["evidence"][0]["basis"] == "product_radar"
    assert log["summary"]["launch_precursor"] == {"open": 0, "hit": 1, "miss": 0, "precision": 1.0}
    _no_youtube_data(log)


def test_outcome_ignores_same_day_single_creator_and_resolves_miss_after_horizon():
    log = predictive.update_outcomes({}, [_ind()], as_of="2026-10-01", radar={}, narratives=[], shadow=True)
    r = _radar([_ev("itau", "launch", date="2026-10-01"), _ev("itau", "launch", date="2026-10-10", conf="criador único")])
    log = predictive.update_outcomes(log, [], as_of="2026-10-20", radar=r, narratives=[], shadow=True)
    assert log["rows"][0]["status"] == "open"
    log = predictive.update_outcomes(log, [], as_of="2026-11-01", radar={}, narratives=[], shadow=True)
    assert log["rows"][0]["status"] == "miss"
    assert log["summary"]["launch_precursor"]["precision"] == 0.0


def test_outcome_hit_from_launch_narrative():
    log = predictive.update_outcomes({}, [_ind()], as_of="2026-11-01", radar={}, narratives=[], shadow=True)
    narr = [{"id": "n1", "entity": "itau", "narrative": "Itaú anuncia lançamento de novo produto de crédito.",
             "run_date": "2026-11-03", "threat_score": 0.5}]
    log = predictive.update_outcomes(log, [], as_of="2026-11-04", radar={}, narratives=narr, shadow=True)
    assert log["rows"][0]["status"] == "hit" and log["rows"][0]["evidence"][0]["basis"] == "narrative"


def test_leading_indicators_never_read_the_radar(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("predictive precursors must not read the radar")
    monkeypatch.setattr(radar_evidence, "load", _boom)
    monkeypatch.setattr(radar_evidence, "confirmed_events", _boom)
    predictive.leading_indicators({"entities": [{"entity": "itau", "score_z": 0.1}]}, [])


def test_time_gated_handler_logs_shadow_outcomes(monkeypatch):
    puts = {}

    class _S3:
        def get_object(self, Bucket, Key):
            raise KeyError(Key)

        def put_object(self, Bucket, Key, Body, **kw):
            puts[Key] = json.loads(Body)

    monkeypatch.setenv("ONCA_DIGESTS_BUCKET", "b")
    monkeypatch.setattr(predictive.boto3, "client", lambda *_: _S3())
    monkeypatch.setattr(predictive.feature_store, "load_history", lambda *a, **k: [])
    monkeypatch.setattr(predictive, "leading_indicators", lambda f, n: [_ind()])
    monkeypatch.setattr(radar_evidence, "load", lambda *a, **k: {})
    body = json.loads(predictive.lambda_handler({}, None)["body"])
    assert body["status"] == "time_gated"
    assert body["outcomes"]["launch_precursor"]["open"] == 1
    assert puts[predictive.OUTCOMES_KEY]["rows"][0]["shadow"] is True
    assert not [k for k in puts if k.startswith("narratives/")]  # nothing published


def test_threat_history_mean_skips_radar_backed_cards():
    from src.dashboard import feed_builder
    hist = [{"id": "a", "entity": "itau", "run_date": "2026-10-20", "threat_score": 0.4, "threat_factors": {"x": 1}},
            {"id": "b", "entity": "itau", "run_date": "2026-10-21", "threat_score": 0.9, "threat_factors": {"x": 1},
             "radar_backed": True}]
    out = feed_builder._attach_threat_history({"itau": {}}, hist, {})
    h = out["itau"][feed_builder.THREAT_HISTORY_KEY]
    assert h["n"] == 1 and h["mean"] == 40.0


# --- #369 Ansoff product_dev ------------------------------------------------------------------
def _draft_vectors_cite_radar(label, industries, swot_bullets, evidence, **_):
    radar_i = [i for i, e in enumerate(evidence) if e["id"].startswith("radar:")]
    assert all("Pix parcelado" in evidence[i]["claim"] for i in radar_i)  # prompt sees the change
    return [{"vector": v, "text": f"{label} {v}.", "confidence": 0.8, "evidence": radar_i}
            for v in ansoff.DIMENSIONS]


def test_ansoff_radar_only_supports_product_dev(monkeypatch):
    monkeypatch.setenv("ONCA_FRAMEWORK_SIGNAL_GATE", "0")  # holds even with the gate off
    out = ansoff.classify_moves({}, _fc_narr("itau", 3), {}, run_date=AS_OF,
                                draft_fn=_draft_vectors_cite_radar, radar_events=[_ev("itau")])
    assert [p["dimension"] for p in out] == ["product_dev"]
    p = out[0]
    assert p["evidence"] == ["radar:itau-launch-2026-11-05"]
    assert p["narrative_id"] == "radar:itau-launch-2026-11-05"
    assert p["radar_evidence"] == [{"id": "radar:itau-launch-2026-11-05",
                                    "label": "Itau — lançamento (YouTube oficial, 05/11/2026)"}]
    _no_youtube_data(p)


def test_ansoff_radar_passes_the_signal_gate_for_product_dev_only():
    out = ansoff.classify_moves({}, _fc_narr("itau", 3), {}, run_date=AS_OF,
                                draft_fn=_draft_vectors_cite_radar, radar_events=[_ev("itau")])
    assert [p["dimension"] for p in out] == ["product_dev"]


def test_ansoff_mixed_evidence_prefers_a_narrative_as_narrative_id():
    def _draft(label, industries, swot_bullets, evidence, **_):
        return [{"vector": "product_dev", "text": "x.", "confidence": 0.8,
                 "evidence": list(range(len(evidence)))[::-1]}]
    out = ansoff.classify_moves({}, _fc_narr("itau", 3), {}, run_date=AS_OF, draft_fn=_draft,
                                radar_events=[_ev("itau")])
    assert out[0]["evidence"][0].startswith("radar:")
    assert not out[0]["narrative_id"].startswith("radar:")


def test_ansoff_radar_never_makes_an_entity_eligible_and_drops_unconfirmed():
    many = [_ev("itau", date=d) for d in ("2026-11-01", "2026-11-02", "2026-11-03")]
    assert ansoff.classify_moves({}, _fc_narr("itau", 2), {}, run_date=AS_OF,
                                 draft_fn=_draft_vectors_cite_radar, radar_events=many) == []
    seen = {}

    def _spy(label, industries, swot_bullets, evidence, **_):
        seen["ids"] = [e["id"] for e in evidence]
        return []
    ansoff.classify_moves({}, _fc_narr("itau", 3), {}, run_date=AS_OF, draft_fn=_spy,
                          radar_events=[_ev("itau", conf="criador único"),
                                        _ev("itau", "price", eid="radar:p"),
                                        _ev("itau", conf="corroborado", n_ch=3, eid="radar:c")])
    assert [i for i in seen["ids"] if i.startswith("radar:")] == ["radar:c"]


def test_ansoff_without_radar_unchanged():
    def _draft(label, industries, swot_bullets, evidence, **_):
        return [{"vector": "product_dev", "text": "x.", "confidence": 0.8, "evidence": [0]}]
    out = ansoff.classify_moves({}, _fc_narr("itau", 3), {}, run_date=AS_OF, draft_fn=_draft)
    assert out[0]["radar_evidence"] == [] and out[0]["narrative_id"] == out[0]["evidence"][0]


# --- #369/#373 refresh of already-drafted entities ------------------------------------------------
def _store(created_by_ent, marks=None):
    doc = {"proposals": [{"id": f"p-{e}", "entity": e, "created": d, "status": "approved"}
                         for e, d in created_by_ent.items()]}
    if marks:
        doc[radar_evidence.REFRESH_KEY] = marks
    return doc


def test_refresh_needs_old_proposal_and_newer_confirmed_launch():
    store = _store({"itau": "2026-08-23", "nubank": "2026-10-20", "btg": "2026-08-23",
                    "c6": "2026-08-24"})
    evs = [_ev("itau", date="2026-11-05"), _ev("nubank", date="2026-11-06"),
           _ev("btg", conf="criador único"), _ev("neon"), _ev("c6", "outage")]
    # nubank too recent; btg single-creator; neon never drafted (normal path); c6 not a launch
    assert radar_evidence.refresh_entities(store, evs, as_of=AS_OF, min_age_days=60, max_n=2) == ["itau"]


def test_refresh_orders_by_newest_launch_caps_and_respects_prior_attempts():
    store = _store({"itau": "2026-08-23", "btg": "2026-08-23", "c6": "2026-08-24"},
                   marks={"c6": "2026-10-30"})
    evs = [_ev("itau", date="2026-11-03"), _ev("btg", date="2026-11-06"), _ev("c6", date="2026-11-07")]
    assert radar_evidence.refresh_entities(store, evs, as_of=AS_OF, min_age_days=60, max_n=2) == ["btg", "itau"]
    assert radar_evidence.refresh_entities(store, evs, as_of=AS_OF, min_age_days=60, max_n=1) == ["btg"]
    marks = radar_evidence.mark_refreshed(store, ["btg"], AS_OF)
    assert marks == {"c6": "2026-10-30", "btg": AS_OF}
    store[radar_evidence.REFRESH_KEY] = marks
    assert radar_evidence.refresh_entities(store, evs, as_of=AS_OF, min_age_days=60, max_n=2) == ["itau"]


def test_refresh_off_by_default(monkeypatch):
    monkeypatch.delenv("ONCA_RADAR_REFRESH", raising=False)
    assert radar_evidence.refresh_enabled() is False
    monkeypatch.setenv("ONCA_RADAR_REFRESH", "1")
    assert radar_evidence.refresh_enabled() is True


def _handler_env(monkeypatch, mod, key, prior):
    puts = {}

    class _Body:
        def __init__(self, b):
            self.b = b

        def read(self):
            return self.b

    class _S3:
        def get_object(self, Bucket, Key):
            if Key == key:
                return {"Body": _Body(json.dumps(prior).encode())}
            raise KeyError(Key)

        def put_object(self, Bucket, Key, Body, **kw):
            puts[Key] = json.loads(Body)

    drafted = []

    def _draft(label, industries, swot_bullets, evidence, **_):
        drafted.append(label)
        return []
    monkeypatch.setenv("ONCA_DIGESTS_BUCKET", "b")
    monkeypatch.setattr(mod.boto3, "client", lambda *_: _S3())
    monkeypatch.setattr(mod.feature_store, "load_history", lambda *a, **k: _fc_narr("itau", 3))
    monkeypatch.setattr(mod.swot_reconcile, "load_beliefs", lambda *a, **k: {})
    monkeypatch.setattr(mod.framework_common, "financial_context_map", lambda *a, **k: {})
    monkeypatch.setattr(mod, "llm_draft", _draft)
    monkeypatch.setattr(mod, "run_date_today", lambda: AS_OF)
    from src.synth import entity_registry
    monkeypatch.setattr(entity_registry, "entity_industry_map", lambda *a, **k: {})  # no live table
    monkeypatch.setattr(radar_evidence, "load", lambda *a, **k: _radar([_ev("itau")]))
    return puts, drafted


def test_handlers_redraft_refreshed_entity_only_when_enabled(monkeypatch):
    for mod, key in ((ansoff, ansoff.ANSOFF_PROPOSALS_KEY),
                     (four_corners, four_corners.FOUR_CORNERS_PROPOSALS_KEY)):
        prior = _store({"itau": "2026-08-23"})
        monkeypatch.delenv("ONCA_RADAR_REFRESH", raising=False)
        puts, drafted = _handler_env(monkeypatch, mod, key, prior)
        body = json.loads(mod.lambda_handler({}, None)["body"])
        assert drafted == [] and body["radar_refreshed"] == []
        assert radar_evidence.REFRESH_KEY not in puts[key]

        monkeypatch.setenv("ONCA_RADAR_REFRESH", "1")
        puts, drafted = _handler_env(monkeypatch, mod, key, prior)
        body = json.loads(mod.lambda_handler({}, None)["body"])
        assert len(drafted) == 1 and body["radar_refreshed"] == ["itau"], mod.__name__
        assert puts[key][radar_evidence.REFRESH_KEY] == {"itau": AS_OF}
        assert puts[key]["proposals"] == prior["proposals"]  # nothing new, history kept
