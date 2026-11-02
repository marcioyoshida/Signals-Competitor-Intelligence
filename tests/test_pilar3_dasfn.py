"""#301 / ADR 022 Phase 6: multi-bank Pilar 3 text from the DASFN qualitative tables."""
import datetime as dt

import pytest

from src.ingest import bcb_km1, pilar3, pilar3_dasfn as D


@pytest.fixture(autouse=True)
def _offline_registry(monkeypatch):
    """No test here may reach the live registry for an entity's industries."""
    monkeypatch.setattr(bcb_km1, "registry_industries", lambda eid: None)

LONG = ("O gerenciamento do risco de liquidez é de responsabilidade da diretoria de riscos, que observa "
        "a política de gerenciamento de riscos e de capital, a declaração de apetite e os limites de "
        "liquidez aprovados pelo conselho de administração da instituição.")


def _row(name, rec, url, arg, cnpj="1"):
    return {"NomeInstituicao": name, "CnpjInstituicao": cnpj, "Recurso": rec, "URLDados": url, "Argumento": arg}


def test_year_reads_the_reference_date_not_a_filing_stamp():
    assert D._year({"Argumento": "26-04-02_202451_20251231_ova.json"}) == 2025
    assert D._year({"Argumento": "2024"}) == 2024
    assert D._year({"Argumento": "", "URLDados": "https://x/ova/v2/2025"}) == 2025


def test_list_keeps_the_latest_year_and_prefers_v2():
    rows = [_row("BCO X", "/ova/{ano}", "https://x/ova/2024", "2024"),
            _row("BCO X", "/ova/v2/{ano}", "https://x/ova/v2/2025", "2025"),
            _row("BCO X", "/ova/{ano}", "https://x/ova/2025", "2025"),
            _row("BCO X", "/liqa/{ano}", "https://x/liqa/2025", "2025"),
            _row("BCO X", "/km1/{trimestre}", "https://x/km1/2025-4", "2025-4")]
    out = D.list_qualitative(rows)["1"]
    assert out["tables"] == {"ova": "https://x/ova/v2/2025", "liqa": "https://x/liqa/2025"}
    assert out["year"] == 2025


class _Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


def test_robots_follow_rfc9309_status_rules():
    answers = {"https://ok": _Resp(200, "User-agent: *\nDisallow: /private\n"),
               "https://none": _Resp(404), "https://broken": _Resp(500)}
    r = D.Robots(get=lambda u: answers[u.rsplit("/robots.txt", 1)[0]])
    assert r.allowed("https://ok/pilar3/ova") and not r.allowed("https://ok/private/x")
    assert r.allowed("https://none/anything")              # 4xx: no restrictions
    assert not r.allowed("https://broken/anything")        # 5xx: assume disallow

    def boom(u):
        raise OSError("unreachable")
    assert not D.Robots(get=boom).allowed("https://down/x")


ALLOW = D.Robots(get=lambda u: _Resp(404))


def test_prose_blocks_keep_text_and_drop_codes_and_numbers():
    doc = {"ova_a": LONG, "codigo": "OVA", "valor": 12.5, "itens": [{"texto": LONG}, {"texto": "curto"}]}
    assert D.prose_blocks(doc) == [LONG]                   # deduplicated, short strings dropped


def test_a_pointer_only_bank_is_skipped():
    pointer = {"ova_a": "As informações podem ser encontradas no Relatório de Gerenciamento de Riscos "
                        "e Capital - Pilar 3, disponível no site de Relações com Investidores."}
    prose, used = D.bank_prose({"ova": "https://b/ova", "liqa": "https://b/liqa"}, robots=ALLOW,
                               fetch=lambda u: pointer, sleep=lambda s: None)
    assert prose == "" and used == []


def test_a_failing_host_is_not_retried_for_its_other_tables():
    calls = []

    def fetch(u):
        calls.append(u)
        if "dead" in u:
            raise OSError("connect timeout")
        return {"t": LONG * 3}
    tables = {"ova": "https://dead/ova", "liqa": "https://dead/liqa", "cra": "https://dead/cra"}
    assert D.bank_prose(tables, robots=ALLOW, fetch=fetch, sleep=lambda s: None) == ("", [])
    assert calls == ["https://dead/ova"]


