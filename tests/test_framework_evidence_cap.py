"""SURF-9 (#89): shared, env-overridable framework evidence cap (raised 12 -> 20)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import framework_common, porter


def test_evidence_cap_default_and_env(monkeypatch):
    monkeypatch.delenv("ONCA_FRAMEWORK_EVIDENCE_CAP", raising=False)
    assert framework_common.evidence_cap() == 20
    monkeypatch.setenv("ONCA_FRAMEWORK_EVIDENCE_CAP", "35")
    assert framework_common.evidence_cap() == 35
    monkeypatch.setenv("ONCA_FRAMEWORK_EVIDENCE_CAP", "junk")
    assert framework_common.evidence_cap() == 20  # bad value -> default


def test_collect_evidence_uses_shared_cap(monkeypatch):
    monkeypatch.delenv("ONCA_FRAMEWORK_EVIDENCE_CAP", raising=False)
    narrs = [{"id": f"n{i}", "threat_score": 0.5, "narrative": f"claim distinct number {i} about the market"}
             for i in range(40)]
    out = porter._collect_evidence_ids(narrs)
    assert len(out) == 20  # new default cap
    assert len(porter._collect_evidence_ids(narrs, max_claims=5)) == 5  # explicit override honored


# --- SURF-9 step 2: financial-context injection ----------------------------------
def test_financial_context_map_composes(monkeypatch):
    from src.ingest import bcb_fundamentals, bcb_km1, bcb_soundness
    from src.synth import financial_tone
    monkeypatch.setenv("ONCA_DIGESTS_BUCKET", "b")
    monkeypatch.setattr(bcb_fundamentals, "load_index", lambda b, s3=None: {})
    monkeypatch.setattr(bcb_fundamentals, "fundamentals_by_entity", lambda i: {"itau": {"roe_pct": 20.9, "leverage": 12.2}})
    monkeypatch.setattr(bcb_soundness, "load_index", lambda b, s3=None: {})
    monkeypatch.setattr(bcb_soundness, "soundness_by_entity", lambda i: {"itau": {"indice_basileia": 15.3, "band": "forte"}})
    monkeypatch.setattr(bcb_km1, "load_index", lambda b, s3=None: {})
    monkeypatch.setattr(bcb_km1, "km1_by_entity", lambda i: {"itau": {"lcr_pct": 202.0}})
    monkeypatch.setattr(financial_tone, "load_index", lambda b, s3=None: {})
    monkeypatch.setattr(financial_tone, "tone_by_entity", lambda i: {"itau": {"financial_tone_net": 0.27}})
    m = framework_common.financial_context_map()
    ctx = m["itau"]
    assert "ROE 20.9%" in ctx and "Basileia 15.3% (forte)" in ctx and "LCR 202.0%" in ctx and "tom financeiro +0.27" in ctx


def test_financial_context_map_empty_without_bucket(monkeypatch):
    monkeypatch.delenv("ONCA_DIGESTS_BUCKET", raising=False)
    assert framework_common.financial_context_map() == {}


def test_draft_prompt_injects_financial_context():
    ctx = "Contexto financeiro (IF.data/Pilar 3/FinBERT, inferência): ROE 20%."
    p = porter._draft_prompt("Itaú", ["banking"], [], [], financial_ctx=ctx)
    assert ctx in p
    assert ctx not in porter._draft_prompt("Itaú", ["banking"], [], [])  # off by default


# --- SURF-9 step 3: multi-pass self-critique/refine ------------------------------
def test_multipass_enabled_env(monkeypatch):
    monkeypatch.delenv("ONCA_FRAMEWORK_MULTIPASS", raising=False)
    assert framework_common.multipass_enabled() is True
    monkeypatch.setenv("ONCA_FRAMEWORK_MULTIPASS", "0")
    assert framework_common.multipass_enabled() is False


def test_refine_draft_revises_and_falls_back(monkeypatch):
    from src.synth import bedrock_llm
    monkeypatch.setattr(bedrock_llm, "converse", lambda prompt, **k: '{"forces":[]}')
    assert framework_common.refine_draft(system="s", base_prompt="p", draft='{"forces":[{"x":1}]}') == '{"forces":[]}'
    assert framework_common.refine_draft(system="s", base_prompt="p", draft="") == ""      # empty → as-is
    monkeypatch.setattr(bedrock_llm, "converse", lambda prompt, **k: None)
    assert framework_common.refine_draft(system="s", base_prompt="p", draft="D") == "D"      # None → fallback


def test_llm_draft_runs_critique_and_uses_refined(monkeypatch):
    from src.synth import bedrock_llm, porter
    calls = []

    def fake(prompt, **k):
        calls.append(prompt)
        # pass 1 = a valid force; pass 2 (critique) = drops it
        return ('{"forces":[{"force":"rivalry","intensity":"high","text":"t","confidence":0.9,"evidence":[0]}]}'
                if len(calls) == 1 else '{"forces":[]}')

    monkeypatch.setattr(porter, "converse", fake)
    monkeypatch.setattr(bedrock_llm, "converse", fake)  # refine_draft imports from bedrock_llm
    ev = [{"id": "n1", "claim": "claim about the market share", "date": "2026-09-01", "axis": None,
           "lenses": ["news"], "threat_score": 0.5}]
    out = porter.llm_draft("Itaú", ["banking"], [], ev)
    assert len(calls) == 2 and out == []  # draft + critique; refined (empty) output used
