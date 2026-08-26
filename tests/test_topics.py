import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import topics as tp


def test_topics_of_from_lenses():
    assert tp.topics_of({"lenses": ["pix"]}) == ["pagamentos"]
    assert tp.topics_of({"lenses": ["regulatory", "dou"]}) == ["regulacao"]
    # multi-topic card, returned in TOPICS order
    got = tp.topics_of({"lenses": ["pix", "regulatory", "funds"]})
    assert got == ["regulacao", "pagamentos", "fundos"]


def test_topics_of_from_axis():
    assert tp.topics_of({"axis": "comparative"}) == ["concorrencia"]
    assert tp.topics_of({"axis": "predictive"}) == ["analise"]


def test_generic_news_falls_through_to_geral():
    # the ubiquitous 'news' lens is intentionally not a topic
    assert tp.topics_of({"lenses": ["news"]}) == ["geral"]
    assert tp.topics_of({}) == ["geral"]


def test_axis_and_lens_combine_deduped():
    # regulatory axis + regulatory lens both map to regulacao -> once
    assert tp.topics_of({"axis": "regulatory", "lenses": ["dou"]}) == ["regulacao"]


def test_question_topics_detects_intent():
    assert tp.question_topics("quais mudanças regulatórias do BACEN afetam o PIX?") == {"regulacao", "pagamentos"}
    assert tp.question_topics("qual o market share do Nubank?") == {"concorrencia"}
    assert "credito" in tp.question_topics("como está a inadimplência do crédito?")
    assert tp.question_topics("me fale algo genérico") == set()


def test_topic_options_only_present_ordered():
    cards = [{"topics": ["pagamentos"]}, {"topics": ["regulacao", "pagamentos"]},
             {"topics": ["geral"]}]
    opts = tp.topic_options(cards)
    slugs = [o["slug"] for o in opts]
    assert slugs == ["regulacao", "pagamentos", "geral"]  # TOPICS order, present only
    assert opts[0]["label"] == "Regulação"
