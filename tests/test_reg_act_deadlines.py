"""#378: CRO compliance calendar — deadlines from the act's own text (regdocs)."""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import feed_builder  # noqa: E402
from src.synth import executive, regulatory  # noqa: E402
from tests.test_regulatory import _card  # noqa: E402

AS_OF = "2026-11-10"
ACT = ("RESOLUÇÃO BCB Nº 584, DE 2 DE OUTUBRO DE 2026. Dispõe sobre … Art. 12. Esta "
       "Resolução entra em vigor em 1º de janeiro de 2027, exceto o art. 5º, que produz "
       "efeitos a partir de 1º de julho de 2027.")


def test_act_deadline_reads_ordinal_first_of_month_and_takes_the_nearest():
    ad = regulatory.act_deadline(ACT, AS_OF)
    assert ad["date"] == "2027-01-01"
    assert "entra em vigor" in ad["cue"]


def test_act_deadline_ignores_past_beyond_horizon_and_publication_day():
    assert regulatory.act_deadline("Esta Resolução entra em vigor na data de sua publicação.", AS_OF) is None
    # the act's own date line (a past date, no cue) is not a deadline
    assert regulatory.act_deadline("RESOLUÇÃO Nº 1, DE 2 DE OUTUBRO DE 2026.", AS_OF) is None
    assert regulatory.act_deadline("entra em vigor em 1º de outubro de 2026.", AS_OF) is None
    assert regulatory.act_deadline("entra em vigor em 1º de janeiro de 2029.", AS_OF) is None  # > 365 d
    assert regulatory.act_deadline("produz efeitos a partir de 15/03/2027.", AS_OF)["date"] == "2027-03-15"


def test_narrative_regex_ordinal_and_market_prazo():
    assert regulatory._future_deadline("A norma entra em vigor em 1º de dezembro de 2026.", AS_OF, 365) \
        == "2026-12-01"
    # "médio/longo prazo" is market talk, not a deadline cue
    assert regulatory._future_deadline("Visão de longo prazo; resultado em 10 de dezembro de 2026.",
                                       AS_OF, 365) is None


def _bcb584():
    return [_card("A Resolução BCB 584 traz novas regras de crédito.", date="2026-11-08")]


def test_act_deadline_wins_and_is_labelled_on_the_card():
    act = {"res-bcb-584": {"date": "2027-01-01", "cue": "entra em vigor em 1o de janeiro de 2027",
                           "source": "BCB"}}
    plain = regulatory.nominate(_bcb584(), as_of=AS_OF)
    assert plain[0]["deadline"] is None and plain[0]["deadline_source"] is None
    c = regulatory.nominate(_bcb584(), as_of=AS_OF, act_deadlines=act)[0]
    assert c["deadline"] == "2027-01-01" and c["days_to_deadline"] == 52
    assert c["deadline_source"] == "ato" and c["signature"].endswith("|2027-01-01")
    n = regulatory.build_narrative(c)
    assert n["deadline_source"] == "ato" and "data do texto do próprio ato" in n["narrative"]


def test_narrative_only_deadline_says_confirm_in_the_act():
    c = regulatory.nominate([_card("Resolução BCB 584 entra em vigor em 1º de dezembro de 2026.",
                                   date="2026-11-08")], as_of=AS_OF)[0]
    assert c["deadline_source"] == "narrativa"
    assert "confirmar no ato" in regulatory.build_narrative(c)["narrative"]


def test_lifecycle_carries_act_deadline():
    narrs = [_card("Consulta pública sobre a Resolução BCB 584.", date="2026-10-01"),
             _card("Publicada a Resolução BCB 584.", date="2026-11-08")]
    act = {"res-bcb-584": {"date": "2027-01-01", "cue": "x", "source": "BCB"}}
    lcs = regulatory.build_lifecycles(narrs, as_of=AS_OF, window=90, act_deadlines=act)
    lc = lcs["res-bcb-584"]
    assert lc["deadline"] == "2027-01-01" and lc["deadline_source"] == "ato"
    card = regulatory.build_lifecycle_card(lc)
    assert card["deadline_source"] == "ato" and "data do texto do próprio ato" in card["narrative"]


class _S3:
    def __init__(self, docs):
        self.docs = docs

    def get_object(self, Bucket, Key):
        if Key not in self.docs:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.docs[Key].encode())}


def test_act_deadlines_from_index_reads_latest_version():
    index = {"res-bcb-584": {"versions": [{"key": "regdocs/res-bcb-584/old.txt", "source": "dou"},
                                          {"key": "regdocs/res-bcb-584/new.txt", "source": "bcb"}]},
             "in-bcb-1": {"versions": [{"key": "regdocs/in-bcb-1/a.txt"}]},
             "gone": {"versions": [{"key": "regdocs/gone/missing.txt"}]}}
    s3 = _S3({"regdocs/res-bcb-584/old.txt": "entra em vigor em 1º de fevereiro de 2027.",
              "regdocs/res-bcb-584/new.txt": ACT,
              "regdocs/in-bcb-1/a.txt": "Esta Instrução entra em vigor na data de sua publicação."})
    out = regulatory.act_deadlines_from_index(index, "b", s3, as_of=AS_OF)
    assert out == {"res-bcb-584": {"date": "2027-01-01", "cue": out["res-bcb-584"]["cue"], "source": "BCB"}}


def test_feed_recounts_days_and_calendar_drops_passed_deadlines():
    assert feed_builder._days_to(None, 5) == 5
    assert feed_builder._days_to("not-a-date", 5) == 5
    import datetime as dt
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    assert feed_builder._days_to(tomorrow, 99) == 1
    item = feed_builder._project_item({"id": "r", "deadline": tomorrow, "days_to_deadline": 40,
                                       "deadline_source": "ato"})
    assert item["days_to_deadline"] == 1 and item["deadline_source"] == "ato"
    row = executive._reg_row({"id": "r", "deadline": tomorrow, "days_to_deadline": 1,
                              "deadline_source": "ato"})
    assert row["deadline_source"] == "ato" and row["deadline"] == tomorrow


def test_cro_calendar_lists_only_upcoming_deadlines():
    from tests.test_executive import _feed
    feed = _feed()
    reg = next(c for c in feed["feed"] if c.get("kind") == "regulatory_lifecycle")
    feed["feed"] += [{**reg, "id": "due", "deadline": "2027-01-01", "days_to_deadline": 52,
                      "deadline_source": "ato"},
                     {**reg, "id": "passed", "deadline": "2026-10-01", "days_to_deadline": -40}]
    cro = executive.build_executive(feed)["cro"]
    ids = [r["id"] for r in cro["panels"]["deadlines"]]
    assert "due" in ids and "passed" not in ids
    assert next(r for r in cro["panels"]["deadlines"] if r["id"] == "due")["deadline_source"] == "ato"
