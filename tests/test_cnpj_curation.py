import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import cnpj_curation as cc
from src.synth import entity_registry as er
from tests.test_entity_registry import FakeTable


def _fetch(mapping):
    def f(cnpj):
        root = "".join(ch for ch in str(cnpj or "") if ch.isdigit())[:8]
        return mapping.get(root)
    return f


def test_verify_accepts_matching_razao():
    ent = {"alias_forms": ["Itaú Unibanco", "ITAU"], "display_name": "Itaú"}
    fetch = _fetch({"60701190": {"razao_social": "ITAU UNIBANCO S.A.", "nome_fantasia": None}})
    res = cc.verify(ent, "60701190", fetcher=fetch)
    assert res["ok"] and res["matched"] == "ITAU"


def test_verify_rejects_wrong_company():
    # a transposed/incorrect CNPJ that resolves to some other company -> rejected
    ent = {"alias_forms": ["Nubank"], "display_name": "Nubank"}
    fetch = _fetch({"18236120": {"razao_social": "PADARIA CENTRAL LTDA", "nome_fantasia": None}})
    res = cc.verify(ent, "18236120", fetcher=fetch)
    assert res["ok"] is False and res["reason"] == "name_mismatch"


def test_verify_rejects_on_fetch_failure():
    ent = {"alias_forms": ["Stone"], "display_name": "Stone"}
    res = cc.verify(ent, "16501555", fetcher=lambda c: None)
    assert res["ok"] is False and res["reason"] == "fetch_failed"


def test_curate_writes_only_verified(monkeypatch):
    t = FakeTable()
    er.put_entity("nubank", "Nubank", ["NUBANK"], confidence="curated", table=t)
    er.put_entity("ghostco", "GhostCo", ["GHOSTCO"], confidence="curated", table=t)
    fetch = _fetch({
        "18236120": {"razao_social": "NU PAGAMENTOS S.A. - NUBANK"},   # matches
        "99999999": {"razao_social": "OUTRA EMPRESA LTDA"},            # mismatch
    })
    seed = {"nubank": "18236120", "ghostco": "99999999"}
    report = cc.curate(seed, fetcher=fetch, apply=True, table=t, pause_sec=0)
    by = {r["entity"]: r for r in report}
    assert by["nubank"]["written"] is True
    assert er.resolve_by_cnpj("18236120", table=t) == "nubank"
    assert by["ghostco"]["ok"] is False and by["ghostco"].get("written") is False
    assert er.get_entity("ghostco", table=t).get("cnpj_roots", []) == []


def test_curate_skips_entity_with_existing_root(monkeypatch):
    t = FakeTable()
    er.put_entity("cielo", "Cielo", ["CIELO"], cnpj_roots=["01027058"], confidence="curated", table=t)
    called = {"n": 0}
    def fetch(c): called["n"] += 1; return None
    report = cc.curate({"cielo": "99999999"}, fetcher=fetch, apply=True, table=t, pause_sec=0)
    assert report[0]["reason"] == "already_has_root" and called["n"] == 0


def test_dry_run_never_writes():
    t = FakeTable()
    er.put_entity("nubank", "Nubank", ["NUBANK"], confidence="curated", table=t)
    fetch = _fetch({"18236120": {"razao_social": "NU PAGAMENTOS S.A. - NUBANK"}})
    report = cc.curate({"nubank": "18236120"}, fetcher=fetch, apply=False, table=t, pause_sec=0)
    assert report[0]["ok"] is True and report[0]["written"] is False
    assert er.get_entity("nubank", table=t).get("cnpj_roots", []) == []
