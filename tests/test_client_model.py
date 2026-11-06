"""#346 (BYOK story 1) + #349 (guardrail parity): a tenant's own model behind converse."""
import json
from types import SimpleNamespace

import pytest

from src.dashboard import agent_ask as aa
from src.synth import bedrock_llm
from src.synth import client_model as cm

KEY = "sk-ant-api03-SECRETSECRETSECRET-wxyz"
T = cm.Target(provider=cm.ANTHROPIC_KEY, model="claude-sonnet-5-5", tenant="t-1", api_key=KEY)
GR = {"id": "gr-1", "version": "1"}


class _Client:
    """A fake ``anthropic.Anthropic``: records the request, returns a canned response."""

    def __init__(self, stop="end_turn", text="Resposta [c1].", exc=None, model="claude-sonnet-5-5"):
        self.kw, self.stop, self.text, self.exc, self.model = None, stop, text, exc, model
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.kw = kw
        if self.exc:
            raise self.exc
        usage = SimpleNamespace(input_tokens=100, output_tokens=40, cache_read_input_tokens=900,
                                cache_creation_input_tokens=0)
        return SimpleNamespace(stop_reason=self.stop, model=self.model, usage=usage,
                               content=[SimpleNamespace(type="thinking", thinking=""),
                                        SimpleNamespace(type="text", text=self.text)])


# --- resolver --------------------------------------------------------------------------------

def test_no_tenant_or_no_store_is_hosted():
    assert cm.resolve_target(None, loader=lambda t: T) is None
    assert cm.resolve_target("t-1", loader=None) is None
    assert cm.tenant_loader() is None            # no key store until #347: everyone is hosted


def test_a_hosted_or_absent_row_is_hosted_and_a_connected_one_is_returned():
    assert cm.resolve_target("t-1", loader=lambda t: None) is None
    assert cm.resolve_target("t-1", loader=lambda t: cm.Target()) is None
    assert cm.resolve_target("t-1", loader=lambda t: T) is T


def test_a_model_off_the_allow_list_is_refused_not_downgraded():
    bad = cm.Target(provider=cm.ANTHROPIC_KEY, model="claude-opus-5-5", api_key=KEY)
    with pytest.raises(cm.ModelError) as e:
        cm.resolve_target("t-1", loader=lambda t: bad)
    assert e.value.code == cm.NO_ACCESS


def test_a_store_failure_raises_instead_of_answering_on_hosted():
    def boom(t):
        raise RuntimeError("kms down")
    with pytest.raises(RuntimeError):
        cm.resolve_target("t-1", loader=boom)


def test_the_key_never_shows_in_repr():
    assert KEY not in repr(T) and KEY not in str(T)
    assert T.client_billed and not T.hosted and cm.Target().hosted


# --- the Anthropic call ----------------------------------------------------------------------

