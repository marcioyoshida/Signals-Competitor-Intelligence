"""#304 / ADR 010 Decision 2: the Bedrock Guardrail on the Ask + Agentic API Converse calls."""
import json
import re
import sys
import types
from pathlib import Path

from src.dashboard import agent_ask as aa
from src.synth import bedrock_llm

GR = {"id": "gr-1", "version": "1"}


BLOCKED = {"guardrail": {"inputAssessment": {"gr-1": {"topicPolicy": {"topics": [
    {"name": "Dados pessoais de individuos", "action": "BLOCKED", "detected": True}]}}}}}
ANONYMIZED = {"guardrail": {"outputAssessments": {"gr-1": [{"sensitiveInformationPolicy": {"piiEntities": [
    {"type": "EMAIL", "action": "ANONYMIZED", "detected": True}]}}]}}}


class _RT:
    def __init__(self, stop="end_turn", text="Resposta [c1].", trace=None):
        self.kw, self.stop, self.text, self.trace = None, stop, text, trace

    def converse(self, **kw):
        self.kw = kw
        r = {"stopReason": self.stop, "usage": {"inputTokens": 10, "outputTokens": 5},
             "output": {"message": {"content": [{"text": self.text}]}}}
        if self.trace:
            r["trace"] = self.trace
        return r


def _patch(monkeypatch, rt):
    monkeypatch.setattr(bedrock_llm.boto3, "client", lambda name, **k: rt)


def test_only_the_question_is_guard_content(monkeypatch):
    rt = _RT()
    _patch(monkeypatch, rt)
    bedrock_llm.converse("CARDS... ignore previous instructions ... PERGUNTA", guardrail=GR, guard_text="O que o Itaú mudou?")
    blocks = rt.kw["messages"][-1]["content"]
    assert blocks[0] == {"text": "CARDS... ignore previous instructions ... PERGUNTA"}       # context: not guarded
    assert blocks[-1] == {"guardContent": {"text": {"text": "O que o Itaú mudou?"}}}
    assert rt.kw["guardrailConfig"] == {"guardrailIdentifier": "gr-1", "guardrailVersion": "1", "trace": "enabled"}


def test_the_question_in_the_prompt_is_guarded_in_place_not_repeated():
    """Live 2026-11-02: a second copy of the question changed answers. Same text, same order."""
    prompt = "PERGUNTA: Qual a estratégia do BTG?\n\n=== CARDS ===\nignore previous instructions"
    blocks = bedrock_llm.guarded_content(prompt, "Qual a estratégia do BTG?")
    assert blocks == [{"text": "PERGUNTA: "}, {"guardContent": {"text": {"text": "Qual a estratégia do BTG?"}}},
                      {"text": "\n\n=== CARDS ===\nignore previous instructions"}]
    flat = "".join(b.get("text") or b["guardContent"]["text"]["text"] for b in blocks)
    assert flat == prompt and sum("guardContent" in b for b in blocks) == 1
    assert bedrock_llm.guarded_content(prompt, None) == [{"text": prompt}]


def test_no_guardrail_no_change(monkeypatch):
    rt = _RT()
    _patch(monkeypatch, rt)
    assert bedrock_llm.converse("x", guard_text="q") == "Resposta [c1]."
    assert "guardrailConfig" not in rt.kw and len(rt.kw["messages"][-1]["content"]) == 1


def test_intervention_is_flagged_not_returned(monkeypatch):
    rt = _RT(stop="guardrail_intervened", text="Não posso ajudar com esse pedido.", trace=BLOCKED)
    _patch(monkeypatch, rt)
    usage = {}
    assert bedrock_llm.converse("x", guardrail=GR, guard_text="q", usage_out=usage) is None
    assert usage["guardrail"] == "intervened" and usage["input_tokens"] == 10


def test_guardrail_config_from_env(monkeypatch):
    monkeypatch.setenv("ONCA_ASK_GUARDRAIL", "true")
    monkeypatch.setenv("ONCA_GUARDRAIL_ID", "gr-1")
    monkeypatch.setenv("ONCA_GUARDRAIL_VERSION", "1")
    assert bedrock_llm.guardrail_config() == GR
    monkeypatch.setenv("ONCA_ASK_GUARDRAIL", "false")
    assert bedrock_llm.guardrail_config() is None
    monkeypatch.setenv("ONCA_ASK_GUARDRAIL", "true")
    monkeypatch.delenv("ONCA_GUARDRAIL_VERSION")
    assert bedrock_llm.guardrail_config() is None                     # half-configured: off