def test_bank_prose_is_sectioned_by_table():
    prose, used = D.bank_prose({"ova": "https://b/ova", "liqa": "https://b/liqa"}, robots=ALLOW,
                               fetch=lambda u: {"t": LONG * 3}, sleep=lambda s: None)
    assert used == ["ova", "liqa"]
    assert prose.startswith(D.TABLES["ova"]) and D.TABLES["liqa"] in prose


def _resources(year):
    return {"1": {"name": "BCO SAFRA S.A.", "year": year, "tables": {"ova": "https://s/ova", "liqa": "https://s/liqa"}},
            "2": {"name": "UNTRACKED DTVM", "year": year, "tables": {"ova": "https://u/ova"}}}


def test_build_resolves_caps_the_date_and_skips_stale_tables():
    fetched = []
    recs = D.build(_resources(2026), resolver=lambda i: ["safra"] if "SAFRA" in i["institution"] else [],
                   robots=ALLOW, fetch=lambda u: fetched.append(u) or {"t": LONG * 3},
                   sleep=lambda s: None, today=dt.date(2026, 11, 2))
    assert list(recs) == ["safra"] and not any("https://u/" in u for u in fetched)
    r = recs["safra"]
    assert r["kind"] == "dasfn" and r["year"] == 2026
    assert r["date"] == "2026-11-02"                      # not 2026-12-31, a future date
    assert r["sentences"] and r["_prose"]
    assert D.build(_resources(2022), resolver=lambda i: ["safra"], robots=ALLOW,
                   fetch=lambda u: {"t": LONG * 3}, sleep=lambda s: None, today=dt.date(2026, 11, 2)) == {}


def test_budget_stops_new_banks():
    ticks = iter([0, 0, 500, 500, 500])
    res = {"1": {"name": "A", "year": 2025, "tables": {"ova": "https://a/ova"}},
           "2": {"name": "B", "year": 2025, "tables": {"ova": "https://b/ova"}}}
    recs = D.build(res, resolver=lambda i: [i["institution"].lower()], robots=ALLOW,
                   fetch=lambda u: {"t": LONG * 3}, sleep=lambda s: None, budget_s=100,
                   clock=lambda: next(ticks), today=dt.date(2026, 11, 2))
    assert list(recs) == ["a"]


class _S3:
    def __init__(self):
        self.objects = {}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise Exception("404")

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body


def test_kb_promotion_uses_the_in_memory_prose_and_a_stable_year_key():
    rec = {"entity": "safra", "kind": "dasfn", "company": "BCO SAFRA S.A.", "year": 2026,
           "date": "2026-11-02", "url": "https://s/ova", "subject": "Pilar 3 — 2026", "_prose": "TEXTO " + LONG}
    s3 = _S3()
    keys = pilar3.promote_to_kb({"records": {"safra": rec}}, bucket="raw", s3=s3,
                                fetch_pages=lambda u: (_ for _ in ()).throw(AssertionError("no PDF fetch")))
    assert keys == ["pilar3/safra/dasfn-2026.txt"]
    body = s3.objects[keys[0]].decode()
    assert body.startswith("Pilar 3 — tabelas qualitativas de gerenciamento de riscos — BCO SAFRA S.A.")
    assert "BCB-DASFN-Pilar3" in s3.objects[keys[0] + ".metadata.json"].decode()
    later = {**rec, "date": "2026-12-06"}                 # next month: same year, same key
    assert pilar3.promote_to_kb({"records": {"safra": later}}, bucket="raw", s3=s3) == []
    stored = {**rec}
    stored.pop("_prose")                                   # a record loaded from the index
    assert pilar3.promote_to_kb({"records": {"safra": stored}}, bucket="raw", s3=_S3()) == []


def test_the_index_never_stores_the_prose():
    out = {}

    class _Put:
        def put_object(self, Bucket, Key, Body, ContentType=None):
            out["body"] = Body.decode()
    pilar3.publish({"records": {"safra": {"entity": "safra", "_prose": "x" * 50, "sentences": ["s"]}}},
                   "b", s3=_Put())
    assert "_prose" not in out["body"] and '"sentences"' in out["body"]


