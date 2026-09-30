"""#159 kill-rule re-measure: matching a radar event to Onça's own narratives."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import cpo_radar_remeasure as rm  # noqa: E402

INTER = {"id": "inter", "name": "Inter", "aliases": ["Banco Inter"], "onca_entities": ["inter"]}
PICPAY = {"id": "picpay", "name": "PicPay", "aliases": [], "onca_entities": ["picpay"]}


def _n(date, key, doc):
    text = rm.prose(json.dumps(doc))
    return {"date": date, "key": key, "text": text, "words": rm.words(text)}


def _ev(**kw):
    ev = {"source": "youtube", "product": "inter", "date": "2026-09-27", "title": "", "reason": ""}
    ev.update(kw)
    return ev


def test_earlier_onca_coverage_counts_as_surfaced():
    # live: Onça's narrative had Inter's Priority Pass change 5 days before the creators' videos
    n = _n("2026-09-22", "narratives/2026-09-22/cand-ent-inter.json",
           {"narrative": "O Banco Inter anunciou a limitação do uso do Priority Pass apenas para salas VIP."})
    ev = _ev(title="fim dos benefícios Priority Pass", reason="fim dos benefícios Priority Pass para clientes Prime")
    assert rm.match(ev, INTER, [n]) == n["key"]
    # …but not a month earlier
    old = dict(n, date="2026-08-20")
    assert rm.match(ev, INTER, [old]) is None


def test_metadata_brand_substrings_and_generic_words_never_match():
    ev = _ev(title="taxa de juros do Pix parcelado", reason="Explica taxa de juros do Pix parcelado")
    # the words sit in lens names / source ids, and "inter" only inside "internacional"
    meta = _n("2026-09-25", "narratives/2026-09-25/thematic-betting.json",
              {"lenses": ["juros", "pix"], "citations": [{"id": "juros:cartao-parcelado"}],
               "narrative": "Expansão internacional das bets."})
    assert rm.match(ev, INTER, [meta]) is None
    pp = {"source": "youtube", "product": "picpay", "date": "2026-09-25", "title": "Central de Cashback",
          "reason": "Nova função que transforma cashback em limite no cartão de crédito"}
    generic = _n("2026-09-29", "narratives/2026-09-29/cand-ent-picpay.json",
                 {"narrative": "O PicPay ampliou o limite de crédito para clientes."})
    assert rm.match(pp, PICPAY, [generic]) is None


def test_app_store_alerts_are_always_left_to_the_human():
    ev = _ev(source="appstore", title="Pico de avaliações negativas no app iOS",
             reason="54 relatos de falha no app iOS")
    n = _n("2026-09-20", "narratives/2026-09-20/comparative-inter.json",
           {"narrative": "O Banco Inter recebeu avaliações negativas de falha no app iOS."})
    assert rm.match(ev, INTER, [n]) is None


def test_noise_the_current_guards_drop_is_excluded():
    assert rm.is_noise(_ev(title="como pedir o cartão de crédito Global", reason="Novo recurso"))
    assert rm.is_noise(_ev(title="novos valores de dividendos", reason="Anúncio de dividendos"))
    assert not rm.is_noise(_ev(title="fim dos benefícios Priority Pass", reason="fim do benefício"))
    assert not rm.is_noise(_ev(source="appstore", title="como", reason=""))


def test_worksheet_splits_original_and_added_products():
    subjects = {"inter": INTER, "itau": {"id": "itau", "name": "Itaú", "aliases": [], "onca_entities": ["itau"]}}
    evs = [_ev(title="fim dos benefícios Priority Pass", reason="Priority Pass", url="https://y/1"),
           _ev(product="itau", title="Encerra cartão histórico", reason="encerra cartão", url="https://y/2")]
    md, summary = rm.worksheet(evs, subjects, [], "2026-09-01", "2026-09-30")
    assert summary["inter"] == {"events": 1, "in_onca": 0, "cand_youtube": 1, "cand_appstore": 0, "original": True}
    assert summary["itau"]["original"] is False
    assert md.index("Original 5") < md.index("Inter |") < md.index("Added 2026-09-27") < md.index("Itaú |")
    assert "- [ ] 2026-09-27" in md and "https://y/1" in md
