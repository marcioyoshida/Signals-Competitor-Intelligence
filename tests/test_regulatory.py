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


def test_industries_for_domain_is_recall_first_for_sector_wide():
    uni = ["banking", "fintech", "insurance", "wealth-management", "crypto", "investment-banking"]
    # catch-all / unknown -> the WHOLE universe (#70: no empty Radar Regulatório)
    assert set(regulatory.industries_for_domain("Setor financeiro", uni)) == set(uni)
    assert set(regulatory.industries_for_domain("Domínio Desconhecido", uni)) == set(uni)
    # a specific domain scopes precisely, intersected with the live universe
    assert regulatory.industries_for_domain("Câmbio & mercado aberto", uni) == ["banking", "investment-banking"]
    assert regulatory.industries_for_domain("Seguros & previdência", uni) == ["insurance"]  # #71
    # display cohort stays precise (never the whole universe)
    assert regulatory._industries_for("Setor financeiro") == ["banking", "fintech", "insurance"]


def test_insurance_domain_classifies_susep_and_seguro():
    assert regulatory._domain_of("Circular SUSEP sobre resseguro e previdência") == "Seguros & previdência"
    assert regulatory._domain_of("nova regra para seguradoras") == "Seguros & previdência"


def test_instrument_number_not_truncated_when_undotted():
    # _NUM must take the whole digit run: an undotted 5304 threads as res-cmn-5304,
    # not the truncated res-cmn-530 (news text omits the pt-BR thousands dot).
    refs = regulatory.instruments_in(_card("a Resolução CMN 5304 e a Resolução CMN nº 5.130"))
    assert "res-cmn-5304" in refs and "res-cmn-5130" in refs


def test_build_lifecycle_card_enumerates_changes():
    narrs = [
        _card("Consulta pública sobre a minuta da Resolução BCB 999.", date="2026-07-10"),
        _card("A Resolução BCB 999 altera a Resolução BCB 700 e revoga o art. 5º; "
              "entra em vigor em 30/11/2026.", date="2026-08-15"),
    ]
    lc = regulatory.build_lifecycles(narrs, as_of="2026-08-23", window=90)["res-bcb-999"]
    card = regulatory.build_lifecycle_card(lc)
    assert card["n_changes"] >= 1
    rels = {c["relation"] for c in card["changes"]}
    assert "amends" in rels or "revokes" in rels
    assert "Mudanças:" in card["narrative"]


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


def test_stage_of_classifies_by_cue_precedence():
    assert regulatory.stage_of("a norma entra em vigor em 01/09") == "vigencia"
    assert regulatory.stage_of("aberta consulta publica sobre a minuta") == "consulta"
    assert regulatory.stage_of("BCB abre fiscalizacao e autuacao por descumprimento") == "fiscalizacao"
    assert regulatory.stage_of("o BCB publicou a resolucao") == "publicacao"  # default


def test_build_lifecycles_threads_stage_progression():
    narrs = [
        _card("Consulta pública sobre a minuta da Resolução BCB 999.", date="2026-07-10"),
        _card("BCB publicou a Resolução BCB 999.", date="2026-07-25"),
        _card("A Resolução BCB 999 entra em vigor em 30/11/2026.", date="2026-08-15"),
    ]
    lc = regulatory.build_lifecycles(narrs, as_of="2026-08-23", window=90)
    t = lc["res-bcb-999"]
    assert t["stages_seen"] == ["consulta", "publicacao", "vigencia"]
    assert t["current_stage"] == "vigencia" and t["status"] == "developing"
    assert t["deadline"] == "2026-11-30" and t["n_dates"] == 3
    c = regulatory.build_lifecycle_card(t)
    assert c["axis"] == "regulatory_lifecycle" and c["subject_type"] == "instrument"
    assert c["is_inference"] and "Ciclo regulatório" in c["narrative"]
    assert "Progressão:" in c["narrative"]


def test_single_date_instrument_is_not_a_lifecycle():
    # one mention on one date -> tracked by the radar, not a lifecycle thread
    assert regulatory.build_lifecycles(
        [_card("BCB publicou a Instrução Normativa BCB 770.", date="2026-08-20")],
        as_of="2026-08-23") == {}


