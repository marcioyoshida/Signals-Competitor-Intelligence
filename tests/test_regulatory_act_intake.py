"""#198: sector-level official acts (no company) become radar cards via the act intake."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import lambda_port  # noqa: E402
from src.synth import regulatory as r  # noqa: E402

URL = "https://www.in.gov.br/web/dou/-/"


def _act(i, title, date, industries, severity="high", text="", **kw):
    return {"id": f"dou:{i}", "source": "DOU", "kind": "regulatory", "title": title, "date": date,
            "url": URL + i, "industries": industries, "severity": severity,
            "text": f"{title} {text}", **kw}


SUSEP_96 = _act("resolucao-susep-n-96-de-14-de-setembro-de-2026-732448943",
                "RESOLUÇÃO SUSEP Nº 96, DE 14 DE SETEMBRO DE 2026", "2026-09-17", ["insurance"],
                text="Dispõe sobre a reversão do saldo de provisões originado de prêmios pagos. "
                     "Altera a Resolução CNSP nº 432.")
PREVIC_728 = _act("portaria-previc-n-728-de-16-de-setembro-de-2026-732448864",
                  "Portaria Previc Nº 728, DE 16 DE setembro DE 2026", "2026-09-17", ["closed-pension"],
                  text="Dispõe sobre a avaliação e o monitoramento de riscos ambientais.")
LC_237 = _act("lei-complementar-n-237-de-15-de-setembro-de-2026-731891987",
              "LEI COMPLEMENTAR Nº 237, DE 15 DE SETEMBRO DE 2026", "2026-09-16", ["insurance"],
              text="Altera a Lei Complementar nº 126, de 15 de janeiro de 2007 (resseguro).")
RN_ANS_679 = _act("resolucao-normativa-ans-n-679-de-10-de-setembro-de-2026-731388381",
                  "RESOLUÇÃO NORMATIVA ANS Nº 679, DE 10 DE SETEMBRO DE 2026", "2026-09-11", ["insurance"])


def test_new_regulators_and_laws_are_own_instruments():
    assert r.own_instrument(SUSEP_96["title"]) == "res-susep-96"
    assert r.own_instrument(PREVIC_728["title"]) == "port-previc-728"
    assert r.own_instrument(LC_237["title"]) == "lc-237"
    assert r.own_instrument("LEI Nº 15.506, DE 16 DE SETEMBRO DE 2026") == "lei-15506"
    assert r.own_instrument(RN_ANS_679["title"]) == "rn-ans-679"
    # ANEEL issues "Resolução Normativa nº …" too — not an ANS act
    assert r.own_instrument("RESOLUÇÃO NORMATIVA Nº 1.100, DE 2 DE SETEMBRO DE 2026") is None


def test_laws_cited_in_a_narrative_are_not_threaded():
    n = {"narrative": "O fundo segue a Lei nº 8.668 e a Lei Complementar nº 109.", "run_date": "2026-09-20"}
    assert r.instruments_in(n) == {}


def test_intake_filters_low_unclassified_and_unnamed_acts():
    digest = {"dou": {"items": [SUSEP_96, _act("x", "PORTARIA Nº 5, DE 1 DE SETEMBRO DE 2026", "2026-09-02",
                                               ["insurance"])],
                      "context": [_act("y", "RESOLUÇÃO SUSEP Nº 90, DE 1 DE SETEMBRO", "2026-09-02",
                                       ["insurance"], severity="low"),
                                  _act("z", "RESOLUÇÃO SUSEP Nº 91, DE 1 DE SETEMBRO", "2026-09-02", [])]},
              "official_acts": {"acts": [LC_237]},
              "news": {"items": [{**PREVIC_728, "kind": "regulatory"}]}}
    keys = sorted(a["instrument_key"] for a in r.acts_from_digest(digest))
    assert keys == ["lc-237", "res-susep-96"]


def test_act_intake_cards_scope_to_the_acts_own_industries_and_cite_it():
    store = r.merge_acts({}, r.acts_from_digest({"official_acts": {"acts": [SUSEP_96, PREVIC_728, LC_237]}}),
                         as_of="2026-09-27", window=21)
    cands = {c["instrument"]: c for c in r.nominate(r.act_narratives(store), as_of="2026-09-27")}
    # only each act's OWN instrument — SUSEP 96 cites Res. CNSP 432, which gets no card from it
    assert set(cands) == {"res-susep-96", "port-previc-728", "lc-237"}
    assert cands["port-previc-728"]["industries"] == ["closed-pension"]
    assert cands["port-previc-728"]["industries_source"] == "federal_acts"
    card = r.build_narrative(cands["res-susep-96"])
    assert card["affected_industries"] == ["insurance"] and card["entity"] is None
    assert card["citations"][0]["url"] == SUSEP_96["url"]
    assert "Resolução SUSEP 96" in card["narrative"] and "reversão do saldo" in card["narrative"]


def test_act_store_keeps_first_seen_and_drops_stale_acts():
    s1 = r.merge_acts({}, [dict(SUSEP_96, instrument_key="res-susep-96")], as_of="2026-09-20", window=21)
    s2 = r.merge_acts(s1, [dict(SUSEP_96, instrument_key="res-susep-96")], as_of="2026-09-27", window=21)
    assert s2["acts"][SUSEP_96["id"]]["first_seen"] == "2026-09-20"
    s3 = r.merge_acts(s2, [], as_of="2026-10-20", window=21)
    assert s3["acts"] == {}


def test_ingest_emits_every_classified_act_uncapped():
    recs = [dict(SUSEP_96, id=f"dou:{i}") for i in range(25)] + [
        _act("low", "RESOLUÇÃO SUSEP Nº 1", "2026-09-01", ["insurance"], severity="low"),
        _act("none", "RESOLUÇÃO SUSEP Nº 2", "2026-09-01", [])]
    out = lambda_port._official_acts(recs)
    assert out["count"] == 25 and len(out["acts"]) == 25
    assert set(out["acts"][0]) >= {"id", "title", "url", "industries", "severity", "text"}
