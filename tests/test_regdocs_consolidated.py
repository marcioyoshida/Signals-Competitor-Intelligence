"""#294: BCB's consolidated text reaches the regulatory cards, the feed and the change fold."""
import http.server
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.dashboard import feed_builder
from src.ingest import reg_documents
from src.synth import regulatory

AMEND = [{"act": "Resolução CMN nº 5.333/2026", "detail": "Inclusão: art. 3º", "effective": None},
         {"act": "Resolução CMN nº 5.334/2026", "detail": "Nova redação: art. 1º, § 9º, incisos I, II e III",
          "effective": "2026-09-01"}]


def _index(*, diff_fetched="2026-10-30", amendments=AMEND, revoked=False):
    return {"res-cmn-5330": {
        "label": "Resolução CMN 5330",
        "versions": [
            {"hash": "a", "source": "dou", "fetched": "2026-09-03"},
            {"hash": "b", "source": "bcb", "fetched": "2026-10-01", "versao": "1.0"},
            {"hash": "c", "source": "bcb", "fetched": diff_fetched, "versao": "2.0",
             "diff": {"vs": "b", "summary": "modifica Art. 1", "added": 0, "removed": 0, "modified": 1}},
        ],
        "bcb": {"versao": "2.0", "amendments": amendments, "revoked": revoked,
                "url": "https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?tipo=x&numero=5330",
                "fetched": "2026-11-01"}}}


def test_consolidated_for_newest_amendment_first_and_a_recent_diff():
    c = regulatory.consolidated_for("res-cmn-5330", _index(), as_of="2026-11-01")
    assert c["versao"] == "2.0" and c["source"] == "BCB" and c["revoked"] is False
    assert [a["act"] for a in c["amendments"]] == ["Resolução CMN nº 5.334/2026", "Resolução CMN nº 5.333/2026"]
    assert c["diff"] == {"summary": "modifica Art. 1", "found": "2026-10-30",
                         "added": 0, "removed": 0, "modified": 1}


def test_consolidated_for_drops_an_old_diff_and_unknown_instruments():
    c = regulatory.consolidated_for("res-cmn-5330", _index(diff_fetched="2026-08-01"), as_of="2026-11-01")
    assert c["diff"] is None and c["amendments"]                 # the amendments still show
    assert regulatory.consolidated_for("res-cmn-9999", _index(), as_of="2026-11-01") is None
    assert regulatory.consolidated_for("res-cmn-5330", {}, as_of="2026-11-01") is None
    # a never-amended norm with no diff has nothing to say
    idx = _index(amendments=[], diff_fetched="2026-01-01")
    assert regulatory.consolidated_for("res-cmn-5330", idx, as_of="2026-11-01") is None
    # ... unless it was revoked
    idx = _index(amendments=[], diff_fetched="2026-01-01", revoked=True)
    assert regulatory.consolidated_for("res-cmn-5330", idx, as_of="2026-11-01")["revoked"] is True


def test_consolidated_sentence():
    c = regulatory.consolidated_for("res-cmn-5330", _index(), as_of="2026-11-01")
    s = regulatory.consolidated_sentence(c)
    assert s.startswith(" Texto consolidado (BCB, versão 2.0): última alteração pela Resolução CMN nº 5.334/2026")
    assert "Diferença para a versão anterior arquivada: modifica Art. 1." in s
    assert regulatory.consolidated_sentence(None) == ""


def _narrs():
    return [{"entity": None, "run_date": "2026-10-28", "lenses": ["regulatory"], "threat_score": 0.5,
             "narrative": "A Resolução CMN 5330 passa a valer a partir de 01/12/2026 para o crédito rural.",
             "citations": [{"url": "https://www.bcb.gov.br/x"}]},
            {"entity": None, "run_date": "2026-10-20", "lenses": ["regulatory"], "threat_score": 0.5,
             "narrative": "Consulta pública sobre a Resolução CMN 5330 e o crédito rural.",
             "citations": [{"url": "https://www.bcb.gov.br/x"}]}]


def test_cards_carry_consolidated_and_the_feed_keeps_it():
    cand = regulatory.nominate(_narrs(), as_of="2026-11-01")[0]
    c = regulatory.consolidated_for(cand["instrument"], _index(), as_of="2026-11-01")
    n = regulatory.build_narrative(cand, consolidated=c)
    assert n["consolidated"] == c and "Texto consolidado (BCB, versão 2.0)" in n["narrative"]
    assert regulatory.build_narrative(cand)["consolidated"] is None          # nothing tracked
    item = feed_builder._project_item(n)
    assert item["consolidated"] == c                                       # not whitelisted away


