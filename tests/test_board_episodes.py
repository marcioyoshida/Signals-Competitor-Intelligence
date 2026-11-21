"""Historic board episodes with answer keys (#339)."""
from __future__ import annotations

import json

import pytest

from src.board import episodes as ep
from src.board import pack as packmod


def test_visible_quarter_uses_publication_lag():
    assert ep.visible_quarter("2015-09-09") == 201503        # 2015-06 is public from 2015-09-28
    assert ep.visible_quarter("2015-09-28") == 201506
    assert ep.visible_quarter("2012-06-03") == 201112        # Cruzeiro do Sul: 2012-03 not yet public
    assert ep.visible_quarter("2012-10-18") == 201206        # BVA: 2012-06 public before the intervention
    assert ep.next_quarters(201209, 3) == [201212, 201303, 201306]


@pytest.mark.parametrize("subject,cat", [
    ("Distribuição de Dividendos/Juros sobre Capital Próprio", "capital_return"),
    ("Remuneração dos Acionistas 2016 (Payout)", "capital_return"),
    ("Aprovação do Programa de Recompra e Outros Assuntos", "capital_return"),
    ("Repactuação das debêntures da Escritura da 4ª emissão", "funding"),
    ("Aprovar a criação de Comitê Independente", "independent_review"),
    ("Alterações na Administração", "governance"),
    ("Nomeação de Membro do CA", "governance"),
    ("Aprovação das Demonstrações Contábeis do 3º trimestre de 2015", "accounts"),
    ("Aumento de Capital Social", "capital_raise"),
    ("Assunto qualquer", "other"),
])
def test_decision_category(subject, cat):
    assert ep.category(subject) == cat


def _stores():
    ifd = [{"as_of": q, "roe_pct": roe, "basileia_headroom_pp": hr, "ativo_bi": 10.0, "carteira_share_pct": 0.3,
            "credito_captacoes_pct": 80.0} for q, roe, hr in ((201203, 2.0, 3.0), (201206, -34.3, -1.29), (201209, -90.0, None))]
    peer = [{"as_of": q, "roe_pct": 9.0, "basileia_headroom_pp": 5.0, "ativo_bi": 11.0, "carteira_share_pct": 0.4,
             "credito_captacoes_pct": 85.0, "conglomerate": True} for q in (201203, 201206, 201209)]
    ipe = [
        {"id": "a", "entity_id": "peer", "doc_class": "comunicado", "date": "2012-10-01", "subject": "Aviso antes"},
        {"id": "b", "entity_id": "peer", "doc_class": "comunicado", "date": "2012-10-25", "subject": "Aviso depois"},
        {"id": "c", "entity_id": "ep-x", "doc_class": "ata_rca", "answer_key": True, "date": "2012-11-05",
         "subject": "Aumento de Capital", "url": "u"},
        {"id": "d", "entity_id": "ep-x", "doc_class": "ata_rca", "answer_key": True, "date": "2013-06-01",
         "subject": "Eleição de Diretor(es)", "url": "u"},        # after the 120-day window
    ]

    def pts(start, closes):
        import datetime as dt
        d0 = dt.date.fromisoformat(start)
        return [[(d0 + dt.timedelta(days=i)).isoformat(), c, c, c, c, 1000.0, 10, None] for i, c in enumerate(closes)]
    b3 = {"entities": {"ep-x": {"XXXX4": pts("2012-08-01", [10.0] * 60 + [6.0] * 40)},
                       "peer": {"PEER4": pts("2012-08-01", [10.0] * 100)},
                       "other": {"OTHR4": pts("2012-08-01", [10.0] * 100)}}}
    return {"prudential": {"entities": {"peer": {"ifdata": peer}}},
            "episode_points": {"ep-x": {"ifdata": ifd}}, "labels": {"peer": "Banco Par"},
            "ipe_rows": ipe, "rating_events": [], "selic": [["2012-08-29", 7.5], ["2012-10-10", 7.25]],
            "complaints": {"entities": {}}, "b3": b3,
            "ref_manifest": {"editions": [{"edition": "2012-09", "date": "2012-09-30", "url": "ref-url",
                                           "sections": [{"key": "BCB-REF/2012-09/09-2-6.txt", "label": "2.6 Teste de estresse", "stress": True}]},
                                          {"edition": "2013-03", "date": "2013-03-31", "url": "later", "sections": []}]},
            "ref_text": {"BCB-REF/2012-09/09-2-6.txt": "Relatório…\nSeção: x\n\nO sistema bancário demonstra solidez. Segunda frase."}}


EP = {"id": "fail-x-2012", "kind": "failure", "rubric": "failure", "event_date": "2012-10-19", "as_of": "2012-10-18",
      "focal": "ep-x", "peers": ["peer"], "episode_entities": {"ep-x": {"label": "Banco X", "cnpj8": "12345678"}},
      "outcome": [{"date": "2012-10-19", "regime": "Intervenção"}], "task": "?"}