def test_run_lets_the_full_ipe_report_win_over_dasfn_tables(monkeypatch):
    from src.ingest import cvm_ipe
    monkeypatch.setattr(cvm_ipe, "fetch_material_facts", lambda *a, **k: [])
    monkeypatch.setattr(pilar3, "build_corpus", lambda rows, **k: {"as_of": "2026-11-02", "records": {
        "itau": {"entity": "itau", "date": "2026-08-04", "url": "https://rad.cvm/x", "sentences": ["a"]}}})
    monkeypatch.setattr(D, "fetch_catalog", lambda: [])
    monkeypatch.setattr(D, "build", lambda res, **k: {
        "itau": {"entity": "itau", "kind": "dasfn", "date": "2025-12-31", "year": 2025, "sentences": ["b"]},
        "bb": {"entity": "bb", "kind": "dasfn", "date": "2025-12-31", "year": 2025, "sentences": ["c"]}})
    published = {}
    monkeypatch.setattr(pilar3, "load_index", lambda b, **k: {})
    monkeypatch.setattr(pilar3, "publish", lambda idx, b, **k: published.update(idx))
    res = pilar3.run("digests")
    assert res["dasfn_entities"] == 2 and res["entities"] == 2
    assert published["records"]["itau"]["url"] == "https://rad.cvm/x"     # the IPE report kept
    assert published["records"]["bb"]["kind"] == "dasfn"


def test_resolver_name_expands_bcb_abbreviations():
    """Live 2026-11-02: BB, BMG, BV and Safra resolved only with BANCO spelled out."""
    from src.ingest.bcb_km1 import resolver_name
    assert resolver_name("BCO DO BRASIL S.A.") == "BANCO DO BRASIL S.A."
    assert resolver_name("BCO VOTORANTIM S.") == "BANCO VOTORANTIM S.A."
    assert resolver_name("EBURY BCO DE CÂMBIO S.A.") == "EBURY BANCO DE CÂMBIO S.A."
    assert resolver_name("BANCO C6 S.A.") == "BANCO C6 S.A."
    assert resolver_name("BCOB S.A.") == "BCOB S.A."                 # a word, not the abbreviation


def test_a_bank_never_binds_to_a_non_filer_namesake():
    """"BANCO B3 S.A." resolves to b3, the exchange: its Pilar 3 must not land there."""
    from src.ingest.bcb_km1 import pilar3_entities
    inds = {"b3": ["financial-data-analytics"], "gm": ["banking"], "neon": ["fintech"]}
    assert pilar3_entities(["b3", "gm", "neon", "unknown"], inds.get) == ["gm", "neon", "unknown"]
    recs = D.build({"1": {"name": "BCO B3 S.A.", "year": 2025, "tables": {"ova": "https://b3/ova"}}},
                   resolver=lambda i: ["b3"], robots=ALLOW, industries_of=inds.get,
                   fetch=lambda u: {"t": LONG * 3}, sleep=lambda s: None, today=dt.date(2026, 11, 2))
    assert recs == {}


def test_km1_applies_the_same_name_and_industry_rules(monkeypatch):
    from src.ingest import bcb_km1
    seen = []
    monkeypatch.setattr(bcb_km1, "_get", lambda u, **k: {"km1_7": {"t": 0.15}})
    res = {"1": {"name": "BCO DO BRASIL S.A.", "url": "https://bb/km1", "base_date": 20250630},
           "2": {"name": "BCO B3 S.A.", "url": "https://b3/km1", "base_date": 20250630}}
    out = bcb_km1.build(res, resolver=lambda i: seen.append(i["institution"]) or
                        (["bb"] if "BRASIL" in i["institution"] else ["b3"]),
                        industries_of={"bb": ["banking"], "b3": ["financial-data-analytics"]}.get)
    assert seen[0] == "BANCO DO BRASIL S.A." and [r["entity"] for r in out] == ["bb"]


def test_the_citation_title_names_the_bank():
    """A generic risk chunk is only attributable through its header (live: BTG's CRA chunk ranked
    first for a Mercado Pago question)."""
    rec = {"entity": "btg", "kind": "dasfn", "company": "BCO BTG PACTUAL S.A.", "year": 2025,
           "date": "2025-12-31", "url": "https://btg/ova", "subject": "Pilar 3 — 2025"}
    meta = pilar3.kb_metadata(rec)
    assert meta["title"].startswith("BCO BTG PACTUAL S.A. — ")
    from src.dashboard import agent_ask
    snip = agent_ask.kb_snippets_from_results([{"content": {"text": "O risco de crédito..."},
        "metadata": meta, "location": {"s3Location": {"uri": "s3://raw/pilar3/btg/dasfn-2025.txt"}}}])
    assert "BCO BTG PACTUAL S.A." in agent_ask.kb_header(snip[0])
