import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import thematic


def _card(entity, text, *, date="2026-08-22", threat=0.5, axis=None, theme=None,
          entity_label=None, is_inference=False):
    n = {
        "entity": entity,
        "entity_label": entity_label,
        "narrative": text,
        "threat_score": threat,
        "run_date": date,
        "lenses": ["market"],
        "citations": [{"url": f"https://ex/{entity}"}],
    }
    if axis:
        n["axis"] = axis
        n["mode"] = "derived"
        n["is_inference"] = True
    if theme:
        n["theme"] = theme
    if is_inference:
        n["is_inference"] = True
        n["mode"] = "derived"
    return n


def _crypto_window():
    # 4 distinct competitors, 5 mentions on a crypto current
    return [
        _card("mb", "Stablecoins de dólar ganham espaço no Mercado Bitcoin (cripto)."),
        _card("coinbase", "Coinbase expande estratégia de criptomoedas no Brasil."),
        _card("binance", "Binance lança novo produto de bitcoin e token."),
        _card("foxbit", "Foxbit amplia serviços de cripto e blockchain."),
        _card("mb", "Mercado Bitcoin registra recorde em criptomoedas.", date="2026-08-20"),
    ]


def test_themes_of_tags_by_keyword():
    got = thematic.themes_of(_card("x", "A empresa registrou prejuizo e lucro liquido no 2T26."))
    assert "quarterly_results" in got
    got2 = thematic.themes_of(_card("x", "Inadimplencia sobe com o corte no credito."))
    assert "credit_risk" in got2
    # accent-insensitive
    assert "credit_risk" in thematic.themes_of(_card("x", "A inadimplência acelera."))


def test_nominate_flags_current_over_threshold():
    cands = {c["theme"]: c for c in thematic.nominate(_crypto_window(), as_of="2026-08-23")}
    assert "crypto" in cands
    c = cands["crypto"]
    assert c["entity_count"] == 4 and c["mentions"] == 5
    assert c["dim"] == "O"


def test_below_threshold_not_a_current():
    # only 2 distinct entities -> below MIN_ENTITIES
    narrs = [
        _card("mb", "Cripto em alta no Mercado Bitcoin."),
        _card("coinbase", "Coinbase amplia criptomoedas."),
        _card("mb", "Mais bitcoin no Mercado Bitcoin.", date="2026-08-21"),
        _card("mb", "Token novo no Mercado Bitcoin.", date="2026-08-20"),
    ]
    assert [c for c in thematic.nominate(narrs, as_of="2026-08-23") if c["theme"] == "crypto"] == []


def test_distinct_entity_gate_ignores_none_entity_cards():
    # 2 real entities + 3 generic (entity=None) crypto mentions -> still below gate
    narrs = [
        _card("mb", "Cripto no Mercado Bitcoin."),
        _card("coinbase", "Criptomoedas na Coinbase."),
        _card(None, "BCB comenta stablecoin."),
        _card(None, "Nota sobre bitcoin."),
        _card(None, "Token e blockchain em debate."),
    ]
    cr = [c for c in thematic.nominate(narrs, as_of="2026-08-23") if c["theme"] == "crypto"]
    assert cr == []  # 2 distinct entities < MIN_ENTITIES, generic cards don't count


def test_swot_hint_maps_dimension():
    cands = {c["theme"]: c for c in thematic.nominate(_crypto_window(), as_of="2026-08-23")}
    assert thematic.swot_hint(cands["crypto"]) == {
        "dimension": "O", "sign": "+", "theme": "crypto",
        "entities": cands["crypto"]["entities"]}
    # data-cadence theme carries no hint
    assert thematic.swot_hint({"dim": None, "theme": "quarterly_results",
                               "entities": []}) is None


def test_emit_on_change_suppresses_recent_same_tier():
    window = _crypto_window()
    prior = [_card(None, "prev current", date="2026-08-21", axis="thematic",
                   theme="crypto")]
    prior[0]["theme_tier"] = 1
    got = [c["theme"] for c in thematic.nominate(
        window + prior, as_of="2026-08-23")]
    assert "crypto" not in got  # standing current within cooldown, same tier


def test_build_narrative_is_labeled_and_capped():
    cand = thematic.nominate(_crypto_window(), as_of="2026-08-23")[0]
    n = thematic.build_narrative(cand)
    assert n["axis"] == "thematic" and n["is_inference"] and n["mode"] == "derived"
    assert n["subject_type"] == "theme" and n["entity"] is None
    assert n["is_alert"] is False and n["threat_score"] <= 0.5
    assert "Corrente setorial" in n["narrative"]
    assert len(n["entities"]) == cand["entity_count"]