# --- #51 instrument nexus: correct checking links + affected cohort -----------
def test_canonical_link_is_the_regulator_page_for_the_instrument():
    c = regulatory.canonical_link("res-cmn-5304", "Resolução CMN 5304")
    assert c and "numero=5304" in c["url"] and "bcb.gov.br" in c["url"]
    assert "Resolu" in c["url"] and "CMN" in c["url"] and c["source"] == "BCB"
    # a type without a stable canonical page (CVM) -> no fabricated link
    assert regulatory.canonical_link("res-cvm-175", "Resolução CVM 175") is None
    assert regulatory.canonical_link("regulamento-pix", "Regulamento do Pix") is None


def test_instrument_citations_drop_the_grab_bag():
    drivers = [{"citations": [
        {"url": "https://news.google.com/rss/articles/CBMijwFBVV"},                 # opaque
        {"url": "https://www.bcb.gov.br/.../exibenormativo?tipo=Resolu+CMN&numero=5337"},
        {"url": "https://www.bcb.gov.br/.../exibenormativo?tipo=Comunicado&numero=45835"},
        {"url": "https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?numProtocolo=1560374"},
        {"url": "https://www.bcb.gov.br/.../exibenormativo?tipo=Resolu+CMN&numero=5304"},  # the right one
    ]}]
    cites = regulatory._instrument_citations("res-cmn-5304", "Resolução CMN 5304", drivers)
    urls = " ".join(c["url"] for c in cites)
    assert "numero=5304" in urls
    assert "5337" not in urls and "5336" not in urls and "45835" not in urls
    assert "cvm.gov.br" not in urls and "news.google" not in urls
    # canonical page present exactly once (dedup collapses + vs %20)
    assert sum(1 for c in cites if c.get("canonical")) == 1
    assert sum("numero=5304" in c["url"] for c in cites) == 1


def test_lifecycle_card_names_cohort_and_scopes_links():
    t = {"instrument": "res-cmn-5304", "label": "Resolução CMN 5304",
         "domain": "Câmbio & mercado aberto", "deadline": None, "days_to_deadline": None,
         "status": "open", "current_stage": "vigencia", "stages_seen": ["vigencia"],
         "first_seen": "2026-08-28", "last_updated": "2026-08-28", "n_dates": 1,
         "timeline": [{"date": "2026-08-28", "stage": "vigencia", "summary": "...",
                       "citations": [
                           {"url": "https://www.bcb.gov.br/x/exibenormativo?tipo=Res+CMN&numero=5337"},
                           {"url": "https://www.bcb.gov.br/x/exibenormativo?tipo=Res+CMN&numero=5304"}]}]}
    card = regulatory.build_lifecycle_card(t)
    assert card["industries"] == ["banking", "investment-banking"]
    assert "exposição provável" in card["narrative"] and "Bancos" in card["narrative"]
    urls = " ".join(c["url"] for c in card["citations"])
    assert "numero=5304" in urls and "5337" not in urls


def test_radar_narrative_carries_change_record_when_provided():
    # §3-on-radar: a drafted record passed to build_narrative rides on the radar card.
    c = regulatory.nominate(
        [_card("Instrução Normativa BCB 770 altera a Resolução CMN 5130; entra em vigor em 30/11/2026.",
               date="2026-08-22")], as_of="2026-08-23")[0]
    rec = {"change": "x", "blast_radius": {"band": "sector", "n_entities": 5},
           "difficulty": {"band": "low"}, "is_inference": True}
    n = regulatory.build_narrative(c, change_record=rec)
    assert n["change_record"] is rec
    # default (no record) stays None
    assert regulatory.build_narrative(c)["change_record"] is None


def test_radar_narrative_tags_affected_industries():
    c = regulatory.nominate(
        [_card("Instrução Normativa BCB 770 sobre PIX; entra em vigor em 30/11/2026.",
               date="2026-08-22")], as_of="2026-08-23")[0]
    n = regulatory.build_narrative(c)
    assert n["industries"] == ["acquiring", "fintech", "banking"]
    assert "exposição provável" in n["narrative"]
    # links are scoped to the instrument (canonical IN BCB 770 page present)
    assert any("numero=770" in (cit.get("url") or "") for cit in n["citations"])


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
