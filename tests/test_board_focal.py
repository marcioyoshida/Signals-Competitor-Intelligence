"""#386 focal vs subject, proximity-chosen peers, proximity-weighted competitor signals."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.board import artifact as A
from src.board import contracts as c
from src.board import pack as P
from src.board import runner as R


def bank(eid, assets, basel, npl, cc, inds=("banking",)):
    return {"entity": eid, "label": eid.title(), "industries": list(inds),
            "fundamentals": {"ativo_bi": assets, "credito_captacoes_pct": cc, "roa_pct": 1.0, "base_date": 202606},
            "soundness": {"indice_basileia": basel, "razao_alavancagem": 7.0, "base_date": 202606},
            "inadimplencia": {"npl_total": npl, "base_date": 202606}}


FEED = {
    "as_of": "2026-11-15", "generated_at": "2026-11-15T00:00:00+00:00",
    "entities": [bank("aurora", 200, 12.0, 5.0, 60), bank("twin", 220, 12.5, 5.2, 62),
                 bank("near", 300, 13.5, 4.0, 70), bank("giant", 20000, 16.0, 2.0, 95),
                 bank("tiny", 2, 20.0, 9.0, 30), bank("ins", 200, 12.0, 5.0, 60, inds=("insurance",))],
    "feed": [{"id": "n-giant", "kind": "competitor:news", "entities": ["giant"], "date": "2026-11-12",
              "narrative": "Giant corta juros do cartão rotativo."},
             {"id": "n-aurora", "kind": "competitor:news", "entities": ["aurora"], "date": "2026-11-11",
              "narrative": "Aurora revisa política de crédito."}],
}


def test_peers_are_the_focal_institutions_nearest_by_proximity():
    p = P.build_pack(FEED, "aurora")
    assert p["focal"] == "aurora" == p["subject"] and p["peers"][:2] == ["twin", "near"]
    assert "ins" not in p["peers"]                                   # no shared industry
    scores = [x["score"] for x in p["proximity"]]
    assert scores == sorted(scores, reverse=True)
    ids = {f["id"] for f in p["facts"]}
    assert {"f-proximity-twin", "f-proximity-near", "f-proximity-giant"} <= ids
    assert "f-proximity-aurora" not in ids                           # never against itself


def test_a_competitor_subject_is_always_in_the_pack_with_its_narratives():
    p = P.build_pack(FEED, "aurora", subject="giant")
    assert p["subject"] == "giant" and "giant" in p["peers"]
    nar = {f["id"]: f for f in p["facts"] if f["kind"] == "narrative"}
    assert nar["f-narrative-n-giant"]["entity"] == "giant" and "f-narrative-n-aurora" in nar
    txt = P.context_text(P.seal(p))
    assert "instituição focal (o conselho que decide): aurora; assunto: giant" in txt
    assert "giant (0." in txt


def test_proximity_of_maps_every_competitor_fact_and_nothing_about_the_focal():
    p = P.build_pack(FEED, "aurora", subject="giant")
    m = P.proximity_of(p)
    assert m["f-km1-giant" if "f-km1-giant" in m else "f-soundness-giant"] == "f-proximity-giant"
    assert m["f-narrative-n-giant"] == "f-proximity-giant"
    assert not any(k.endswith("-aurora") or k == "f-narrative-n-aurora" for k in m)


def test_citing_a_competitor_fact_requires_its_proximity():
    pos = {"officer": "risk", "diagnosis": "x", "options": [],
           "citations": [{"pack_fact_id": "f-soundness-giant"}]}
    ids = {"f-soundness-giant", "f-proximity-giant"}
    prox = {"f-soundness-giant": "f-proximity-giant"}
    errs = c.validate_position(pos, officer="risk", pack_fact_ids=ids, proximity_of=prox)
    assert any("also cite f-proximity-giant" in e for e in errs)
    pos["citations"].append({"pack_fact_id": "f-proximity-giant"})
    assert c.validate_position(pos, officer="risk", pack_fact_ids=ids, proximity_of=prox) == []


def test_envelope_and_prompt_are_framed_for_the_focal_institution():
    env = c.make_envelope(run_id="r", phase="diagnosis", shared_context_id="a" * 64, task="t",
                          focal="aurora", subject="giant")
    up = c.user_prompt("risk", env)
    assert "conselho da instituição focal aurora" in up and "Assunto da pergunta: giant" in up
    assert "f-proximity" in up
    assert "focal" not in c.make_envelope(run_id="r", phase="diagnosis", shared_context_id="a" * 64, task="t")


def test_synthesis_options_carry_competitor_signals_by_code():
    syn = {"options": [{"id": "o1", "citations": ["f-soundness-giant", "f-soundness-twin", "f-km1-aurora"]},
                       {"id": "o2", "citations": ["f-km1-aurora"]}]}
    R._competitor_signals(syn, "aurora", {"f-soundness-giant": "giant", "f-soundness-twin": "twin",
                                          "f-km1-aurora": "aurora"}, {"giant": 0.31, "twin": 0.95})
    assert syn["options"][0]["competitor_signals"] == [{"entity": "twin", "proximity": 0.95},
                                                       {"entity": "giant", "proximity": 0.31}]
    assert syn["options"][1]["competitor_signals"] == []


def test_runner_threads_focal_and_enforces_proximity_for_every_officer():
    p = P.seal(P.build_pack(FEED, "aurora", subject="giant"))
    prompts = []

    def call(officer, phase, system, prompt):
        prompts.append(prompt)
        if officer == "secretary":
            return json.dumps({"summary": "s", "options": [
                {"id": f"o{i}", "description": "d", "customer_impact": "low",
                 "citations": ["f-soundness-giant", "f-proximity-giant"]} for i in (1, 2)]})
        off = prompt.rsplit('"officer": "', 1)[1].split('"')[0]
        # cites a competitor fact WITHOUT its proximity: must be re-prompted, then fixed
        cites = [{"pack_fact_id": "f-soundness-giant"}]
        if "rejeitada" in prompt:
            cites.append({"pack_fact_id": "f-proximity-giant"})
        return json.dumps({"officer": off, "diagnosis": "x", "options": [], "citations": cites})
    out = R.run_brief(run_id="r", pack_sha=p["sha256"], pack_ids=P.fact_ids(p), task="t", call=call,
                      focal="aurora", subject="giant", proximity_of=P.proximity_of(p),
                      fact_entity={f["id"]: f.get("entity") for f in p["facts"]},
                      proximity_score={x["peer"]: x["score"] for x in p["proximity"]})
    assert out["status"] == "complete"
    assert all("instituição focal aurora" in x for x in prompts if "Fase:" in x)
    assert sum("rejeitada" in x for x in prompts) == 11        # every officer turn re-prompted once
    assert out["synthesis"]["options"][0]["competitor_signals"][0]["entity"] == "giant"


def test_board_pack_names_whose_board_and_shows_the_competitor_table():
    p = P.build_pack(FEED, "aurora", subject="giant")
    run = {"run_id": "r", "status": "complete", "focal": "aurora", "subject": "giant", "task": "t",
           "proximity": p["proximity"],
           "synthesis": {"options": [{"id": "o1", "description": "d", "customer_impact": "low",
                                      "competitor_signals": [{"entity": "giant", "proximity": 0.31}]}]}}
    md = A.render_markdown(run)
    assert "# Briefing de conselho — aurora" in md and "**Assunto:** giant" in md
    assert "## Concorrentes por proximidade a aurora" in md and "| Twin | 0." in md
    assert "| o1 | d | baixo | giant (0.31) |" in md


def test_other_competitors_moves_reach_the_pack_tied_to_their_proximity():
    feed = dict(FEED, feed=FEED["feed"] + [
        {"id": f"n-near-{i}", "kind": "competitor:news", "entities": ["near"], "date": f"2026-11-0{i}",
         "narrative": f"Near move {i}."} for i in range(1, 5)])
    p = P.build_pack(feed, "aurora", subject="twin")
    nar = {f["id"]: f["entity"] for f in p["facts"] if f["kind"] == "narrative"}
    assert nar["f-narrative-n-giant"] == "giant"                 # a non-subject peer's move is in
    assert sum(1 for e in nar.values() if e == "near") == P.PEER_NARRATIVES   # capped per peer
    assert P.proximity_of(p)["f-narrative-n-giant"] == "f-proximity-giant"