def test_request_shape_and_usage():
    c = _Client()
    text, used = cm.anthropic_converse(
        T, "DADOS ... PERGUNTA", system="SYS", max_tokens=700, client=c,
        history=[{"role": "user", "text": "q1"}, {"role": "assistant", "text": "a1"}])
    assert text == "Resposta [c1]."
    kw = c.kw
    assert kw["model"] == "claude-sonnet-5-5" and kw["system"] == "SYS"
    assert kw["max_tokens"] == cm.MIN_MAX_TOKENS                 # thinking needs room past 700
    assert kw["output_config"] == {"effort": "low"}
    assert kw["betas"] == [cm.FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert "temperature" not in kw and "thinking" not in kw       # both 400 on Sonnet 5.5
    assert [m["role"] for m in kw["messages"]] == ["user", "assistant", "user"]
    assert kw["messages"][-1]["content"][-1] == {"type": "text", "text": "DADOS ... PERGUNTA"}
    assert used == {"input_tokens": 1000, "output_tokens": 40, "cache_read": 900, "cache_write": 0,
                    "model": "claude-sonnet-5-5", "stop_reason": "end_turn"}


def test_malformed_history_is_dropped_like_the_hosted_path():
    c = _Client()
    cm.anthropic_converse(T, "p", client=c, history=[{"role": "assistant", "text": "a"}])
    assert len(c.kw["messages"]) == 1


def test_a_refusal_is_an_error_not_an_empty_answer():
    with pytest.raises(cm.ModelError) as e:
        cm.anthropic_converse(T, "p", client=_Client(stop="refusal", text=""))
    assert e.value.code == cm.REFUSED


def _status_error(cls, status, body=None):
    import httpx2
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("x", response=httpx2.Response(status, request=req), body=body)


@pytest.mark.parametrize("cls_name,status,code", [
    ("AuthenticationError", 401, cm.KEY_REJECTED),
    ("PermissionDeniedError", 403, cm.NO_ACCESS),
    ("NotFoundError", 404, cm.NO_ACCESS),
    ("RateLimitError", 429, cm.UNAVAILABLE),
    ("InternalServerError", 529, cm.UNAVAILABLE),
])
def test_sdk_errors_map_to_reader_codes(cls_name, status, code):
    anthropic = pytest.importorskip("anthropic")
    exc = _status_error(getattr(anthropic, cls_name), status)
    with pytest.raises(cm.ModelError) as e:
        cm.anthropic_converse(T, "p", client=_Client(exc=exc))
    assert e.value.code == code
    assert e.value.__cause__ is None and KEY not in str(e.value)   # nothing chained to a log


def test_no_credit_and_workspace_codes():
    anthropic = pytest.importorskip("anthropic")
    assert cm._classify(_status_error(anthropic.APIStatusError, 402)).code == cm.NO_CREDIT
    ws = _status_error(anthropic.BadRequestError, 400,
                       body={"error": {"message": "key is not scoped to a workspace"}})
    assert cm._classify(ws).code == cm.NEEDS_WORKSPACE


# --- converse: routing and the no-fallback rule ------------------------------------------------

class _Bedrock:
    def __init__(self):
        self.calls = 0

    def converse(self, **kw):
        self.calls += 1
        return {"stopReason": "end_turn", "usage": {"inputTokens": 1, "outputTokens": 1},
                "output": {"message": {"content": [{"text": "NOVA"}]}}}


def _bedrock(monkeypatch):
    rt = _Bedrock()
    monkeypatch.setattr(bedrock_llm.boto3, "client", lambda name, **k: rt)
    return rt


def test_hosted_target_runs_bedrock_unchanged(monkeypatch):
    rt = _bedrock(monkeypatch)
    assert bedrock_llm.converse("p", target=None) == "NOVA"
    assert bedrock_llm.converse("p", target=cm.Target()) == "NOVA"
    assert rt.calls == 2


def test_client_target_answers_on_the_client_model(monkeypatch):
    rt = _bedrock(monkeypatch)
    monkeypatch.setattr(cm, "anthropic_converse",
                        lambda t, p, **k: ("CLIENTE", {"input_tokens": 5, "output_tokens": 2,
                                                       "model": t.model}))
    usage = {}
    assert bedrock_llm.converse("p", target=T, usage_out=usage) == "CLIENTE"
    assert rt.calls == 0
    assert usage["provider"] == cm.ANTHROPIC_KEY and usage["client_billed"] is True


def test_a_failing_client_model_never_falls_back_to_hosted(monkeypatch, capsys):
    rt = _bedrock(monkeypatch)

    def fail(t, p, **k):
        raise cm.ModelError(cm.KEY_REJECTED, "the provider rejected the key")
    monkeypatch.setattr(cm, "anthropic_converse", fail)
    usage = {}
    assert bedrock_llm.converse("p", target=T, usage_out=usage) is None
    assert rt.calls == 0 and usage["model_error"] == cm.KEY_REJECTED
    out = capsys.readouterr().out
    assert KEY not in out and "t-1" not in out and cm.KEY_REJECTED in out


# --- #349 guardrail parity ---------------------------------------------------------------------

class _GR:
    def __init__(self, by_source):
        self.by_source, self.seen = by_source, []

    def apply_guardrail(self, **kw):
        self.seen.append((kw["source"], kw["content"][0]["text"]["text"]))
        return self.by_source.get(kw["source"], {"action": "NONE"})


BLOCK = {"action": "GUARDRAIL_INTERVENED",
         "assessments": [{"topicPolicy": {"topics": [{"name": "x", "action": "BLOCKED"}]}}]}
MASK = {"action": "GUARDRAIL_INTERVENED", "outputs": [{"text": "Contato: {EMAIL} [c1]."}],
        "assessments": [{"sensitiveInformationPolicy": {"piiEntities": [
            {"type": "EMAIL", "action": "ANONYMIZED"}]}}]}


def _guard(monkeypatch, by_source, answer="Contato: a@b.com [c1]."):
    g = _GR(by_source)
    monkeypatch.setattr(bedrock_llm.boto3, "client", lambda name, **k: g)
    called = []
    monkeypatch.setattr(cm, "anthropic_converse",
                        lambda t, p, **k: called.append(p) or (answer, {"output_tokens": 1}))
    return g, called


def test_a_blocked_question_never_reaches_the_client_model(monkeypatch):
    g, called = _guard(monkeypatch, {"INPUT": BLOCK})
    usage = {}
    assert bedrock_llm.converse("CARDS + q", target=T, guardrail=GR, guard_text="q",
                                usage_out=usage) is None
    assert usage["guardrail"] == "intervened" and called == []
    assert g.seen == [("INPUT", "q")]                    # the question only, never the context


def test_an_answer_is_checked_and_masked_not_refused(monkeypatch):
    g, called = _guard(monkeypatch, {"OUTPUT": MASK})
    usage = {}
    assert bedrock_llm.converse("p", target=T, guardrail=GR, guard_text="q",
                                usage_out=usage) == "Contato: {EMAIL} [c1]."
    assert "guardrail" not in usage and [s for s, _ in g.seen] == ["INPUT", "OUTPUT"]


def test_a_blocked_answer_is_refused(monkeypatch):
    _guard(monkeypatch, {"OUTPUT": BLOCK})
    usage = {}
    assert bedrock_llm.converse("p", target=T, guardrail=GR, guard_text="q", usage_out=usage) is None
    assert usage["guardrail"] == "intervened"


def test_a_guardrail_error_fails_closed():
    class Down:
        def apply_guardrail(self, **kw):
            raise RuntimeError("throttled")
    assert cm.apply_guardrail(GR, "q", "INPUT", client=Down()) == (True, "")


# --- the Ask handler ------------------------------------------------------------------------

def _ask(monkeypatch, *, loader=None, code=None):
    gaps, seen = [], {}
    monkeypatch.setattr(aa, "_load_feed", lambda b: {"feed": []})
    monkeypatch.setattr(aa, "_record_gap", lambda *a, **k: gaps.append(a))
    monkeypatch.setattr(aa, "_quota_take", lambda sub: None)
    monkeypatch.setenv("ONCA_SITE_BUCKET", "b")
    monkeypatch.delenv("ONCA_ASK_GUARDRAIL", raising=False)
    monkeypatch.setattr(cm, "tenant_loader", lambda: loader)

    def fake_converse(prompt, *, usage_out=None, target=None, **kw):
        seen["target"] = target
        if code:
            usage_out["model_error"] = code
        return None

    monkeypatch.setattr(bedrock_llm, "converse", fake_converse)
    monkeypatch.setattr(aa, "select_grounding", lambda *a, **k: ([{"id": "c1", "narrative": "x"}], "lexical"))
    ev = {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": "u"}}}}, "headers": {},
          "body": json.dumps({"q": "O que o Itaú mudou no crédito?"})}
    return json.loads(aa.lambda_handler(ev, None)["body"]), gaps, seen


def test_ask_without_a_connected_model_is_hosted(monkeypatch):
    body, gaps, seen = _ask(monkeypatch)
    assert seen["target"] is None and body["reason"] == "no-model" and len(gaps) == 1


def test_ask_reports_the_client_model_error_and_records_no_gap(monkeypatch):
    body, gaps, _ = _ask(monkeypatch, code=cm.NO_CREDIT)
    assert body["reason"] == "client-model-error" and body["model_error"] == cm.NO_CREDIT
    assert body["answer"] == cm.MESSAGES[cm.NO_CREDIT] and gaps == []


# --- the agent API: client-billed tokens don't draw the included quota (#353) ------------------

def test_agent_api_answers_on_the_client_model_without_drawing_the_quota(monkeypatch):
    from src.dashboard import agent_api as aapi
    from src.dashboard import api_keys, api_usage, tenant_config

    monkeypatch.setenv("ONCA_SITE_BUCKET", "bucket")
    monkeypatch.delenv("ONCA_ASK_GUARDRAIL", raising=False)
    monkeypatch.setattr(api_keys, "lookup_key",
                        lambda raw, table=None: {"tenant_id": "acme", "scopes": ["ask"], "key_hash": "h"})
    monkeypatch.setattr(api_keys, "touch_last_used", lambda key_hash, table=None: None)
    monkeypatch.setattr(tenant_config, "get_tenant_config",
                        lambda t: {"tier": "saas", "modules": ["banking"]})
    monkeypatch.setattr(aapi, "_load_feed", lambda b: {"feed": []})
    seen = {}
    monkeypatch.setattr(cm, "tenant_loader", lambda: (lambda t: seen.setdefault("tenant", t) and T))

    def fake_answer(q, *, converser, **kw):
        converser("prompt", system="sys", max_tokens=100)
        return {"answer": "ok", "refused": False, "grounded": True, "citations": []}
    monkeypatch.setattr(aapi, "answer", fake_answer)
    monkeypatch.setattr(cm, "anthropic_converse",
                        lambda t, p, **k: ("ok", {"input_tokens": 12, "output_tokens": 34}))
    drawn = []
    monkeypatch.setattr(api_usage, "record_usage",
                        lambda tenant_id, tokens, quota=None, table=None: drawn.append(tokens) or None)
    monkeypatch.setattr(api_usage, "get_usage", lambda t: {"period": "2026-11", "tokens_used": 0})

    body = json.loads(aapi.lambda_handler(
        {"headers": {"authorization": "Bearer sk_onca_x"}, "body": json.dumps({"q": "hi"})}, None)["body"])
    assert seen["tenant"] == "acme" and drawn == [0]
    assert body["usage"]["total_tokens"] == 46 and body["usage"]["client_billed"] is True
    assert body["usage"]["provider"] == cm.ANTHROPIC_KEY
