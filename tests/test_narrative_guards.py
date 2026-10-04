"""Deterministic backstops on LLM briefings (live failures of 2026-09-20..10-03)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import narrative_guards as ng
from src.synth import synthesize

NUBANK_NEWS = {"_lens": "news", "id": "news:9a3d", "source": "News", "date": "2026-09-20",
               "title": "Nubank assume controle da operação do Nubank Parque e impacta o Palmeiras"}
MARKET = {"_lens": "market", "id": "market:NUBANK", "source": "BCB-IFDATA", "institution": "NUBANK"}
SEC_6K = {"_lens": "sec", "id": "sec:1", "source": "SEC", "form": "6-K", "filed": "2026-08-31",
          "title": "Nu Holdings Ltd. – Investor Day on December 8, 2026"}
BCB_AUTH = {"_lens": "regulatory", "id": "bcb-auth:1", "source": "BCB-Autorizacoes",
            "subject": "Autorização para funcionamento de instituição de pagamento"}


def test_regulator_invented_from_a_dataset_row_is_dropped():
    text = ("O Banco Central assume o controle da operação do Nubank Parque, impactando o Palmeiras. "
            "A Nu Holdings Ltd. informou que realizará o Investor Day em 8 de dezembro de 2026.")
    out, dropped = ng.drop_unsupported_regulator_claims(text, [NUBANK_NEWS, MARKET, SEC_6K])
    assert dropped == ["O Banco Central assume o controle da operação do Nubank Parque, impactando o Palmeiras."]
    assert out.startswith("A Nu Holdings Ltd.")


def test_regulator_kept_when_a_source_is_its_own_act_or_names_it():
    text = "O Banco Central autorizou a instituição a funcionar como instituição de pagamento."
    assert ng.drop_unsupported_regulator_claims(text, [BCB_AUTH])[1] == []
    news = dict(NUBANK_NEWS, title="CVM multa gestora por falhas em fundos")
    assert ng.drop_unsupported_regulator_claims("A CVM multou a gestora em R$ 2 milhões.", [news])[1] == []


def test_regulator_as_object_or_passive_is_not_an_action():
    for s in ("No mercado, o Nubank continua a ser monitorado pelo Banco Central do Brasil.",
              "A Nu Holdings informou à CVM um fato relevante.",
              "O presidente do Banco Central comentou a Selic."):
        assert ng.acting_regulators(s) == set(), s
    assert ng.acting_regulators("O Banco Central do Brasil decretou a liquidação do banco.") == {"bcb"}
    assert ng.acting_regulators("A CVM, em nota, determinou a suspensão da oferta.") == {"cvm"}


def test_past_date_in_future_tense_is_dropped_future_date_kept():
    text = ("A Nu Holdings Ltd. informou que, em 10 de setembro de 2026, a empresa divulgará um relatório trimestral. "
            "A empresa realizará o Investor Day em 8 de dezembro de 2026. "
            "Em 10 de setembro de 2026 a empresa divulgou o relatório.")
    out, dropped = ng.drop_stale_future(text, "2026-09-27")
    assert len(dropped) == 1 and "divulgará" in dropped[0]
    assert "8 de dezembro" in out and "divulgou" in out
    # a URL's digits are not a date
    assert ng.drop_stale_future("Fará o pagamento (https://x.gov.br/2026-01-02/a).", "2026-09-27")[1] == []


def test_sec_is_never_called_the_cvm():
    out, n = ng.fix_sec_cvm("comunicado à Comissão de Valores Mobiliários (SEC) e à CVM (SEC).")
    assert n == 2 and "Comissão de Valores Mobiliários (SEC)" not in out and ng.SEC_NAME in out


def test_synthesize_applies_guards_and_records_them(monkeypatch):
    llm = ("O Banco Central assume o controle da operação do Nubank Parque https://news.google.com/a. "
           "A Nu Holdings informou à Comissão de Valores Mobiliários (SEC) o Investor Day em 8 de dezembro de 2026 "
           "https://www.sec.gov/x.")
    monkeypatch.setattr(synthesize.bedrock_llm, "converse", lambda *a, **k: llm)
    monkeypatch.setattr(synthesize, "run_date_today", lambda: "2026-09-20")
    cand = {"id": "cand-ent-nubank", "kind": "entity_fusion", "entity": "nubank", "lenses": ["news", "sec"],
            "sources": [dict(NUBANK_NEWS, url="https://news.google.com/a"), dict(SEC_6K, url="https://www.sec.gov/x"),
                        MARKET]}
    out = synthesize.synthesize_candidate(cand, use_llm=True)
    assert "Banco Central" not in out["narrative"] and ng.SEC_NAME in out["narrative"]
    assert out["mode"] == "llm" and len(out["guards"]["dropped_regulator"]) == 1 and out["guards"]["sec_fixed"] == 1
