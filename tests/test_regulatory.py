import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import regulatory


def _card(text, *, date="2026-08-20", threat=0.4, axis=None, instrument=None,
          signature=None):
    n = {
        "entity": None,
        "narrative": text,
        "threat_score": threat,
        "run_date": date,
        "lenses": ["regulatory"],
        "citations": [{"url": "https://bcb/exibenormativo?numero=1"}],
    }
    if axis:
        n["axis"] = axis
        n["mode"] = "derived"
        n["is_inference"] = True
    if instrument:
        n["instrument"] = instrument
    if signature is not None:
        n["signature"] = signature
    return n


def test_instruments_in_extracts_and_excludes_comunicados():
    refs = regulatory.instruments_in(_card(
        "A Instrução Normativa BCB:770 e a Resolução CMN 4.966 tratam do tema; "
        "ver também o Regulamento do Pix versão 2.10.0 e o Comunicado 45785."))
    assert "in-bcb-770" in refs
    assert any(k.startswith("res-cmn-") for k in refs)
    assert "regulamento-pix" in refs and "2.10.0" in refs["regulamento-pix"]
    assert not any(k.startswith("comunicado-") for k in refs)  # excluded by default
    # opt-in
    refs2 = regulatory.instruments_in(_card("Comunicado 45785 do BCB."),
                                      include_comunicados=True)
    assert "comunicado-45785" in refs2


def test_future_deadline_needs_cue_and_future_date():
    t = "A norma entra em vigor em 15/12/2026 para todas as instituições."
    assert regulatory._future_deadline(t, "2026-08-23", 365) == "2026-12-15"
    # a date without a cue is not a deadline
    assert regulatory._future_deadline("Reunião em 15/12/2026.", "2026-08-23", 365) is None
    # a past date is ignored even with a cue
    assert regulatory._future_deadline("prazo era 01/01/2026.", "2026-08-23", 365) is None
    # textual pt-BR date
    assert regulatory._future_deadline(
        "passa a valer a partir de 1 de outubro de 2026.", "2026-08-23", 365) == "2026-10-01"


def test_nominate_threads_instrument_with_deadline_and_domain():
    narrs = [_card("Instrução Normativa BCB 770 sobre APIs de crédito e portabilidade; "
                   "entra em vigor em 30/11/2026.", date="2026-08-22")]
    cands = regulatory.nominate(narrs, as_of="2026-08-23")
    assert len(cands) == 1
    c = cands[0]
    assert c["instrument"] == "in-bcb-770"
    assert c["deadline"] == "2026-11-30" and c["days_to_deadline"] == 99
    assert c["domain"] == "Crédito & portabilidade"
    assert regulatory.swot_hint(c) == {
        "dimension": "T", "sign": "-", "instrument": "in-bcb-770",
        "domain": "Crédito & portabilidade"}


def test_build_narrative_alerts_on_near_deadline():
    narrs = [_card("Circular BCB 4111 sobre PIX passa a valer a partir de 01/09/2026.",
                   date="2026-08-22")]
    c = regulatory.nominate(narrs, as_of="2026-08-23")[0]
    n = regulatory.build_narrative(c)
    assert n["axis"] == "regulatory" and n["subject_type"] == "instrument"
    assert n["is_inference"] and n["mode"] == "derived"
    assert n["entity"] is None
    assert n["is_alert"] is True           # deadline within ALERT_WITHIN days
    assert "Radar regulatório" in n["narrative"] and "Prazo: vence em" in n["narrative"]


def test_no_deadline_instrument_is_context_not_alert():
    c = regulatory.nominate([_card("Regulamento do Pix versão 2.10.0 divulgado.")],
                            as_of="2026-08-23")[0]
    n = regulatory.build_narrative(c)
    assert n["is_alert"] is False and n["deadline"] is None
    assert n["threat_score"] == 0.25


def test_emit_on_change_suppresses_unchanged_and_refires_on_new_deadline():
    base = _card("Instrução Normativa BCB 770; entra em vigor em 30/11/2026.",
                 date="2026-08-22")
    prior = _card("prev", date="2026-08-21", axis="regulatory", instrument="in-bcb-770",
                  signature="Instrução Normativa BCB 770|2026-11-30")
    # unchanged signature within cooldown -> suppressed
    assert [c for c in regulatory.nominate([base, prior], as_of="2026-08-23")
            if c["instrument"] == "in-bcb-770"] == []
    # a changed deadline -> new signature -> re-fires
    moved = _card("Instrução Normativa BCB 770; entra em vigor em 31/12/2026.",
                  date="2026-08-22")
    got = [c for c in regulatory.nominate([moved, prior], as_of="2026-08-23")
           if c["instrument"] == "in-bcb-770"]
    assert len(got) == 1 and got[0]["deadline"] == "2026-12-31"
