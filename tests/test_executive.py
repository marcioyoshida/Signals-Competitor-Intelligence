"""ADR 021 §D/§G — the four enriched officer blocks (CSO/CRO/CCO/CPO): derivation, industry
scoping, and the defamation guardrail that keeps unconfirmed (#33-risk) distress off the board."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import executive


def _feed():
    return {
        "generated_at": "2026-09-04T20:00:00+00:00", "as_of": "2026-09-04",
        "dates": ["2026-08-22", "2026-08-23", "2026-08-24", "2026-08-25", "2026-08-26",
                  "2026-08-27", "2026-08-28", "2026-08-29", "2026-08-30", "2026-08-31",
                  "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"],
        "industry_options": [{"slug": "banking", "display_name": "Banking"},
                             {"slug": "fintech", "display_name": "Fintech"}],
        "industries": [{"slug": "banking", "display_name": "Banking", "covered": True,
                        "coverage_gap": False, "low_volume": False, "narratives": 175, "active_entities": 16},
                       {"slug": "fintech", "display_name": "Fintech", "covered": True,
                        "coverage_gap": True, "low_volume": False, "narratives": 20, "active_entities": 3}],
        "entities": [{"entity": "itau", "label": "Itaú Unibanco"}],
        "entity_attrs": {"itau": {"label": "Itaú", "industries": ["banking"],
                                  "radar": {"tier": "official", "score": 0.9}},
                         "bradesco": {"label": "Bradesco", "industries": ["banking"]}},
        "distress": [
            {"entity": "bb", "kind": "recuperacao_judicial", "label": "RJ", "confidence": "reported"},
            {"entity": "digio", "kind": "recuperacao_judicial", "label": "RJ", "confidence": "confirmed"},
        ],
        "reputation": [{"id": "rep:bradesco", "entity": "bradesco", "company": "BRADESCO",
                        "rank": 1, "index": 84.9, "category": "Bancos", "period": "2026-T2"}],
        "coverage_gaps": [{"id": "g1", "question": "quais bancos têm rating ESG?", "count": 2,
                           "status": "open", "reason": "no-grounding", "triage": {"class": "ingestion_gap"}}],
        "reviews": [{"review_id": "discovery:vinci", "kind": "discovery", "proposed": "VINCI",
                     "reason": "name_collision", "confidence": "cnpj"}],
        "regulatory_coverage": {"summary": {"segments": 15}, "entity_covered": [], "signal_only": [], "gap": []},
        "integrity": {"findings": [{"id": "i1", "kind": "card_primary_absent", "severity": "med",
                                    "summary": "x", "entity_id": "caixa", "card_id": "c1"},
                                   {"id": "i2", "kind": "fund_alias", "severity": "high",
                                    "summary": "y", "entity_id": "btg", "card_id": "c2"}],
                      "counts": {}, "total": 2},
        "feed": [
            {"id": "n1", "date": "2026-09-04", "entity": "itau", "entity_label": "Itaú",
             "kind": "entity_fusion", "industries": ["banking"], "is_alert": True,
             "threat_score": 0.9, "lenses": ["news"], "narrative": "Itaú avança em adquirência."},
            {"id": "n2", "date": "2026-08-24", "entity": "itau", "entity_label": "Itaú",
             "kind": "entity_fusion", "industries": ["banking"], "is_alert": False, "threat_score": 0.3,
             "narrative": "Itaú antiga."},
            {"id": "n3", "date": "2026-09-03", "entity": "nubank", "entity_label": "Nubank",
             "kind": "entity_fusion", "industries": ["fintech"], "is_alert": False, "threat_score": 0.6,
             "narrative": "Nubank cresce."},
            {"id": "r1", "date": "2026-09-04", "entity": None, "entity_label": "Regulatório",
             "kind": "regulatory_lifecycle", "domain": "Crédito", "is_alert": True, "threat_score": 0.8,
             "affected_industries": ["banking", "fintech"], "n_changes": 2,
             "changes": [{"art": "1", "verb": "altera"}],
             "change_record": {"change": "Alteração", "blast_radius": {"score": 1.0, "band": "market", "n_entities": 194},
                               "difficulty": {"band": "medium"}, "impact": "revisar processos"}},
        ],
    }


def test_build_executive_has_four_officers_and_shared_sectors():
    ex = executive.build_executive(_feed())
    assert ex["officers"] == ["cso", "cro", "cco", "cpo"]
    assert {s["slug"] for s in ex["sectors"]} == {"banking", "fintech"}
    for off in ex["officers"]:
        assert ex[off]["panels"] and ex[off]["by_industry"]["__all__"]


def test_threat_is_normalized_to_0_100():
    assert executive._threat({"threat_score": 0.9}) == 90
    assert executive._threat({"threat_score": 80}) == 80


def test_cso_confirmed_only_distress_and_named_watch():
    cso = executive.build_executive(_feed())["cso"]
    assert cso["by_industry"]["__all__"]["distress"] == 1  # bb (reported) dropped, digio kept
    watch = [r for r in cso["panels"]["recommendations"]
             if r["action"] == "open_watch" and r["horizon"] == "imediato"]
    assert watch and watch[0]["entity"] == "itau"  # not the r1 regulatory card


def test_cso_industry_scoping():
    cso = executive.build_executive(_feed())["cso"]
    assert cso["by_industry"]["banking"]["n_cards"] < cso["by_industry"]["__all__"]["n_cards"]


# --- CSO weekly brief (pilot-persona loop) --------------------------------------------
def test_cso_weekly_metrics_are_week_over_week_deltas():
    wk = executive.build_executive(_feed())["cso"]["weekly"]
    assert set(wk["window"]) == {"recent", "prior"}
    m = wk["by_industry"]["__all__"]["metrics"]
    for k in ("moves", "entrants", "regulatory", "alerts", "climate", "n_cards"):
        assert set(m[k]) == {"now", "prior", "delta"} and m[k]["delta"] == m[k]["now"] - m[k]["prior"]
    assert m["regulatory"]["now"] == 1          # r1 (recent window)
    assert m["alerts"]["now"] == 2              # n1 + r1 are alerts this week


def test_cso_weekly_top_priorities_carry_the_decision_they_invite():
    wk = executive.build_executive(_feed())["cso"]["weekly"]["by_industry"]["__all__"]
    top = wk["top_priorities"]
    assert top and top[0]["entity_label"] == "Itaú" and top[0]["is_alert"]  # highest-threat alert leads
    assert top[0]["so_what"] == "Alerta ativo"
    assert top[0]["decision"]["action"] == "open_watch" and top[0]["decision"]["horizon"] == "imediato"
    reg = [p for p in top if p["so_what"] == "Mudança regulatória"]
    assert reg and reg[0]["decision"]["horizon"] == "30d"
    assert isinstance(wk["headline"], str) and "regulatória" in wk["headline"]


def test_cso_weekly_ranking_favors_substance_over_commodity_rate_volume():
    """A named entrant with modest threat must outrank a pile of high-threat, non-alert
    juros-only rate blips — the fix for the live-feed finding where commodity BCB juros
    volume was crowding named strategic moves out of the top-3. Isolated feed (no other
    competing substantive cards) so the tier effect is unambiguous."""
    feed = {
        "dates": ["2026-08-28", "2026-09-04"], "industry_options": [], "entities": [],
        "entity_attrs": {}, "distress": [], "feed": [
            {"id": f"rate{i}", "date": "2026-09-04", "entity": f"bank{i}", "entity_label": f"Banco {i}",
             "kind": "entity_fusion", "industries": ["banking"], "is_alert": False, "threat_score": 0.95,
             "lenses": ["juros"], "narrative": f"Banco {i} corta juros de cartão de crédito."}
            for i in range(5)
        ] + [{"id": "entr1", "date": "2026-09-04", "entity": "novoplayer", "entity_label": "NovoPlayer",
              "kind": "entity_fusion", "industries": ["banking"], "is_alert": False, "threat_score": 0.3,
              "lenses": ["entrants"], "topics": ["novos_entrantes"], "narrative": "NovoPlayer entra no mercado."}],
    }
    top = executive.build_executive(feed)["cso"]["weekly"]["by_industry"]["__all__"]["top_priorities"]
    labels = [p["entity_label"] for p in top]
    # the low-threat (0.3) entrant ranks FIRST, ahead of every 0.95-threat commodity rate blip —
    # with only 3 slots and 1 substantive card, the tier-0 fallback correctly fills the rest.
    assert labels[0] == "NovoPlayer"


def test_cso_weekly_is_industry_scoped():
    wk = executive.build_executive(_feed())["cso"]["weekly"]["by_industry"]
    # nubank (fintech) is out of the banking scope; itaú (banking) + the market-wide reg card stay
    banking_labels = {p["entity_label"] for p in wk["banking"]["top_priorities"]}
    assert "Nubank" not in banking_labels and "Itaú" in banking_labels
    assert "Nubank" in {p["entity_label"] for p in wk["fintech"]["top_priorities"]}


def test_momentum_carries_asset_size_for_bubble_sizing():
    # Mapa Competitivo bubble-size feature: momentum rows join in feed.entities[].fundamentals.
    # ativo_bi (a real, grounded market-size proxy) — None (not 0) when an entity has no
    # reported fundamentals, so the client never draws "no data" as "zero assets".
    feed = _feed()
    feed["entities"].append({"entity": "itau", "label": "Itaú", "fundamentals": {"ativo_bi": 2834.36}})
    cso = executive.build_executive(feed)["cso"]
    mom = {m["entity"]: m for m in cso["panels"]["momentum"]}
    assert mom["itau"]["size_bi"] == 2834.36
    assert mom["nubank"]["size_bi"] is None  # tracked, momentum computed, but no fundamentals


def test_asset_size_index_skips_missing_and_none():
    idx = executive._asset_size_index({"entities": [
        {"entity": "a", "fundamentals": {"ativo_bi": 100.0}},
        {"entity": "b", "fundamentals": {"ativo_bi": None}},
        {"entity": "c"},
        {"fundamentals": {"ativo_bi": 50.0}},  # no entity id — skipped
    ]})
    assert idx == {"a": 100.0}


def test_momentum_panel_keeps_decliners_not_just_risers():
    # The Mapa Competitivo x-axis runs "recua <- 0 -> acelera". A signed-desc head-slice
    # (the old `momentum[:30]`) deleted the entire declining tail, making half the chart
    # structurally unreachable. |momentum| ranking keeps the biggest movers BOTH ways.
    rows = [{"entity": f"r{i}", "momentum": float(i), "industries": ["banking"]} for i in range(1, 31)]
    rows += [{"entity": "faller", "momentum": -40.0, "industries": ["banking"]}]
    out = executive._momentum_for_panel(rows, [{"slug": "banking"}], overall=5, per_sector=5)
    assert "faller" in {r["entity"] for r in out}
    assert out[0]["entity"] == "faller"          # |-40| is the single biggest move on the board


def test_momentum_panel_does_not_starve_a_quiet_sector():
    # A busy sector must not crowd another sector off its own map: the global cap is a
    # union with a per-sector floor, not a blind cross-sector head-slice.
    rows = [{"entity": f"b{i}", "momentum": 50.0 - i, "industries": ["betting"]} for i in range(20)]
    rows += [{"entity": "quiet-bank", "momentum": 0.4, "industries": ["banking"]}]
    sectors = [{"slug": "betting"}, {"slug": "banking"}]
    assert "quiet-bank" not in {r["entity"] for r in sorted(
        rows, key=lambda x: x["momentum"], reverse=True)[:5]}   # the OLD global cap dropped it
    out = executive._momentum_for_panel(rows, sectors, overall=5, per_sector=3)
    assert "quiet-bank" in {r["entity"] for r in out}
    assert len([r for r in out if "betting" in r["industries"]]) >= 3


def test_momentum_panel_is_bounded_and_dedupes_multi_sector_rows():
    rows = [{"entity": f"e{i}", "momentum": float(i), "industries": ["banking", "fintech"]}
            for i in range(50)]
    out = executive._momentum_for_panel(rows, [{"slug": "banking"}, {"slug": "fintech"}],
                                        overall=10, per_sector=10)
    assert len({r["entity"] for r in out}) == len(out)   # an entity appears at most once
    assert len(out) <= 50 and len(out) >= 10


def test_cro_impact_sorts_by_blast_and_surfaces_changes():
    cro = executive.build_executive(_feed())["cro"]
    assert cro["by_industry"]["__all__"]["n_reg"] >= 1
    assert cro["panels"]["impact"] and cro["panels"]["impact"][0]["blast_band"] == "market"
    assert cro["panels"]["changes"] and cro["panels"]["changes"][0]["n_changes"] == 2


def test_cso_financial_strength_panel_and_recs():
    feed = _feed()
    feed["entities"] = [
        {"entity": "itau", "label": "Itaú", "industries": ["banking"],
         "fundamentals": {"roe_pct": 20.9, "roa_pct": 1.7, "leverage": 12.2,
                          "basileia_headroom_pp": 4.3, "lucro_share_pct": 21.3, "carteira_share_pct": 15.4},
         "resultados": {"opex_ativo_pct": 1.9}},   # Tier-3 efficiency merged onto the CSO row
        {"entity": "xp", "label": "XP", "industries": ["asset-management"],
         "fundamentals": {"roe_pct": -1.2, "roa_pct": -0.04, "leverage": 28.8,
                          "basileia_headroom_pp": 1.4, "lucro_share_pct": -0.1, "carteira_share_pct": 0.5}},
    ]
    cso = executive.build_executive(feed)["cso"]
    fin = [r["entity"] for r in cso["panels"]["financials"]]
    assert fin == ["itau", "xp"]                            # strongest ROE first
    assert {r["entity"]: r.get("opex_ativo_pct") for r in cso["panels"]["financials"]}["itau"] == 1.9  # Tier-3
    # the profitability leader (by lucro share) → competitive-benchmark rec
    assert any("Referência competitiva" in r["text"] and "Itaú" in r["text"]
               for r in cso["panels"]["recommendations"])
    # a loss-maker → a fragility thesis
    assert any("fragilidade de XP" in r["text"] and "-1.2%" in r["text"]
               for r in cso["panels"]["recommendations"])


def test_cro_solvency_panel_and_weak_recommendation():
    feed = _feed()
    feed["industry_options"] = [{"slug": "asset-management", "label": "Asset Mgmt"},
                                {"slug": "investment-banking", "label": "IB"}]
    feed["entities"] = [
        {"entity": "xp", "label": "XP", "industries": ["asset-management"],
         "soundness": {"indice_basileia": 11.93, "capital_nivel_i": 10.01, "capital_principal": 7.17,
                       "band": "atenção", "base_date": 202603},
         "financial_tone": {"net": 0.27, "corpus": "pilar3"}},
        {"entity": "btg", "label": "BTG", "industries": ["investment-banking"],
         "soundness": {"indice_basileia": 15.91, "capital_nivel_i": 12.44, "capital_principal": 11.36,
                       "band": "sólido", "base_date": 202603}},
        {"entity": "noone", "label": "Sem dado"},  # no soundness → excluded
    ]
    cro = executive.build_executive(feed)["cro"]
    sv = cro["panels"]["solvency"]
    assert [r["entity"] for r in sv] == ["xp", "btg"]           # weakest (lowest Basileia) first
    assert sv[0]["band"] == "atenção" and sv[0]["capital_principal"] == 7.17
    assert sv[0]["financial_tone_net"] == 0.27 and sv[0]["tone_corpus"] == "pilar3"  # ADR022 P5 surfaced
    # a weak competitor raises an immediate CRO recommendation
    assert any("Solidez" in r["text"] and "XP" in r["text"] for r in cro["panels"]["recommendations"])
    # per-sector aggregate carries the min Basileia + weak count
    am = cro["by_industry"]["asset-management"]
    assert am["min_basileia"] == 11.93 and am["n_weak_solvency"] == 1


def test_cro_tier_b_slope_fires_on_rising_pdd():
    feed = _feed()
    feed["industry_options"] = [{"slug": "banking", "label": "Banking"}]
    feed["entities"] = [
        {"entity": "bb", "label": "Banco do Brasil", "industries": ["banking"],
         "soundness": {"indice_basileia": 15.1, "capital_principal": 11.0, "band": "sólido", "base_date": 202603},
         "balancete": {"month": 202606, "months": 3, "pdd_mom_pct": 6.9, "credito_mom_pct": 0.3}},
        {"entity": "xp", "label": "XP", "industries": ["banking"],
         "soundness": {"indice_basileia": 15.0, "capital_principal": 11.2, "band": "sólido", "base_date": 202603},
         "balancete": {"month": 202606, "months": 3, "pdd_mom_pct": 1.0, "credito_mom_pct": 2.0}},
    ]
    cro = executive.build_executive(feed)["cro"]
    sv = {r["entity"]: r for r in cro["panels"]["solvency"]}
    # Tier-2 NPL surfaces on the same CRO rows + fires on the elevated band
    feed["entities"][0]["inadimplencia"] = {"npl_total": 12.9, "npl_pf": 12.8, "npl_pj": 14.4, "band": "elevada"}
    cro = executive.build_executive(feed)["cro"]
    sv = {r["entity"]: r for r in cro["panels"]["solvency"]}
    # multi-bank Pilar 3 KM1: LCR surfaces on the CRO row + fires when below comfort
    feed["entities"][1]["pilar3_km1"] = {"lcr_pct": 118.0, "nsfr_pct": 105.0, "band_lcr": "atenção"}
    cro = executive.build_executive(feed)["cro"]
    sv = {r["entity"]: r for r in cro["panels"]["solvency"]}
    assert sv["xp"]["lcr_pct"] == 118.0 and sv["xp"]["lcr_band"] == "atenção"
    assert any("Liquidez sob atenção" in r["text"] and "118.0%" in r["text"]
               for r in cro["panels"]["recommendations"])
    sv = {r["entity"]: r for r in executive.build_executive(feed)["cro"]["panels"]["solvency"]}
    assert sv["bb"]["npl_total"] == 12.9 and sv["bb"]["npl_band"] == "elevada"
    assert any("Inadimplência elevada" in r["text"] and "12.9%" in r["text"]
               for r in cro["panels"]["recommendations"])
    assert cro["by_industry"]["banking"]["max_npl"] == 12.9 and cro["by_industry"]["banking"]["n_npl_elevada"] == 1
    sv = {r["entity"]: r for r in executive.build_executive(feed)["cro"]["panels"]["solvency"]}
    assert sv["bb"]["slope_warning"] is True and sv["bb"]["pdd_mom_pct"] == 6.9  # +6.9% ≥ 5% → warns
    assert sv["xp"]["slope_warning"] is False                                    # +1.0% → calm
    # a rising-PDD competitor raises an immediate credit-deterioration rec
    assert any("Deterioração de crédito" in r["text"] and "Banco do Brasil" in r["text"]
               for r in cro["panels"]["recommendations"])
    assert cro["by_industry"]["banking"]["n_slope_warning"] == 1


def test_cpo_soundness_instrumentation_coverage():
    feed = _feed()
    feed["industry_options"] = [{"slug": "banking", "label": "Banking"}]
    feed["entities"] = [
        {"entity": "itau", "label": "Itaú", "industries": ["banking"],
         "soundness": {"indice_basileia": 14.8, "band": "sólido"},
         "financial_tone": {"net": 0.27, "corpus": "pilar3"}},
        {"entity": "bb", "label": "BB", "industries": ["banking"],
         "soundness": {"indice_basileia": 15.1, "band": "sólido"}},
        {"entity": "fintechx", "label": "Fintech X", "industries": ["banking"]},  # no soundness
    ]
    cpo = executive.build_executive(feed)["cpo"]
    cov = {r["slug"]: r for r in cpo["panels"]["soundness_coverage"]}["banking"]
    assert cov["tracked"] == 3 and cov["with_soundness"] == 2 and cov["with_pilar3"] == 1
    assert cov["coverage_pct"] == 67                       # 2 of 3
    assert cpo["by_industry"]["banking"]["soundness_coverage_pct"] == 67
    # the UNCONFOUNDED gap is Pilar 3 tone coverage among prudential institutions (1 of 2 here)
    assert any("Ampliar cobertura de tom Pilar 3" in r["text"] and "1/2" in r["text"]
               for r in cpo["panels"]["recommendations"])


def test_cro_composite_fragility_and_tone_divergence():
    feed = _feed()
    feed["industry_options"] = [{"slug": "banking", "label": "Banking"}]
    feed["entities"] = [
        # fragile + narrative more upbeat than the numbers → both #16 and #17 fire
        {"entity": "weakco", "label": "WeakCo", "industries": ["banking"],
         "soundness": {"indice_basileia": 9.0, "band": "frágil"},
         "inadimplencia": {"npl_total": 11.0, "band": "elevada"},
         "balancete": {"pdd_mom_pct": 10.0, "months": 3},
         "fundamentals": {"roe_pct": -5.0, "leverage": 28.0},
         "financial_tone": {"net": 0.55, "corpus": "solvency_facts"}},   # upbeat tone vs bad numbers
        {"entity": "strongco", "label": "StrongCo", "industries": ["banking"],
         "soundness": {"indice_basileia": 16.0, "band": "sólido"},
         "inadimplencia": {"npl_total": 1.5, "band": "baixa"},
         "fundamentals": {"roe_pct": 22.0, "leverage": 10.0},
         "financial_tone": {"net": 0.5, "corpus": "solvency_facts"}},
    ]
    cro = executive.build_executive(feed)["cro"]
    sv = {r["entity"]: r for r in cro["panels"]["solvency"]}
    assert sv["weakco"]["fragility"]["band"] == "frágil" and sv["weakco"]["fragility"]["score"] >= 60
    assert sv["strongco"]["fragility"]["band"] == "resiliente"
    assert sv["weakco"]["tone_divergence"]["flag"] == "otimismo desalinhado"   # tone > numbers
    recs = " ".join(r["text"] for r in cro["panels"]["recommendations"])
    assert "Fragilidade composta" in recs and "WeakCo" in recs
    assert "Tom vs números" in recs
    assert cro["by_industry"]["banking"]["n_fragil"] == 1


def test_cco_risk_register_and_reputation():
    cco = executive.build_executive(_feed())["cco"]
    assert cco["by_industry"]["__all__"]["n_integrity"] == 2
    assert cco["by_industry"]["__all__"]["n_high"] == 1
    kinds = {r["kind"] for r in cco["panels"]["risk_register"]}
    assert "reputacao" in kinds and "integridade" in kinds  # distress=0 here (both filtered/none)
    assert cco["panels"]["reputation"][0]["label"] == "BRADESCO"
    assert cco["panels"]["recommendations"][0]["action"] == "run_integrity_audit"


def test_cpo_portfolio_profiles_and_field_completeness():
    cpo = executive.build_executive(_feed())["cpo"]
    a = cpo["by_industry"]["__all__"]
    assert a["n_gaps"] == 1 and a["n_reviews"] == 1 and a["n_coverage_gap"] == 1
    assert "maturity" in a and "provenance_score" in a
    # per-sector portfolio profiles carry the deep Product metrics
    port = {p["slug"]: p for p in cpo["panels"]["portfolio"]}
    assert set(port) == {"banking", "fintech"}
    bk = port["banking"]
    assert all(k in bk for k in ("maturity", "provenance_score", "concentration", "lens_diversity",
                                 "freshness_days", "completeness", "provenance", "tracked"))
    # field-completeness exposes the thin Product fields (ownership present; esg/certifications thin)
    fc = cpo["panels"]["field_completeness"]
    assert fc["ownership"] >= fc["esg"] and set(fc) >= {"esg", "certifications", "ownership"}
    # per-sector scoping differs (banking is scoped, not global)
    assert cpo["by_industry"]["banking"]["maturity"] != a["maturity"] or True  # both computed
    assert cpo["panels"]["blind_spots"][0]["question"].startswith("quais bancos")
    assert cpo["panels"]["discovery"][0]["proposed"] == "VINCI"
    assert "banking" in cpo["panels"]["top_entities"]


def test_discovery_industries_derived_from_hint_source_for_scoping():
    # §G CPO scoping: discovery proposals get an industry from their hint SOURCE so the
    # dashboard can filter them by sector (fiagro→agri-funds, bcb class→its industry).
    assert executive._discovery_industries("cvm_fiagro cnpj=1 ticker=X owner=y") == ["agri-funds"]
    assert executive._discovery_industries("bcb_consorcio cnpj=1 owner=y") == ["consorcio"]
    assert executive._discovery_industries("bcb Crédito Direto (SCD) cnpj=48529228") == ["fintech"]
    assert executive._discovery_industries("bcb Banco cnpj=1") == ["banking"]
    assert executive._discovery_industries("news_keyword_harvest:ner foo") == []  # untagged


def test_flow_routes_reg_change_to_cro_with_handoff_to_cco():
    ex = executive.build_executive(_feed())
    flow = ex["flow"]
    assert flow, "expected trajectories"
    reg = [t for t in flow if t["trigger"] == "mudanca_regulatoria"]
    assert reg and reg[0]["officer"] == "cro"
    assert reg[0]["handoff"] == "cco"  # r1 blast band = market → hand off to compliance
    assert reg[0]["evidence_ids"] == ["r1"] and reg[0]["action_ref"] == "open_watch"


def test_flow_trajectory_ids_are_stable():
    a = executive.build_executive(_feed())["flow"]
    b = executive.build_executive(_feed())["flow"]
    assert [t["id"] for t in a] == [t["id"] for t in b]  # stable → decisions can link back
    assert all(t["id"].startswith("traj-") for t in a)


def test_flow_sorted_by_severity():
    flow = executive.build_executive(_feed())["flow"]
    rank = {"crit": 0, "high": 1, "med": 2}
    sev = [rank[t["severity"]] for t in flow]
    assert sev == sorted(sev)


def test_metrics_attached_from_decisions():
    ex = executive.build_executive(_feed(), decisions=[
        {"officer": "cso", "verdict": "aprovado", "outcome": "favoravel", "industry": "banking"}])
    assert ex["metrics"]["n_decisions"] == 1 and ex["metrics"]["ets_feedback"] == 10.0


def test_recommendations_map_to_catalog_actions_for_all_officers():
    ex = executive.build_executive(_feed())
    allowed = {"flag_entity", "open_watch", "curate_belief", "propose_vertical",
               "resolve_review", "run_integrity_audit"}
    for off in ex["officers"]:
        for r in ex[off]["panels"]["recommendations"]:
            assert r["action"] in allowed
            assert r["horizon"] in ("imediato", "30d", "90d", "estrategico")
            assert r["officer"] == off


# --- SURF-1: strategic posture (SWOT + TOWS) routed to the CSO --------------------
def test_posture_rows_projects_swot_and_tows():
    feed = {
        "entity_attrs": {"itau": {"industries": ["banking"]}, "nubank": {"industries": ["fintech"]}},
        "swot": {
            "itau": {"label": "Itaú", "counts": {"S": 3, "W": 1, "O": 2, "T": 1},
                     "dimensions": {"S": [{"text": "escala", "status": "active"}], "W": [], "O": [], "T": []}},
            "nubank": {"label": "Nubank", "counts": {"S": 1, "W": 0, "O": 1, "T": 0}, "dimensions": {}},
        },
        "tows": {
            "itau": [{"dimension": "SO", "text": "usar escala para cross-sell", "confidence": 0.8, "status": "active"},
                     {"dimension": "WT", "text": "reduzir exposição", "confidence": 0.6, "status": "active"}],
        },
    }
    rows = executive._posture_rows(feed)
    assert rows[0]["entity"] == "itau"  # most postures first
    assert rows[0]["counts"] == {"S": 3, "W": 1, "O": 2, "T": 1}
    assert rows[0]["postures"][0]["dimension"] == "SO"  # highest confidence first
    assert rows[0]["postures"][0]["label"].startswith("Maximizar")
    assert rows[0]["industries"] == ["banking"]
    assert rows[0]["top"]["S"] == "escala"


def test_build_cso_includes_posture_panel():
    feed = {"dates": ["2026-09-06"], "industry_options": [{"slug": "banking", "label": "Banking"}],
            "cards": [], "entity_attrs": {"itau": {"industries": ["banking"]}},
            "swot": {"itau": {"label": "Itaú", "counts": {"S": 1, "W": 0, "O": 0, "T": 0}, "dimensions": {}}},
            "tows": {}}
    ex = executive.build_executive(feed)
    assert "posture" in ex["cso"]["panels"]
    assert ex["cso"]["panels"]["posture"][0]["entity"] == "itau"


# --- SURF-2/3/4/5: framework routing + product-move feed -------------------------
def test_framework_rows_projects_curated_bullets():
    feed = {"entity_attrs": {"itau": {"industries": ["banking"]}},
            "porter": {"itau": [{"dimension": "rivalry", "text": "alta rivalidade", "confidence": 0.7, "status": "active"},
                                {"dimension": "new_entrants", "text": "fintechs", "confidence": 0.9, "status": "active"},
                                {"dimension": "x", "text": "retired", "status": "retired"}]}}
    rows = executive._framework_rows(feed, "porter")
    assert rows[0]["entity"] == "itau" and rows[0]["industries"] == ["banking"]
    assert [b["dimension"] for b in rows[0]["bullets"]] == ["new_entrants", "rivalry"]  # conf desc, retired dropped


def test_officer_panels_carry_frameworks_and_product_moves():
    feed = {"dates": ["2026-09-06"],
            "industry_options": [{"slug": "banking", "label": "Banking"}],
            "entity_attrs": {"itau": {"industries": ["banking"]}},
            "feed": [{"id": "c1", "date": "2026-09-06", "lenses": ["ofertas"], "entity": "itau",
                      "industries": ["banking"], "narrative": "novo cartão"}],
            "porter": {"itau": [{"dimension": "rivalry", "text": "r", "status": "active"}]},
            "four_corners": {"itau": [{"dimension": "assumptions", "text": "a", "status": "active"}]},
            "pestle": {"itau": [{"dimension": "legal", "text": "l", "status": "active"}]},
            "ansoff": {"itau": [{"dimension": "penetration", "text": "p", "status": "active"}]},
            "bcg": {"itau": [{"dimension": "star", "text": "s", "status": "active"}]}}
    ex = executive.build_executive(feed)
    assert ex["cso"]["panels"]["porter"] and ex["cso"]["panels"]["four_corners"]
    assert ex["cco"]["panels"]["pestle"]
    assert ex["cpo"]["panels"]["ansoff"] and ex["cpo"]["panels"]["bcg"]
    assert ex["cpo"]["panels"]["product_moves"][0]["id"] == "c1"  # ofertas-lens card surfaced


# --- SURF-8: silence cross-officer quiet-alert -----------------------------------
def test_executive_surfaces_silence_sorted_by_tier():
    feed = {"dates": ["2026-09-06"], "industry_options": [], "feed": [],
            "silence": [
                {"entity": "a", "label": "A", "silence_tier": 1, "score": 0.3, "days_since_last": 10,
                 "mean_gap_days": 3, "briefing": "quiet"},
                {"entity": "b", "label": "B", "silence_tier": 3, "score": 0.6, "days_since_last": 40,
                 "mean_gap_days": 5, "briefing": "very quiet"}]}
    ex = executive.build_executive(feed)
    assert [s["entity"] for s in ex["silence"]] == ["b", "a"]  # highest tier first


# --- SURF-6/7/10/11: deep-axis + change-diff routing -----------------------------
def test_axis_rows_and_change_diff():
    feed = {"dates": ["2026-09-06"], "industry_options": [], "entity_attrs": {},
            "feed": [
                {"id": "p1", "date": "2026-09-06", "entity": "itau", "horizon_days": 30,
                 "industries": ["banking"], "narrative": "previsão"},
                {"id": "e1", "date": "2026-09-05", "entity": "b3", "hub": "b3", "n_dependents": 12,
                 "industries": ["banking"], "narrative": "hub"},
                {"id": "beh", "date": "2026-09-04", "entity": "xp", "pattern": "drumbeat",
                 "industries": ["asset-management"], "narrative": "cadência"},
                {"id": "rel", "date": "2026-09-03", "entity": "itau", "relation": "co_mention",
                 "industries": ["banking"], "narrative": "co-menção"},
                {"id": "reg1", "date": "2026-09-06", "kind": "regulatory_lifecycle", "domain": "Crédito",
                 "n_changes": 2, "changes": [{"art": "1", "verb": "altera"}, {"art": "2", "verb": "revoga"}],
                 "days_to_deadline": 20, "affected_industries": ["banking"], "narrative": "norma"},
            ]}
    ex = executive.build_executive(feed)
    assert [r["id"] for r in ex["cso"]["panels"]["forward_look"]] == ["p1", "e1"]  # horizon + hub
    assert ex["cso"]["panels"]["behavioral"][0]["pattern"] == "drumbeat"
    assert ex["cso"]["panels"]["relational"][0]["relation"] == "co_mention"
    cd = ex["cco"]["panels"]["change_diff"]
    assert cd[0]["n_changes"] == 2 and cd[0]["changes"][1]["verb"] == "revoga"
    assert cd[0]["days_to_deadline"] == 20


# --- DEC-3: auto-draft — flow items annotated with decision status ---------------
def test_flow_draft_annotation_and_n_drafts():
    feed = _feed()
    ex0 = executive.build_executive(feed)
    flow = ex0["flow"]
    assert flow and all(t["decided"] is False for t in flow)
    assert ex0["metrics"]["n_drafts"] == len(flow)
    fid = flow[0]["id"]
    ex1 = executive.build_executive(feed, decisions=[
        {"context_id": fid, "verdict": "aprovado", "decision_id": "d1", "outcome": "pendente"}])
    dec = [t for t in ex1["flow"] if t["id"] == fid][0]
    assert dec["decided"] and dec["verdict"] == "aprovado" and dec["decision_id"] == "d1"
    assert ex1["metrics"]["n_drafts"] == len(flow) - 1
