"""Ask grounding on per-entity BCB metrics + untracked distress rows (live 11-06 Basileia decline)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import agent_ask as aa


def _feed():
    return {
        "run_date": "2026-11-07",
        "entities": [
            {"entity": "banco_x", "label": "Banco X",
             "soundness": {"indice_basileia": 15.56, "capital_nivel_i": 12.85, "capital_principal": 11.28,
                           "razao_alavancagem": 6.79, "indice_imobilizacao": 31.61, "band": "sólido",
                           "base_date": 202606},
             "pilar3_km1": {"lcr_pct": 145.0, "nsfr_pct": 121.0, "band_lcr": "confortável", "lcr_qoq_pp": -4.0,
                            "basileia_pct": 16.0, "leverage_pct": 7.0, "trimestre": "2026-3"},
             "fundamentals": {"roe_pct": 13.1, "roa_pct": 1.2, "leverage": 10.9, "ativo_bi": 2015.22,
                              "lucro_bi": 12.113, "base_date": 202606},
             "inadimplencia": {"npl_total": 2.74, "npl_pf": 3.78, "npl_pj": 1.54, "band": "baixa",
                               "base_date": 202606}},
            {"entity": "banco_y", "label": "Banco Y"},
        ],
        "entity_attrs": {"banco_x": {"label": "Banco X", "industries": ["banking"]},
                         "banco_y": {"label": "Banco Y", "industries": ["banking"]}},
        "feed": [{"id": "cand-ent-banco_x", "date": "2026-11-07", "entity": "banco_x", "entity_label": "Banco X",
                  "entities": ["banco_x"], "lenses": ["news"], "narrative": "Banco X divulgou resultados do 3T26.",
                  "citations": [{"url": "https://example.org/n"}]}],
        "distress": [{"entity": None, "kind": "liquidacao_extrajudicial", "label": "Liquidação Extrajudicial",
                      "first_seen": "2026-10-16", "last_seen": "2026-11-07", "subject_name": "COOPERATIVA TESTE LTDA",
                      "cnpj": "11111111", "latest_title": "COOPERATIVA TESTE LTDA: liquidação",
                      "latest_url": "https://www.bcb.gov.br/x", "tracked": False}],
    }


def test_bank_metric_cards_carry_basileia_with_source_and_date():
    cards = {c["id"]: c for c in aa.bank_metric_cards(_feed())}
    assert set(cards) == {"prud:banco_x:202606", "km1:banco_x:2026-3", "fund:banco_x:202606"}
    p = cards["prud:banco_x:202606"]["narrative"]
    assert "data-base 06/2026" in p and "Capital Nível I 12,85%" in p and "faixa de solidez: sólido" in p
    k = cards["km1:banco_x:2026-3"]["narrative"]
    assert "3T2026" in k and "(LCR) 145%" in k
    f = cards["fund:banco_x:202606"]["narrative"]
    assert "ROE 13,1% a.a." in f and "inferência" in f and "R$ 2.015,2 bi" in f and "15+ dias 2,74%" in f
    assert all(c["citations"] and c["entities"] == ["banco_x"] for c in cards.values())


def _two_sources(s_base, k_tri):
    feed = _feed()
    e = feed["entities"][0]
    e["soundness"]["base_date"], e["pilar3_km1"]["trimestre"] = s_base, k_tri
    return {c["id"].split(":")[0]: c for c in aa.bank_metric_cards(feed)}


def test_newer_km1_leads_basileia_and_older_ifdata_follows_as_comparison():
    c = _two_sources(202606, "2026-3")
    k, p = c["km1"]["narrative"], c["prud"]["narrative"]
    assert "Índice de Basileia 16% (3T2026; anterior: 15,56% em 06/2026, BCB IF.data)" in k
    assert "Razão de Alavancagem 7% (3T2026; anterior: 6,79% em 06/2026" in k
    assert "Basileia 1" not in p and "Alavancagem" not in p and "folga" not in p   # stated once
    assert "folga" not in k                                     # whole-percent KM1: no false precision
    assert {x["url"] for x in c["km1"]["citations"]} == {aa.DASFN_URL, aa.IFDATA_URL}


def test_newer_ifdata_leads_and_km1_follows():
    c = _two_sources(202609, "2026-2")
    k, p = c["km1"]["narrative"], c["prud"]["narrative"]
    assert "Índice de Basileia 15,56% (09/2026; anterior: 16% em 2T2026, Pilar 3 KM1)" in p
    assert "folga sobre o mínimo regulatório de 10,5%: 5,06 p.p." in p
    assert "Basileia" not in k.split("):", 1)[1] and "(LCR) 145%" in k
    assert {x["url"] for x in c["prud"]["citations"]} == {aa.IFDATA_URL, aa.DASFN_URL}


def test_same_base_date_states_the_ifdata_figure_once_without_comparison():
    c = _two_sources(202609, "2026-3")
    assert "Índice de Basileia 15,56% (09/2026)" in c["prud"]["narrative"]
    assert "anterior" not in c["prud"]["narrative"] and "Basileia" not in c["km1"]["narrative"].split("):", 1)[1]


def test_single_source_states_its_own_figure():
    feed = _feed()
    feed["entities"][0].pop("pilar3_km1")
    c = {x["id"].split(":")[0]: x for x in aa.bank_metric_cards(feed)}
    assert "Índice de Basileia 15,56% (06/2026)" in c["prud"]["narrative"] and "km1" not in c


def test_system_prompt_asks_for_the_most_recent_figure_first():
    system, _ = aa.build_messages("q", [])
    assert "comece pelo valor mais recente" in system


def test_entity_without_metrics_gets_no_card():
    assert not [c for c in aa.bank_metric_cards(_feed()) if c["entity"] == "banco_y"]


def test_basileia_question_grounds_on_prudential_cards():
    seen = {}
    def conv(user, system=None, max_tokens=700, history=None):
        seen["u"] = user
        return "O Índice de Basileia do Banco X é 15,56% (06/2026) [prud:banco_x:202606]."
    for mods in (None, ["banking"]):
        r = aa.answer("Qual o índice de Basileia do Banco X?", feed=_feed(), modules=mods, converser=conv)
        assert "[prud:banco_x:202606]" in seen["u"] and "[km1:banco_x:2026-3]" in seen["u"]
        assert r["grounded"] is True and r.get("reason") is None


def test_untracked_distress_card_does_not_crash_unscoped_ask():
    # #355 rows carry entity None; `entities: [None]` raised TypeError in _card_blob (unscoped Ask).
    cards = aa.distress_cards(_feed())
    assert cards[0]["entities"] == [] and cards[0]["entity_label"] == "COOPERATIVA TESTE LTDA"
    assert cards[0]["id"] == "distress:cnpj:11111111:liquidacao_extrajudicial"
    r = aa.answer("Qual o índice de Basileia do Banco X?", feed=_feed(), converser=lambda *a, **k: "ok [prud:banco_x:202606]")
    assert r["grounded"] is True
    # a scoped tenant still never sees the untracked row (no attributable entity)
    assert aa._scope_cards_to_modules(cards, _feed(), ["banking"]) == []
