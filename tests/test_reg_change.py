"""ADR 009 Phase A — the amending-act change parser (deterministic, sourced)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import reg_change as RC


ACT = ("A Resolução CMN 5337, de 27 de agosto de 2026, altera a Resolução CMN 5304 para "
       "ampliar o prazo de reembolso, dá nova redação ao art. 12 e revoga o art. 7º da "
       "Resolução CMN 5130. Inclui o inciso II no art. 5 e entra em vigor em 01/12/2026.")


def test_enumerates_each_change_bound_to_its_own_clause():
    ch = RC.parse_changes(ACT, self_key="res-cmn-5337")
    by_rel = {c["relation"]: c for c in ch}
    assert set(by_rel) == {"amends", "restates", "revokes", "inserts"}
    # each verb binds ONLY its own clause's article/target — not later clauses'
    assert by_rel["amends"]["targets"][0]["instrument"] == "res-cmn-5304"
    assert by_rel["amends"]["articles"] == []            # altera targets the whole resolução
    assert by_rel["restates"]["articles"] == ["art. 12"] and not by_rel["restates"]["targets"]
    assert by_rel["revokes"]["articles"] == ["art. 7o"]
    assert by_rel["revokes"]["targets"][0]["instrument"] == "res-cmn-5130"
    assert "inciso ii" in by_rel["inserts"]["articles"]


def test_numbers_are_not_truncated_undotted():
    # regression: an undotted 4-digit number must resolve fully (5304, not 530)
    ch = RC.parse_changes("altera a Resolução CMN 5304", self_key="x")
    assert ch and ch[0]["targets"][0]["instrument"] == "res-cmn-5304"


def test_past_tense_news_narrative_is_parsed():
    # our narratives are largely news-derived (preterite), not the act's present tense
    t = ("O BCB alterou a Resolução CMN nº 5.304 para ampliar o prazo e revogou o art. 7º; "
         "também deu nova redação ao art. 12.")
    rels = {c["relation"] for c in RC.parse_changes(t, self_key="res-cmn-5337")}
    assert {"amends", "revokes", "restates"} <= rels


def test_self_reference_is_not_a_target():
    ch = RC.parse_changes("A Resolução BCB 999 altera a Resolução BCB 999.", self_key="res-bcb-999")
    # the act citing itself is not an amend TARGET; with no article + no other target,
    # the change is too weak to enumerate -> dropped
    assert all("res-bcb-999" not in [t["instrument"] for t in c["targets"]] for c in ch)


def test_bare_verb_without_article_or_target_is_dropped():
    assert RC.parse_changes("O órgão altera sua rotina interna de trabalho.") == []


def test_summary_is_readable_and_capped():
    ch = RC.parse_changes(ACT, self_key="res-cmn-5337")
    s = RC.summarize_changes(ch, limit=2)
    assert "altera Resolução CMN 5304" in s
    assert s.count(";") >= 1


def test_empty_text_is_safe():
    assert RC.parse_changes("") == [] and RC.parse_changes(None) == []
    assert RC.summarize_changes([]) == ""
