"""ADR 009 §3 — the bounded LLM change-record (grounded facts, rated inference)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import reg_change_record as RCR


def _fake_converse(text):
    return lambda *a, **k: text


_GOOD = json.dumps({
    "change": "Amplia o prazo de reembolso.",
    "affected_surfaces": ["base de clientes", "sistemas gerenciais", "core DICT", "x", "y", "z"],
    "impact": "Ajustar sistemas e comunicar clientes.",
    "difficulty_score": 0.8,
    "difficulty_drivers": ["migração de dados", "prazo curto"],
    "action_required": "Planejar release até a data.",
    # the model tries to sneak these — must be IGNORED
    "blast_radius": {"n_entities": 999},
    "affected_industries": ["crypto"],
})


def test_grounded_facts_come_from_caller_not_the_model():
    rec = RCR.build_change_record(
        label="Resolução CMN 5304", domain="Pagamentos / PIX",
        industries=["acquiring", "fintech", "banking"], n_entities=14,
        changes=[{"verb": "altera", "targets": [], "articles": ["art. 5"]}],
        effective_date="2026-12-01", source_url="https://in.gov.br/x",
        converse_fn=_fake_converse(_GOOD))
    assert rec["is_inference"] is True and rec["mode"] == "derived"
    # n_entities + industries are OURS, not the model's 999 / crypto
    assert rec["blast_radius"]["n_entities"] == 14 and rec["affected_industries"] == ["acquiring", "fintech", "banking"]
    assert rec["blast_radius"]["band"] == "market"           # 14 -> market
    assert rec["effective_date"] == "2026-12-01"
    # difficulty rated by the model, band derived from the score
    assert rec["difficulty"]["score"] == 0.8 and rec["difficulty"]["band"] == "high"
    assert len(rec["affected_surfaces"]) <= 4                # capped


def test_blast_band_scales_with_entity_count():
    def band(n):
        r = RCR.build_change_record(label="x", domain="d", industries=["banking"], n_entities=n,
                                    changes=[{"verb": "altera"}], converse_fn=_fake_converse(_GOOD))
        return r["blast_radius"]["band"]
    assert band(1) == "narrow" and band(6) == "sector" and band(30) == "market"


def test_none_when_llm_unavailable_or_unparseable():
    assert RCR.build_change_record(label="x", domain="d", industries=["banking"], n_entities=1,
                                   changes=[{"verb": "altera"}], converse_fn=_fake_converse(None)) is None
    assert RCR.build_change_record(label="x", domain="d", industries=["banking"], n_entities=1,
                                   changes=[{"verb": "altera"}], converse_fn=_fake_converse("no json here")) is None


def test_enrich_lifecycles_only_records_real_changes_and_is_bounded():
    lifecycles = {
        "res-cmn-5304": {"instrument": "res-cmn-5304", "label": "Resolução CMN 5304",
                         "domain": "Câmbio & mercado aberto", "deadline": "2026-12-01",
                         "timeline": [{"summary": "altera a Resolução CMN 5130 e revoga o art. 7º"}]},
        "res-cmn-9999": {"instrument": "res-cmn-9999", "label": "Resolução CMN 9999",
                         "domain": "Setor financeiro",
                         "timeline": [{"summary": "dispõe sobre um tema novo sem alterar nada"}]},
    }
    n = RCR.enrich_lifecycles(lifecycles, industry_counts={"banking": 10, "investment-banking": 4},
                              converse_fn=_fake_converse(_GOOD), max_records=20)
    assert n == 1                                             # only the amending one
    assert "change_record" in lifecycles["res-cmn-5304"]
    assert "change_record" not in lifecycles["res-cmn-9999"]
    # n_entities = banking(10)+investment-banking(4) for the Câmbio domain
    assert lifecycles["res-cmn-5304"]["change_record"]["blast_radius"]["n_entities"] == 14
