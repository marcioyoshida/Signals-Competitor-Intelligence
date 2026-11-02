"""#309: a question that names a bank retrieves that bank's own KB documents first.

Banks' Pilar 3 boilerplate reads alike. Live 2026-11-02, an unfiltered retrieve ranked BTG's
credit-risk chunk first for a Mercado Pago question and missed Safra's own text."""
import inspect
import json
import sys
import types

from src.dashboard import agent_ask as A

FEED = {"entities": [
    {"entity": "bb", "label": "Banco do Brasil"}, {"entity": "safra", "label": "Safra"},
    {"entity": "j-safra", "label": "J. Safra"}, {"entity": "banco_bv", "label": "Banco Bv"},
    {"entity": "mercado_pago", "label": "Mercado Pago"}, {"entity": "c6", "label": "C6 Bank"},
    {"entity": "nubank", "label": "Nubank"}, {"entity": "itau", "label": "Itaú"},
    {"entity": "original", "label": "Original"}, {"entity": "btg", "label": "BTG Pactual"},
]}


def test_named_entities_is_conservative():
    cases = {
        "Qual o apetite de risco do Safra?": ["safra"],              # not j-safra (needs the "j")
        "como o safra gerencia risco?": ["safra"],                    # lowercase still scopes
        "Como o Mercado Pago gerencia o risco de crédito?": ["mercado_pago"],
        "Como o Banco do Brasil gerencia o risco de liquidez?": ["bb"],   # label phrase, not banco_bv
        "Como o BB gerencia liquidez?": ["bb"],
        "Como o Itaú Unibanco, trata o risco?": ["itau"],
        "E o BTG Pactual?": ["btg"],
        "Banco Original gerencia liquidez?": ["original"],
        "Como está o crédito no Brasil?": [],                         # a generic word scopes nothing
        "Qual a estratégia do banco?": [],
        "leia o texto original": [],                                  # a common word, not a name
        "O que mudou no mercado de pagamentos?": [],                  # "mercado" alone is not Mercado Pago
    }
    for q, want in cases.items():
        assert A.named_entities(q, FEED) == want, q


def test_named_entities_caps_and_puts_the_dashboard_scope_first():
    got = A.named_entities("Compare C6, Nubank e Safra", FEED, scope={"entity": "itau"})
    assert got[0] == "itau" and len(got) == A.KB_ENTITY_MAX


def test_common_word_ids_cover_the_registry_seed():
    """BB and XP are 2-letter ids matched only as whole words; the rest of the seed is listed."""
    from src.synth.entities import AMBIGUOUS_TOKENS
    assert AMBIGUOUS_TOKENS - {"BB", "XP", "CERC"} <= A._COMMON_WORD_IDS


def _hit(uri, text, entity=None):
    meta = {"entity": entity} if entity else {}
    return {"content": {"text": text}, "location": {"s3Location": {"uri": uri}}, "metadata": meta, "score": 0.8}


class _Client:
    def __init__(self, fail_filter=False):
        self.calls, self.fail_filter = [], fail_filter

    def retrieve(self, **kw):
        cfg = kw["retrievalConfiguration"]["vectorSearchConfiguration"]
        self.calls.append(cfg.get("filter"))
        flt = cfg.get("filter")
        if flt:
            if self.fail_filter:
                raise RuntimeError("filter unsupported")
            eid = flt["equals"]["value"]
            return {"retrievalResults": [_hit(f"s3://raw/pilar3/{eid}/a.txt", f"risco de crédito do {eid}", eid)]}
        return {"retrievalResults": [_hit("s3://raw/pilar3/btg/a.txt", "risco de crédito do btg", "btg"),
                                     _hit("s3://raw/pilar3/mercado_pago/a.txt", "risco de crédito do mercado_pago",
                                          "mercado_pago")]}


def _patch_boto(monkeypatch, client):
    monkeypatch.setenv("ONCA_KB_ID", "kb")
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda name: client))


def test_filtered_hits_come_first_and_duplicates_collapse(monkeypatch):
    c = _Client()
    _patch_boto(monkeypatch, c)
    out = A._kb_retrieve("Como o Mercado Pago gerencia o risco de crédito?", entities=["mercado_pago"])
    assert c.calls[0] == {"equals": {"key": "entity", "value": "mercado_pago"}} and c.calls[1] is None
    assert out[0]["uri"].endswith("mercado_pago/a.txt")
    assert [s["uri"] for s in out].count("s3://raw/pilar3/mercado_pago/a.txt") == 1
    assert len({s["id"] for s in out}) == len(out)                 # citation ids stay unique


def test_no_entities_means_one_unfiltered_retrieve(monkeypatch):
    c = _Client()
    _patch_boto(monkeypatch, c)
    A._kb_retrieve("risco de crédito no setor")
    assert c.calls == [None]


def test_a_filter_failure_degrades_to_the_unfiltered_search(monkeypatch):
    c = _Client(fail_filter=True)
    _patch_boto(monkeypatch, c)
    out = A._kb_retrieve("Safra liquidez", entities=["safra"])
    assert len(out) == 2 and out[0]["uri"].endswith("btg/a.txt")


def test_answer_passes_named_entities_only_to_a_retriever_that_takes_them():
    seen = {}

    def scoped(q, entities=None):
        seen["entities"] = entities
        return [{"id": "kb:0", "subject": "texto", "uri": "s3://x"}]

    def plain(q):                                   # older stubs and callers: question only
        seen["plain"] = True
        return [{"id": "kb:0", "subject": "texto", "uri": "s3://x"}]

    feed = {**FEED, "feed": []}
    conv = lambda *a, **k: "Resposta [kb:0]."
    A.answer("Como o Safra gerencia o risco de liquidez?", feed=feed, converser=conv, kb_retrieve=scoped)
    assert seen["entities"] == ["safra"]
    A.answer("Como o Safra gerencia o risco de liquidez?", feed=feed, converser=conv, kb_retrieve=plain)
    assert seen["plain"]


def test_every_production_retriever_accepts_entities():
    assert A._accepts_entities(A._kb_retrieve)
    src = inspect.getsource(A)
    assert "lambda qq, entities=None: _kb_retrieve(qq, entities=entities)" in src   # officer reads