def test_handler_syncs_before_the_cards_and_attaches_consolidated(monkeypatch):
    monkeypatch.setenv("ONCA_DIGESTS_BUCKET", "digests")
    monkeypatch.setenv("ONCA_REGDOCS", "true")
    monkeypatch.setenv("ONCA_REG_LIFECYCLE_MIN_DATES", "2")
    monkeypatch.setattr(regulatory, "run_date_today", lambda: "2026-11-01")
    monkeypatch.setattr(regulatory.feature_store, "load_history", lambda *a, **k: _narrs())
    monkeypatch.setattr(regulatory, "update_acts", lambda *a, **k: {})
    monkeypatch.setattr(regulatory, "_retract_same_day", lambda *a, **k: [])
    monkeypatch.setattr(regulatory.boto3, "client", lambda *a, **k: object())
    order, written, lifecycles = [], [], {}

    def fake_sync(targets, bucket, **kw):
        order.append("sync")
        assert [t["instrument_key"] for t in targets] == ["res-cmn-5330"]    # BCB-served, no DOU link
        assert kw["budget_s"] == 120
        return {"targets": 1, "stored": [], "unchanged": 1, "skipped": 0, "deferred": 0,
                "errors": [], "index": _index()}

    def fake_write(narr, bucket, s3):
        order.append("card")
        written.append(narr)
        return f"k/{narr['id']}"

    def fake_publish(lcs, bucket, **kw):
        lifecycles.update(lcs)
        return len(lcs)

    monkeypatch.setattr(reg_documents, "sync_documents", fake_sync)
    monkeypatch.setattr(regulatory, "_write", fake_write)
    monkeypatch.setattr(regulatory, "publish_lifecycles", fake_publish)
    out = json.loads(regulatory.lambda_handler({}, None)["body"])
    assert order[0] == "sync" and "card" in order
    assert written and written[0]["consolidated"]["versao"] == "2.0"
    assert lifecycles["res-cmn-5330"]["consolidated"]["versao"] == "2.0"
    lc_card = regulatory.build_lifecycle_card(lifecycles["res-cmn-5330"])
    assert lc_card["consolidated"]["versao"] == "2.0" and "Texto consolidado" in lc_card["narrative"]
    assert out["regdocs"]["with_consolidated"] == 1


def test_handler_with_the_flag_off_fetches_nothing(monkeypatch):
    monkeypatch.setenv("ONCA_DIGESTS_BUCKET", "digests")
    monkeypatch.delenv("ONCA_REGDOCS", raising=False)
    monkeypatch.setattr(regulatory.feature_store, "load_history", lambda *a, **k: _narrs())
    monkeypatch.setattr(regulatory, "update_acts", lambda *a, **k: {})
    monkeypatch.setattr(regulatory, "_retract_same_day", lambda *a, **k: [])
    monkeypatch.setattr(regulatory.boto3, "client", lambda *a, **k: object())
    monkeypatch.setattr(reg_documents, "sync_documents", lambda *a, **k: pytest.fail("fetched"))
    monkeypatch.setattr(regulatory, "_write", lambda n, b, s: n["id"])
    monkeypatch.setattr(regulatory, "publish_lifecycles", lambda l, b, **k: len(l))
    assert json.loads(regulatory.lambda_handler({}, None)["body"])["regdocs"] is None


def test_cdk_turns_the_flag_on_for_the_regulatory_lambda():
    src = (ROOT / "infra/app.py").read_text()
    blk = src[src.index('"OncaRegulatory"'):]
    blk = blk[:blk.index("digests_bucket.grant_read_write(regulatory_fn)")]
    assert '"ONCA_REGDOCS": "true"' in blk and '"ONCA_REGDOCS_BUDGET_S": "120"' in blk
    assert "ONCA_REG_LLM" not in blk.split("environment=")[1]     # the LLM record stays off


# --------------------------------------------------------------------------------- render -------
SITE = ROOT / "src/dashboard/site"


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(SITE), **k)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    try:
        browser = pw.chromium.launch()
    except Exception as exc:  # noqa: BLE001 - no browser installed here
        pw.stop()
        pytest.skip(f"Chromium not available: {str(exc)[:80]}")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    pg = browser.new_page()
    pg.route("**/api/**", lambda r: r.fulfill(status=503, content_type="application/json", body="{}"))
    pg.goto(f"http://127.0.0.1:{srv.server_address[1]}/v3/index.html")
    pg.wait_for_function("() => !!(window.OncaCtx && window.OncaCtx.changePanel)")
    yield pg
    srv.shutdown()
    browser.close()
    pw.stop()


def test_change_fold_renders_the_consolidated_text(page):
    c = regulatory.consolidated_for("res-cmn-5330", _index(), as_of="2026-11-01")
    html = page.evaluate("(c) => window.OncaCtx.changePanel(c)", {"consolidated": c, "changes": []})
    assert "Mudança regulatória" in html and "texto consolidado" in html      # fold opens on it alone
    assert "versão 2.0" in html and "Resolução CMN nº 5.334/2026" in html
    assert "a partir de" in html and "Nova redação: art. 1º, § 9º" in html
    assert "Desde a versão anterior arquivada:</b> modifica Art. 1" in html
    assert 'href="https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?' in html


def test_change_fold_escapes_and_refuses_foreign_links(page):
    c = {"source": "BCB", "versao": "1.0", "url": "javascript:alert(1)", "revoked": True,
         "amendments": [{"act": "<img src=x onerror=alert(1)>", "detail": "x", "effective": None}], "diff": None}
    html = page.evaluate("(c) => window.OncaCtx.changePanel(c)", {"consolidated": c})
    assert "javascript:" not in html and "<img" not in html and "&lt;img" in html
    assert "revogada ou cancelada" in html
    assert page.evaluate("() => window.OncaCtx.changePanel({})") == ""      # no change intel: nothing
