"""#329 spend gate + per-run token cap."""
import threading

from src.board import gate as G


def dec(cfg=None, row=None, conn=None, tenant="t1", boom=None):
    def config(t):
        if boom == "config":
            raise RuntimeError("ddb")
        return cfg

    def model(t):
        if boom == "model":
            raise RuntimeError("kms")
        return conn
    return G.decide(tenant, config=config, raw_row=lambda t: row, client_model=model)


PAYING = {"tenant_id": "t1", "billing": {"state": "active"}}
TRIAL = {"tenant_id": "t1", "billing": {"state": "trial"}}
OPERATOR = {"tenant_id": "t1", "billing": None}


def test_client_model_passes_even_on_trial_and_is_client_billed():
    d = dec(TRIAL, {}, conn=object())
    assert d.allowed and d.reason == "client_model" and d.client_billed


def test_paying_tenant_passes_on_hosted():
    d = dec(PAYING, {})
    assert d.allowed and d.reason == "paying" and not d.client_billed


def test_owner_flag_must_be_literally_true():
    assert dec(OPERATOR, {"board_owner_ok": True}).reason == "owner_flag"
    assert not dec(OPERATOR, {"board_owner_ok": "true"}).allowed


def test_trial_lapsed_unbilled_and_unknown_are_refused():
    assert dec(TRIAL, {}).reason == "not_paying:trial"
    assert dec({"billing": {"state": "lapsed"}}, {}).reason == "not_paying:lapsed"
    assert dec(OPERATOR, {}).reason == "not_paying:unbilled"
    assert dec(None, None).reason == "tenant_not_provisioned"
    assert dec(PAYING, {}, tenant="").reason == "no_tenant"


def test_lookup_failures_fail_closed():
    assert dec(PAYING, {}, boom="config").reason == "tenant_lookup_failed"
    d = dec(TRIAL, {}, boom="model")               # broken key lookup grants nothing
    assert not d.allowed
    assert dec(PAYING, {}, boom="model").reason == "paying"


def test_token_cap_default_override_and_bounds():
    assert dec(PAYING, {}).token_cap == G.DEFAULT_TOKEN_CAP
    assert dec(PAYING, {"board_token_cap": 50_000}).token_cap == 50_000
    assert dec(PAYING, {"board_token_cap": 10**9}).token_cap == G.MAX_TOKEN_CAP
    assert dec(PAYING, {"board_token_cap": "x"}).token_cap == G.DEFAULT_TOKEN_CAP


def test_meter_is_thread_safe_and_stops_at_the_cap():
    m = G.Meter(cap=10_000)
    ts = [threading.Thread(target=lambda: [m.add({"input_tokens": 40, "output_tokens": 10})
                                           for _ in range(50)]) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert m.calls == 200 and m.total == 10_000 and m.stop()
    assert m.summary()["tokens_in"] == 8_000
    assert not G.Meter(cap=10).stop()
