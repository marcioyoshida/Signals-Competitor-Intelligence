"""ADR 019 C1–C5 (#303): the structured ingest digest is BYTE-IDENTICAL across the refactor.

Every source is stubbed with a deterministic fixture, and the handler runs end to end (structured
mode, bucket + registry configured, so every side effect fires). The golden file was produced by
the pre-refactor handler (origin/main ff6403f). Three things are compared:

* ``digest``: the exact bytes written to S3;
* ``state``: every delta-state call (source key, seed flag, move key/value field, threshold),
  as a sorted multiset, so the migrated loop proves the same state keys and knobs;
* ``calls``: every fetch and side-effect call with its arguments, as a sorted multiset, which
  proves the C3 ``spec.params`` move kept every effective value (defaults included).

Regenerate only on purpose: ``ONCA_GOLDEN_UPDATE=1 pytest tests/test_adr019_golden.py``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.ingest import lambda_port as lp

GOLDEN = Path(__file__).parent / "fixtures" / "adr019_golden_digest.json"


def _recs(prefix: str, n: int = 3, **extra):
    return [{"id": f"{prefix}-{i}", "title": f"{prefix} title {i}", "text": f"{prefix} text {i}",
             "date": f"2026-10-{20 + i:02d}", **extra} for i in range(n)]


class _Ctx:
    aws_request_id = "golden"

    def get_remaining_time_in_millis(self):
        return 900_000


def _run(monkeypatch, env: dict[str, str] | None = None, after=None):
    """``after(monkeypatch, rec)`` may swap a stub before the handler runs (completion tests)."""
    calls: list[str] = []
    state: list[str] = []

    def _plain(x):
        if callable(x):
            return f"<fn {getattr(x, '__name__', type(x).__name__)}>"      # no memory addresses
        if isinstance(x, (set, frozenset)):
            return sorted(x)
        return x

    def rec(name, ret):
        def f(*a, **k):
            calls.append(json.dumps([name, [_plain(x) for x in a],
                                     {kk: _plain(v) for kk, v in sorted(k.items())}],
                                    sort_keys=True, default=repr))
            return ret(*a, **k) if callable(ret) else ret
        return f

    for k in [k for k in os.environ if k.startswith("ONCA_")]:
        monkeypatch.delenv(k, raising=False)
    base = {"ONCA_DIGESTS_BUCKET": "digests", "ONCA_ENTITIES_TABLE": "ents",
            "ONCA_COMPETITORS": "ALFA,BETA", "ONCA_COMPETITOR_ISPB": "111,222",
            "ONCA_SEC_TICKERS": "STNE,PAGS", "ONCA_FATOS_WATCHLIST": "GAMA",
            "ONCA_JUROS_COMPETITORS": "ALFA", "ONCA_DOU_WATCHLIST": "DELTA",
            "ONCA_DOU_SECTOR_TOPICS": "false"}
    for k, v in {**base, **(env or {})}.items():
        monkeypatch.setenv(k, v)

    def new_since(source, docs, seed_if_empty=False, commit=True):
        state.append(json.dumps(["new", source, bool(seed_if_empty)]))
        return [d for d in docs][:2]

    def moves_since(source, items, key_field, value_field, min_pct):
        state.append(json.dumps(["moves", source, key_field, value_field, float(min_pct)]))
        return [{**items[0], "pct": 20.0}] if items else []

    monkeypatch.setattr(lp, "_new_since_last_run", new_since)
    monkeypatch.setattr(lp, "_moves_since_last_run", moves_since)
    monkeypatch.setattr(lp, "_populate_corpus_and_sync", rec("corpus", None))
    monkeypatch.setattr(lp, "_promote_decisions_to_kb", lambda: None)

    from src.ingest import source_health
    monkeypatch.setattr(source_health, "merge_and_publish", lambda *a, **k: None)

    put = {}

    class _S3:
        def put_object(self, Bucket, Key, Body, **k):
            put[Key] = Body

    monkeypatch.setattr(lp.boto3, "client", lambda name, *a, **k: _S3())

    S = monkeypatch.setattr
    # IF.data: cached quarter
    S(lp.bcb_ifdata, "load_index", rec("ifdata.load_index", {"top": _recs("mkt", 2), "count": 2}))
    S(lp.bcb_ifdata, "latest_base_date", rec("ifdata.latest_base_date", 202606))
    S(lp.bcb_ifdata, "resolve_progress", rec("ifdata.resolve_progress", (0, True)))
    # new entrants + side effects
    S(lp.bcb_autorizacoes, "fetch_authorized", rec("autorizacoes.fetch", _recs("ent", 3, is_fintech=True, name="Ent Co")))
    S(lp.bcb_autorizacoes, "apply_verified_certifications", rec("autorizacoes.certs", []))
    S(lp.receita_cnpj, "enrich_entrants", rec("receita.enrich", lambda rows: [r.setdefault("trade_name", "Brand") for r in rows]))
    from src.synth import entity_registry as er
    S(er, "auto_create_from_entrant", rec("er.auto_create", lambda e, confidence=None: "eid-" + e["id"]))
    S(er, "classify_industries", rec("er.classify", (["fintech"], False)))
    S(er, "propose_news_safe", rec("er.propose_news_safe", None))
    S(er, "propose_industry", rec("er.propose_industry", None))
    S(er, "propose_group_merges", rec("er.group_merges", 0))
    S(er, "fatos_terms", rec("er.fatos_terms", ["REGFATO"]))
    S(er, "resolve_by_cnpj", rec("er.resolve_by_cnpj", lambda c: "eid-" + str(c)[:4] if c else None))
    S(er, "accumulate_aliases", rec("er.accumulate", lambda eid, names: True))
    S(er, "list_entities", rec("er.list_entities", [{"entity_id": "x", "cnpj_roots": ["12345678"]}]))
    # fiagro
    S(lp.cvm_fiagro, "fetch_fiagro", rec("fiagro.fetch", _recs("fia", 2, cnpj="11111111000100", pl=10, cotistas=5)))
    S(lp.cvm_fiagro, "for_pl_moves", rec("fiagro.for_pl", lambda rows: rows))
    S(lp.cvm_fiagro, "for_cotista_moves", rec("fiagro.for_cot", lambda rows: rows))
    S(lp.cvm_fiagro, "for_new_registrations", rec("fiagro.for_newreg", lambda rows, lookback_days=None: rows))
    # pix / juros / inf diario
    S(lp.bcb_pix, "fetch_recent", rec("pix.fetch", _recs("pixrow", 2)))
    S(lp.bcb_pix, "by_institution", rec("pix.by_inst", lambda rows, watchlist_ispb=None: _recs("pix", 3, ispb="1", tx_value=9)))
    S(lp.bcb_juros, "fetch_daily", rec("juros.fetch", _recs("jrow", 2)))
    S(lp.bcb_juros, "filter_rates", rec("juros.filter", lambda rows, institutions=None, modalities=None: _recs("jur", 3, move_key="k", rate_year=3)))
    S(lp.bcb_juros, "for_moves", rec("juros.for_moves", lambda rows: rows))
    S(lp.cvm_inf_diario, "fetch_latest", rec("infd.fetch", _recs("infd", 3, move_key="k", pl=1)))
    S(lp.cvm_inf_diario, "for_moves", rec("infd.for_moves", lambda rows: rows))
    # fatos / sec
    S(lp.cvm_ipe, "fetch_material_facts", rec("fatos.fetch", [{**r, "governance": i == 2, "company": "Co" + str(i), "cnpj": "2222222200010" + str(i)}
                                                            for i, r in enumerate(_recs("fato", 3))]))
    S(lp.sec_filings, "fetch_filings", rec("sec.fetch", _recs("sec", 3)))
    S(lp.sec_filings, "enrich_with_content", rec("sec.enrich", lambda rows: [r.setdefault("text", "body") for r in rows]))
    # macro / stores
    S(lp.bcb_macro, "fetch_selic", rec("macro.selic", {"rate": 15.0}))
    S(lp.bcb_macro, "fetch_focus", rec("macro.focus", [{"ipca": 4.5}]))
    S(lp.datajud, "fetch_recuperacao_judicial", rec("datajud.fetch", _recs("rj", 2)))
    S(lp.bcb_reclamacoes, "fetch_ranking", rec("reclam.fetch", _recs("rec", 2, period="2026T2", rank=1, entity="e")))
    S(lp.bcb_reclamacoes, "map_to_entities", rec("reclam.map", lambda rows, resolver=None: rows))
    S(lp.bcb_reclamacoes, "update_store", rec("reclam.store", None))
    # registry-loop document sources
    S(lp.federal_acts, "annotate", lambda rows: rows)
    S(lp.bcb_normativos, "fetch_recent", rec("normativos.fetch", _recs("bcb", 3, kind="regulatory", severity="high", industries=["banking"])))
    S(lp.cvm_fundos, "fetch_funds", rec("fundos.fetch", _recs("fund", 3)))
    S(lp.cvm_ofertas, "fetch_recent", rec("ofertas.fetch", _recs("of", 3, issuer="Issuer", issuer_cnpj="33333333000100")))
    S(lp.cvm_ofertas, "update_status_changes", rec("ofertas.status", lambda offerings, bucket: _recs("ofs", 1)))
    S(lp.cvm_normas, "fetch_recent", rec("cvm_normas.fetch", _recs("cvmn", 2)))
    S(lp.dou, "fetch_dou", rec("dou.fetch", _recs("dou", 3)))
    S(lp.cade, "fetch_atos", rec("cade.fetch", _recs("cade", 2)))
    S(lp.cade, "map_to_entities", rec("cade.map", lambda rows, resolver=None: rows))
    S(lp.ceis_cnep, "build_cnpj_index", rec("ceis.index", {"12345678": "x"}))
    S(lp.ceis_cnep, "fetch_sanctions", rec("ceis.fetch", _recs("san", 2)))
    S(lp.ceis_cnep, "map_to_entities", rec("ceis.map", lambda rows, cnpj_index=None: rows))
    S(lp.ceis_cnep, "update_store", rec("ceis.store", None))

    if after is not None:
        after(monkeypatch, rec)
    out = lp.lambda_handler({"mode": "structured"}, _Ctx())
    assert out["statusCode"] == 200
    return {"digest": put["lambda-digests/golden.json"].decode("utf-8"),
            "state": sorted(state), "calls": sorted(calls)}


VERTICALS = {"fs": {}}   # the live deployment; C5 asserts the sector behaviour separately


@pytest.mark.parametrize("name", list(VERTICALS))
def test_digest_state_and_calls_match_the_pre_refactor_golden(monkeypatch, name):
    update = os.environ.get("ONCA_GOLDEN_UPDATE") == "1"      # read before _run clears ONCA_*
    got = _run(monkeypatch, VERTICALS[name])
    if update:
        data = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
        data[name] = got
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True))
        pytest.skip("golden updated")
    want = json.loads(GOLDEN.read_text())[name]
    assert got["digest"] == want["digest"]
    assert got["state"] == want["state"]
    assert got["calls"] == want["calls"]
