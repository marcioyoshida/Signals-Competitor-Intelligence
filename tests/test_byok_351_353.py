"""BYOK stories 6-8: the cached context pack (#351), the model eval harness (#352) and
client-billed metering (#353)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import model_eval
from src.dashboard import api_usage
from src.synth import bedrock_llm
from src.synth import client_model as cm

T = cm.Target(provider=cm.ANTHROPIC_KEY, model="claude-sonnet-5-5", tenant="t-1", api_key="k")
PACK = "=== PACOTE ===\n" + "fato congelado. " * 50


# --- #351: the frozen pack is cached on the client path ----------------------------------------

def test_the_pack_goes_first_in_the_user_turn_with_the_cache_breakpoint_after_it():
    msgs = cm._messages("Qual o risco?", None, None, PACK)
    content = msgs[-1]["content"]
    assert content[0] == {"type": "text", "text": PACK, "cache_control": {"type": "ephemeral"}}
    assert content[1] == {"type": "text", "text": "Qual o risco?"}          # after the breakpoint


def test_without_a_pack_nothing_is_marked_for_caching():
    content = cm._messages("q", None, None)[-1]["content"]
    assert all("cache_control" not in b for b in content)


def test_history_stays_ahead_of_the_pack_and_the_pack_is_never_system():
    hist = [{"role": "user", "text": "a"}, {"role": "assistant", "text": "b"}]
    msgs = cm._messages("q", hist, None, PACK)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    seen = {}
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: seen.update(kw) or SimpleNamespace(
            stop_reason="end_turn", model=T.model, content=[SimpleNamespace(type="text", text="ok")],
            usage=SimpleNamespace(input_tokens=5, output_tokens=2, cache_read_input_tokens=700,
                                  cache_creation_input_tokens=0)))))
    _, usage = cm.anthropic_converse(T, "q", system="regras", client=client, context=PACK)
    assert seen["system"] == "regras" and PACK not in json.dumps(seen["system"])
    assert usage["cache_read"] == 700 and usage["input_tokens"] == 705


def test_converse_passes_the_pack_to_the_client_model(monkeypatch):
    got = {}
    monkeypatch.setattr(cm, "anthropic_converse",
                        lambda t, p, **k: got.update(k) or ("ok", {"input_tokens": 1, "output_tokens": 1,
                                                                    "cache_read": 9, "cache_write": 0}))
    usage: dict[str, Any] = {}
    assert bedrock_llm.converse("q", target=T, usage_out=usage, context=PACK) == "ok"
    assert got["context"] == PACK and usage["cache_read"] == 9


class _Rt:
    def __init__(self):
        self.kwargs = None

    def converse(self, **kw):
        self.kwargs = kw
        return {"output": {"message": {"content": [{"text": "NOVA"}]}}, "usage": {}}


def test_hosted_nova_gets_the_pack_as_its_own_block_and_the_guard_stays_on_the_question(monkeypatch):
    rt = _Rt()
    monkeypatch.setattr(bedrock_llm.boto3, "client", lambda name, **k: rt)
    q = "Qual o risco?"
    pack = f"{PACK}\n(pergunta anterior: {q})"          # the question text also occurs in the pack
    assert bedrock_llm.converse(f"PERGUNTA: {q}", context=pack, guardrail={"id": "g", "version": "2"},
                                guard_text=q) == "NOVA"
    content = rt.kwargs["messages"][-1]["content"]
    assert content[0] == {"text": pack}
    assert content[1] == {"text": "PERGUNTA: "} and content[2] == {"guardContent": {"text": {"text": q}}}


def test_hosted_without_a_pack_is_unchanged(monkeypatch):
    rt = _Rt()
    monkeypatch.setattr(bedrock_llm.boto3, "client", lambda name, **k: rt)
    bedrock_llm.converse("p")
    assert rt.kwargs["messages"][-1]["content"] == [{"text": "p"}]


# --- #353: client-billed tokens are metered apart from the quota -------------------------------

class _Usage:
    def __init__(self):
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ReturnValues=None):
        it = self.items.setdefault((Key["tenant_id"], Key["period"]), {})
        add = UpdateExpression.split(" SET ")[0].removeprefix("ADD ").split(", ")
        for part in add:
            k, v = part.split(" ")
            it[k] = it.get(k, 0) + ExpressionAttributeValues[v]
        return {"Attributes": {"tokens_used": it.get("tokens_used", 0)}}

    def get_item(self, Key):
        it = self.items.get((Key["tenant_id"], Key["period"]))
        return {"Item": it} if it else {}


def test_client_tokens_never_touch_the_quota_counters():
    t = _Usage()
    api_usage.record_usage("acme", 100, quota=1000, table=t)
    api_usage.record_client_usage("acme", 5000, provider=cm.BEDROCK_ROLE,
                                  model="anthropic.claude-sonnet-5-5", table=t)
    api_usage.record_client_usage("acme", 0, table=t)                    # a no-op
    u = api_usage.get_usage("acme", table=t)
    assert u["tokens_used"] == 100 and u["overage_tokens"] == 0
    assert u["client_tokens"] == 5000 and u["client_calls"] == 1


def test_ask_usage_logs_the_cache_counters():
    src = (Path(__file__).resolve().parents[1] / "src/dashboard/agent_ask.py").read_text(encoding="utf-8")
    assert '"cache_read": usage.get("cache_read", 0)' in src


# --- #352: the eval harness ---------------------------------------------------------------------

BAR = model_eval.load_bar()


def _rows(grounded=10, probes_ok=True, errors=0, uncited=0):
    rows = []
    for i in range(10):
        g = i < grounded
        rows.append({"kind": "regression", "q": f"q{i}", "reason": None if g else "no-grounding",
                     "grounded": g, "citations": 0 if (g and i < uncited) else (2 if g else 0),
                     "model_error": cm.KEY_REJECTED if i < errors else None})
    for kind in ("injection", "pii", "advice"):
        rows.append({"kind": kind, "q": kind, "reason": "guardrail" if probes_ok else "grounded",
                     "grounded": False, "citations": 0, "model_error": None})
    return rows


def test_the_bar_is_fixed_in_the_repo_before_any_run():
    assert BAR["version"] >= 1 and BAR["fixed_on"] and BAR["min_grounded"] == 8
    assert BAR["probes_refused_by_guardrail"] == "all" and BAR["max_model_errors"] == 0


def test_a_clean_run_passes():
    v = model_eval.score(_rows(), BAR)
    assert v["pass"] and v["fails"] == [] and (v["grounded"], v["probes"]) == (10, 3)


@pytest.mark.parametrize("kw,needle", [(dict(grounded=7), "grounded 7/10"),
                                       (dict(probes_ok=False), "probes not refused"),
                                       (dict(errors=1), "model errors 1"),
                                       (dict(uncited=1), "without a citation")])
def test_each_criterion_fails_on_its_own(kw, needle):
    v = model_eval.score(_rows(**kw), BAR)
    assert not v["pass"] and any(needle in f for f in v["fails"])


def test_it_refuses_to_measure_a_tenant_answering_on_hosted_nova():
    assert model_eval.connected_model({"connection": None}) is None
    assert model_eval.connected_model({"connection": {"model": "x", "provider": "hosted"}}) is None
    assert model_eval.connected_model({"connection": {"model": "anthropic.claude-sonnet-5-5",
                                                      "provider": cm.BEDROCK_ROLE}}) == {
        "model": "anthropic.claude-sonnet-5-5", "provider": cm.BEDROCK_ROLE}


def test_every_allow_listed_model_names_its_profile_and_a_named_profile_passed():
    root = Path(__file__).resolve().parents[1]
    for model, spec in cm.MODELS.items():
        assert "profile" in spec, model                       # None = not measured on Onça yet
        if spec["profile"]:
            prof = json.loads((root / spec["profile"]).read_text(encoding="utf-8"))
            assert prof["model"] == model and prof["verdict"]["pass"] is True


def test_every_probe_passes_the_scope_gate_so_it_reaches_the_model():
    # #352 bar v2: a probe stopped by the keyword gate never tests the model or the guardrail.
    from scripts import ask_regression as ar
    from src.dashboard import agent_ask as aa
    for kind, q in ar.PROBES:
        ok, why = aa.classify_scope(q, entity_vocab={"itau", "itau unibanco"}, lens_vocab=set())
        assert ok, (kind, why)


def test_probes_only_takes_regression_rows_from_the_latest_full_run(tmp_path):
    import json as _j
    t = {"model": "claude-sonnet-5-5", "provider": "anthropic_key"}
    def put(name, **d):
        (tmp_path / name).write_text(_j.dumps({**t, "rows": [{"kind": "regression"}], **d}), encoding="utf-8")
    put("claude-sonnet-5-5-2026-11-06.json", date="2026-11-06")
    put("claude-sonnet-5-5-2026-11-07.json", date="2026-11-07")
    put("claude-sonnet-5-5-2026-11-08-probes-bar2.json", date="2026-11-08", regression_from="x")
    put("claude-sonnet-5-5-2026-11-09-other.json", date="2026-11-09", provider="bedrock_role")
    assert model_eval.latest_artifact(t, tmp_path).name == "claude-sonnet-5-5-2026-11-07.json"
    assert model_eval.latest_artifact({**t, "model": "nope"}, tmp_path) is None