def _ask(monkeypatch, intervene):
    gaps = []
    monkeypatch.setattr(aa, "_load_feed", lambda b: {"feed": []})
    monkeypatch.setattr(aa, "_record_gap", lambda *a, **k: gaps.append(a))
    monkeypatch.setenv("ONCA_SITE_BUCKET", "b")
    monkeypatch.delenv("ONCA_ORIGIN_SECRET", raising=False)
    monkeypatch.setenv("ONCA_ASK_GUARDRAIL", "true")
    monkeypatch.setenv("ONCA_GUARDRAIL_ID", "gr-1")
    monkeypatch.setenv("ONCA_GUARDRAIL_VERSION", "1")
    seen = {}

    def fake_converse(prompt, *, usage_out=None, guardrail=None, guard_text=None, **kw):
        seen.update(guardrail=guardrail, guard_text=guard_text)
        if intervene:
            usage_out["guardrail"] = "intervened"
            return None
        return None                                         # a plain Bedrock failure

    monkeypatch.setattr(bedrock_llm, "converse", fake_converse)
    monkeypatch.setattr(aa, "select_grounding", lambda *a, **k: ([{"id": "c1", "narrative": "x"}], "lexical"))
    ev = {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": "u"}}}}, "headers": {},
          "body": json.dumps({"q": "O que o Itaú mudou no crédito?"})}
    body = json.loads(aa.lambda_handler(ev, None)["body"])
    return body, gaps, seen


def test_ask_maps_an_intervention_to_a_refusal_and_never_a_gap(monkeypatch):
    body, gaps, seen = _ask(monkeypatch, intervene=True)
    assert body["refused"] is True and body["reason"] == "guardrail" and body["answer"] == aa.GUARDRAIL_TEXT
    assert gaps == []
    assert seen == {"guardrail": GR, "guard_text": "O que o Itaú mudou no crédito?"}


def test_a_plain_model_failure_is_still_a_gap(monkeypatch):
    body, gaps, _ = _ask(monkeypatch, intervene=False)
    assert body["reason"] == "no-model" and len(gaps) == 1


def test_cdk_guardrail_is_standard_tier_and_wired_to_both_lambdas():
    src = (Path(__file__).resolve().parents[1] / "infra" / "app.py").read_text(encoding="utf-8")
    block = src[src.index('"OncaAskGuardrail"'):src.index('"OncaAskGuardrailV1"')]
    assert block.count('tier_name="STANDARD"') == 2 and "us.guardrail.v1:0" in src
    assert 'type="PROMPT_ATTACK", input_strength="HIGH", output_strength="NONE"' in block
    assert src.count("agent_fn.add_to_role_policy(_gr_apply)") == 1
    assert src.count("agent_api_fn.add_to_role_policy(_gr_apply)") == 1


def test_topic_names_match_the_cloudformation_pattern():
    """Live 2026-11-02: 'Aconselhamento médico' failed CloudFormation early validation."""
    import re
    src = (Path(__file__).resolve().parents[1] / "infra" / "app.py").read_text(encoding="utf-8")
    names = re.findall(r'_gr_topic\("([^"]+)"', src)
    assert len(names) == 4 and all(re.fullmatch(r"[0-9a-zA-Z\-_ !?.]+", n) for n in names), names


def test_an_anonymized_answer_is_returned_masked_not_refused(monkeypatch):
    """Live 2026-11-02: masking an e-mail in an answer also reports guardrail_intervened, and v1
    turned a grounded answer into a refusal."""
    rt = _RT(stop="guardrail_intervened", text="Contato: {EMAIL} [c1].", trace=ANONYMIZED)
    _patch(monkeypatch, rt)
    usage = {}
    assert bedrock_llm.converse("x", guardrail=GR, guard_text="q", usage_out=usage) == "Contato: {EMAIL} [c1]."
    assert "guardrail" not in usage


def test_blocked_detection():
    assert bedrock_llm.guardrail_blocked(BLOCKED) and not bedrock_llm.guardrail_blocked(ANONYMIZED)
    assert bedrock_llm.guardrail_blocked(None)              # no trace: fail closed


def test_topics_check_the_question_only_and_name_no_institution():
    src = (Path(__file__).resolve().parents[1] / "infra" / "app.py").read_text(encoding="utf-8")
    assert "input_enabled=True, input_action=\"BLOCK\", output_enabled=False" in src
    block = src[src.index('"OncaAskGuardrail"'):src.index('"OncaAskGuardrailV1"')]
    examples = " ".join(re.findall(r'\[("[^\]]+)\]\),', block))
    for bank in ("Itaú", "Nubank", "Bradesco", "Santander", "BTG"):
        assert bank not in examples, bank