def test_feed_is_frozen_at_as_of():
    feed = ep.build_feed(EP, _stores())
    assert ep.lookahead(feed) == []
    ids = {x["id"] for x in feed["feed"]}
    assert "ipe-a" in ids and "ipe-b" not in ids                    # filing after as_of excluded
    focal = next(e for e in feed["entities"] if e["entity"] == "ep-x")
    assert focal["fundamentals"]["base_date"] == 201206              # 201209 not public yet
    assert focal["label"] == "Banco X"
    assert feed["macro"]["selic"]["current"] == 7.25
    assert feed["macro"]["selic"]["last_decision"] == {"date": "2012-10-10", "previous": 7.5, "value": 7.25}
    ref = [e for e in feed["sector_events"] if e["id"].startswith("ref-")]
    assert ref and ref[0]["date"] == "2012-09-30" and "solidez" in ref[0]["title"]
    price = next(x for x in feed["feed"] if x["kind"] == "market:b3" and x["entities"] == ["ep-x"])
    assert price["date"] == "2012-10-17"                             # window ends the day before


def test_lookahead_detects_late_items():
    feed = ep.build_feed(EP, _stores())
    feed["feed"].append({"id": "late", "date": "2012-10-20"})
    feed["entities"][0]["fundamentals"]["base_date"] = 201209
    bad = ep.lookahead(feed)
    assert "late" in bad and "ep-x:fundamentals" in bad


def test_episode_doc_refuses_lookahead(monkeypatch):
    real = ep.build_feed

    def leaky(e, s):
        f = real(e, s)
        f["sector_events"].append({"id": "future", "date": "2013-01-01"})
        return f
    monkeypatch.setattr(ep, "build_feed", leaky)
    with pytest.raises(ValueError, match="look-ahead"):
        ep.episode_doc(EP, _stores(), {"failure": []}, "t")


def test_answer_key_window_and_signal():
    doc = ep.episode_doc(EP, _stores(), {"failure": [{"id": "losses"}]}, "t")
    key = doc["answer_key"]
    assert [d["subject"] for d in key["board_decisions"]] == ["Aumento de Capital"]   # 2013-06 outside window
    assert key["decision_categories"] == {"capital_raise": 1}
    assert key["outcome"][0]["regime"] == "Intervenção"
    assert [p["quarter"] for p in key["prudential_after"]][:2] == [201206, 201209]    # answer key may look ahead
    sig = key["pre_event_signal"]
    assert sig["visible"] and "prudential" in sig["basis"]
    assert doc["usable"] and doc["rubric"]["curated"]


def test_market_only_signal():
    stores = _stores()
    stores["episode_points"]["ep-x"]["ifdata"] = [{"as_of": 201206, "roe_pct": 4.0, "basileia_headroom_pp": 4.0, "ativo_bi": 10.0}]
    feed = ep.build_feed(EP, stores)
    sig = ep.pre_event_signal(feed, "ep-x")
    assert sig["visible"] and sig["basis"] == ["market"]                 # -40% vs a flat sector


def test_unusable_without_focal_data():
    stores = _stores()
    stores["episode_points"] = {}
    doc = ep.episode_doc(EP, stores, {}, "t")
    assert not doc["usable"] and "prudenciais" in doc["usable_reason"]


def test_feed_goes_through_the_real_pack_builder():
    doc = ep.episode_doc(EP, _stores(), {}, "t")
    p = packmod.seal({**packmod.build_pack(doc["feed"], doc["focal"]), "scenario": doc["scenario"]})
    kinds = {f["kind"] for f in p["facts"]}
    assert {"fundamentals", "macro", "sector_event", "proximity"} <= kinds
    assert all((f["as_of"] or "") <= "2012-10-18" for f in p["facts"])


def test_complaint_rank_within_band_and_method():
    s = {"entities": {"a": [{"period": "2015-S1", "category": "big", "method": "m", "indice": 50.0}],
                      "b": [{"period": "2015-S1", "category": "big", "method": "m", "indice": 30.0}],
                      "c": [{"period": "2015-S1", "category": "small", "method": "m", "indice": 99.0}]}}
    assert ep.complaint_rank(s, s["entities"]["b"][0]) == 2
    assert ep.complaint_rank(s, s["entities"]["a"][0]) == 1


def test_config_is_consistent():
    cfg = ep.load_config()
    ids = [e["id"] for e in cfg["episodes"]]
    assert len(ids) == len(set(ids)) and 5 <= len(ids) <= 8
    for e in cfg["episodes"]:
        assert e["as_of"] <= e["event_date"]
        assert e["focal"] not in e["peers"]
        assert e["rubric"] in cfg["rubrics"]
        if e["kind"] == "failure":
            assert e.get("outcome") and e["focal"] in e["episode_entities"]
        else:
            assert e["trigger"]["url"].startswith("https://")


def test_written_episodes_have_no_lookahead():
    from pathlib import Path
    files = sorted(Path(ep.OUT_DIR).glob("*-*.json"))
    files = [f for f in files if f.name != "episodes.json"]
    assert files, "run scripts/build_board_episodes.py"
    for f in files:
        doc = json.loads(f.read_text(encoding="utf-8"))
        assert ep.lookahead(doc["feed"]) == [], f.name
        assert doc["feed"]["as_of"] == doc["as_of"]
