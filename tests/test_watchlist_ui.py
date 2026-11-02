"""#302: the "Meus concorrentes" drawer on /exec (headless Chromium, 127.0.0.1 only).

Search covers only the tenant's own scoped entities, chips add/remove, and Salvar sends exactly one
``set_watchlist`` with the list, the ranking boost and the alert prefs. Rendered in both themes."""
import http.server
import os
import threading
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parents[1] / "src" / "dashboard" / "site"
SHOTS = os.environ.get("ONCA_WL_SHOTS")          # set to a directory to keep screenshots


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(SITE), **k)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    try:
        b = pw.chromium.launch()
    except Exception as exc:  # noqa: BLE001 - no browser installed here
        pw.stop()
        pytest.skip(f"Chromium not available: {str(exc)[:80]}")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield b, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    b.close()
    pw.stop()


SETUP = """(theme) => {
  document.documentElement.setAttribute("data-theme", theme);
  ADMIN = false;
  DATA = Object.assign({}, DATA, { tenant: "acme", watchlist: ["itau"], score_weights: { watchlist: 2 },
    alert_prefs: { push: true, email: false, only_watchlist: false },
    entities: [{ entity: "itau", label: "Itaú Unibanco" }, { entity: "bradesco", label: "Bradesco" },
               { entity: "banco_inter", label: "Banco Inter" }] });
  window.__acts = [];
  postAct = async (b) => { window.__acts.push(b); return { outcome: "applied", watchlist: b.args.entity_ids,
    score_weights: b.args.score_weights, alert_prefs: b.args.alert_prefs }; };
  Ctx.loadScopedFeed = async () => ({ data: DATA });
  render = () => {};
  openWatchlist();
}"""


@pytest.mark.network
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_drawer_searches_adds_removes_and_saves(browser, theme):
    b, base = browser
    pg = b.new_page(viewport={"width": 420, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.route("**/api/**", lambda r: r.fulfill(status=503, content_type="application/json", body="{}"))
    pg.goto(f"{base}/v3/index.html")
    pg.wait_for_function("() => typeof openWatchlist === 'function'")
    pg.evaluate(SETUP, theme)
    assert pg.inner_text("#drTitle") == "Meus concorrentes"
    assert "Itaú Unibanco" in pg.inner_text("#wlChips")
    assert pg.input_value("#wlBoost") == "2"
    pg.fill("#wlQ", "brad")
    assert pg.inner_text("#wlRes").startswith("Bradesco")
    pg.fill("#wlQ", "itau")                                    # already chosen: not offered again
    assert "Itaú" not in pg.inner_text("#wlRes")
    pg.fill("#wlQ", "nubank")                                  # outside the scoped feed: never offered
    assert "Nada encontrado" in pg.inner_text("#wlRes")
    pg.fill("#wlQ", "inter")
    pg.click("[data-add='banco_inter']")
    pg.click("[data-rm='itau']")
    assert pg.inner_text("#wlChips").strip().startswith("Banco Inter")
    pg.check("#wlOnly")
    if SHOTS:
        pg.screenshot(path=str(Path(SHOTS) / f"watchlist-{theme}.png"), full_page=True)
    # chips and controls stay legible: text colour differs from its background
    fg, bg = pg.evaluate("""() => { const c = getComputedStyle(document.querySelector('.wl-chip'));
        return [c.color, c.backgroundColor]; }""")
    assert fg != bg
    pg.click("#wlSave")
    pg.wait_for_function("() => document.getElementById('wlMsg').textContent === 'Salvo.'")
    acts = pg.evaluate("window.__acts")
    assert acts == [{"intent": "set_watchlist", "args": {
        "entity_ids": ["banco_inter"], "score_weights": {"watchlist": 2},
        "alert_prefs": {"push": True, "email": False, "only_watchlist": True}}}]
    assert errors == []
    pg.close()


@pytest.mark.network
def test_rows_mark_watched_entities_and_hoje_sorts_by_rank(browser):
    b, base = browser
    pg = b.new_page()
    pg.route("**/api/**", lambda r: r.fulfill(status=503, content_type="application/json", body="{}"))
    pg.goto(f"{base}/v3/index.html")
    pg.wait_for_function("() => typeof rowsCard === 'function'")
    html = pg.evaluate("""() => rowsCard([{ id: "a", entity_label: "Itaú", title: "x", watched: true, threat_score: 50 },
                                          { id: "b", entity_label: "Bradesco", title: "y", threat_score: 80 }])""")
    assert html.count("watch-star") == 1 and "Meu concorrente · Itaú" in html
    order = pg.evaluate("""() => { DATA = { feed: [
        { id: "hi", is_alert: true, threat_score: 80, rank_score: 80, date: "2099-01-01", lenses: ["product"] },
        { id: "w", is_alert: true, threat_score: 60, rank_score: 120, watched: true, date: "2099-01-01", lenses: ["product"] }] };
      SECTOR = "__all__";
      return hojeAlerts("cso", "2000-01-01T00:00:00Z").cards.map(c => c.id); }""")
    assert order == ["w", "hi"]
    pg.close()
