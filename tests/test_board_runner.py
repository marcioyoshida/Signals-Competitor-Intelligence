"""#326 phase runner: diagnosis → challenge → quantify → synthesize, offline with a fake model."""
import json
import threading
import time

from src.board import runner as R

SHA = "b" * 64
IDS = {"f-km1-alfa", "f-npl-alfa", "f-macro-selic"}


def pos(officer, text="ok", cite="f-km1-alfa", red=()):
    return json.dumps({"officer": officer, "diagnosis": text, "constraints": [],
                       "options": [{"id": "opt-1", "description": f"{officer} opção",
                                    "customer_impact": "medium", "capital_liquidity_effect": "",
                                    "red_lines": list(red)}],
                       "citations": [{"pack_fact_id": cite, "source": "BCB", "as_of": "2026-3"}],
                       "tool_call_summary": []})


SYNTH = json.dumps({
    "summary": "Dois caminhos.",
    "options": [
        {"id": "opt-1", "description": "Reforçar liquidez", "customer_impact": "low",
         "capital_liquidity_effect": "+LCR", "supported_by": ["risk", "cfo"], "citations": ["f-km1-alfa"]},
        {"id": "opt-2", "description": "Rever preços", "customer_impact": "high",
         "capital_liquidity_effect": "", "supported_by": ["cpo"], "citations": ["f-npl-alfa", "f-invented"]},
        {"id": "opt-3", "description": "Inventada", "customer_impact": "low", "citations": ["f-nope"]},
    ],
    "residual_regulatory_risk": "Prazo do BCB.",
    "monitoring_triggers": [{"text": "Selic sobe", "citations": ["f-macro-selic"]},
                            {"text": "boato", "citations": []}],
})


class Fake:
    def __init__(self, overrides=None, synth=SYNTH):
        self.calls, self.overrides, self.synth = [], overrides or {}, synth
        self.lock = threading.Lock()

    def __call__(self, officer, phase, system, prompt):
        with self.lock:
            self.calls.append((officer, phase))
        if (officer, phase) in self.overrides:
            v = self.overrides[(officer, phase)]
            return v() if callable(v) else v
        if officer == "secretary":
            return self.synth
        if phase == "challenge":
            return pos(officer, text=f"Discordo — {officer} vê risco de conduta.", red=["sem venda casada"])
        return pos(officer)


def run(fake, **kw):
    return R.run_brief(run_id="r1", pack_sha=SHA, pack_ids=IDS, task="Rebaixamento do Banco Alfa",
                       call=fake, **kw)


def test_full_run_calls_every_phase_in_the_right_shape():
    f = Fake()
    out = run(f)
    assert out["status"] == "complete" and out["gaps"] == []
    by_phase = {}
    for o, p in f.calls:
        by_phase.setdefault(p, set()).add(o)
    assert by_phase["diagnosis"] == {"cso", "risk", "cco", "cpo", "cro"}
    assert by_phase["challenge"] == {"cro", "cco", "risk"}
    assert by_phase["quantify"] == {"risk", "cpo", "cso"}
    assert by_phase["synthesize"] == {"secretary"}


def test_synthesis_keeps_only_cited_claims_and_known_officers():
    out = run(Fake())
    s = out["synthesis"]
    assert [o["id"] for o in s["options"]] == ["opt-1", "opt-2"]
    assert s["options"][0]["supported_by"] == ["risk"]            # "cfo" is not a board
    assert s["options"][1]["citations"] == ["f-npl-alfa"]          # invented id stripped
    assert [t["text"] for t in s["monitoring_triggers"]] == ["Selic sobe"]
    assert any("Inventada" in d for d in out["dropped"]) and any("boato" in d for d in out["dropped"])


def test_dissents_are_verbatim_challenge_text():
    out = run(Fake())
    d = {x["officer"]: x for x in out["dissents"]}
    assert set(d) == {"cro", "cco", "risk"}
    assert d["cco"]["text"] == "Discordo — cco vê risco de conduta."
    assert d["cco"]["red_lines"] == ["sem venda casada"] and d["risk"]["challenged"] == ["cso"]


def test_officer_timeout_is_a_visible_gap_and_the_run_continues():
    f = Fake({("cpo", "diagnosis"): lambda: time.sleep(1.5) or pos("cpo")})
    out = run(f, timebox_s={"diagnosis": 0.5})
    assert out["status"] == "partial" and out["synthesis"] is not None
    assert any(g["officer"] == "cpo" and "prazo" in g["gap"] for g in out["gaps"])
    assert ("cpo", "quantify") not in f.calls                      # no diagnosis → not quantified


def test_invalid_officer_is_a_gap_not_a_crash():
    f = Fake({("cso", "diagnosis"): "isto não é JSON"})
    out = run(f)
    assert out["status"] == "partial"
    assert any(g["officer"] == "cso" and g["gap"].startswith("invalid") for g in out["gaps"])
    assert f.calls.count(("cso", "diagnosis")) == 2               # one re-prompt
    assert ("risk", "challenge") not in f.calls                    # nothing of cso's to challenge


def test_spend_cap_stops_between_phases_with_partial_results():
    n = {"i": 0}

    def stop():
        n["i"] += 1
        return n["i"] > 2                                          # allow diagnosis + challenge
    f = Fake()
    out = run(f, stop=stop)
    assert out["status"] == "partial" and "quantify" not in out["phases"]
    assert out["dissents"] and out["synthesis"] is None
    assert any("limite de custo" in g["gap"] for g in out["gaps"])
    assert not any(p == "quantify" for _, p in f.calls)


def test_synthesis_with_fewer_than_two_cited_options_is_rejected():
    bad = json.dumps({"options": [{"id": "o", "description": "x", "customer_impact": "low",
                                   "citations": ["f-km1-alfa"]}]})
    out = run(Fake(synth=bad))
    assert out["status"] == "partial" and out["synthesis"] is None
    assert any(g["phase"] == "synthesize" and "mínimo 2" in g["gap"] for g in out["gaps"])


def test_nobody_answers_means_no_synthesis_call():
    f = Fake({(o, "diagnosis"): None for o in ("cso", "risk", "cco", "cpo", "cro")})
    out = run(f)
    assert out["status"] == "partial" and ("secretary", "synthesize") not in f.calls
