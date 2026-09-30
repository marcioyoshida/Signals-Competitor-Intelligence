"""#163 — phone viewports as a HARD gate: no horizontal overflow, 44px tap targets.

The #128 matrix measures one page (/exec, default officer) at 480px. Real phones are narrower
(390 iPhone 13–15, 412 common Android), and three overflow bugs were found BY HAND on
2026-09-26 on views the matrix never opens (an officer tab, /v2/admin). This check walks:

    pages  : /exec × {Hoje, Painel} × each officer tab (cso/cro/cco/cpo), /v2/admin, /entry/
    widths : 390×844, 412×915   (touch, mobile UA metrics)
    themes : light, dark

and asserts, per combination, ``document.documentElement.scrollWidth <= innerWidth``. A failure
names the OUTERMOST offending elements (right edge past the viewport while the parent's is not)
so the report points at the fix, not at 800 descendants. At the phone widths it also asserts
the primary actions (officer tabs, Aprovar/Rejeitar, Perguntar) are at least 44×44 CSS px.

One browser, one context, sequential navigation — this Lambda's --single-process Chromium dies
on a second context (see checks/smoke.py). /exec is seeded with the persona's captured session.
"""
from __future__ import annotations

import json
import time

from playwright.sync_api import sync_playwright

from qa_pipeline.lib import auth, config
from qa_pipeline.lib.browser import ARTIFACTS_BUCKET, engine, launch_args, upload_artifact
from qa_pipeline.lib.checklist import Checklist

PHONES = {"390": {"width": 390, "height": 844}, "412": {"width": 412, "height": 915}}
THEMES = ("light", "dark")
OFFICERS = ("cso", "cro", "cco", "cpo")
THEME_KEY = "onca.theme.v2"          # src/dashboard/site/v2/app.js
MIN_TAP = 44
TOLERANCE = 1                         # sub-pixel rounding only

# The outermost elements whose right edge is past the viewport — each described well enough
# to find in the source (tag#id.class "text…" right=NNN).
OFFENDERS_JS = """(vw) => {
  const out = [];
  for (const el of document.body.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.right <= vw + %d) continue;
    const cs = getComputedStyle(el);
    if (cs.position === 'fixed') continue;   // viewport-relative: stretches WITH an overflow, never causes one
    const p = el.parentElement, pr = p ? p.getBoundingClientRect() : null;
    if (pr && pr.right > vw + %d) continue;           // only the outermost offender
    let clip = false;                                  // inside a horizontal scroller = fine
    for (let a = p; a && a !== document.body; a = a.parentElement) {
      const ox = getComputedStyle(a).overflowX;
      if (ox === 'auto' || ox === 'scroll' || ox === 'hidden' || ox === 'clip') { clip = true; break; }
    }
    if (clip) continue;
    const cls = (el.className && el.className.baseVal === undefined) ? String(el.className).trim().split(/\\s+/).slice(0, 2).join('.') : '';
    out.push(`${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}${cls ? '.' + cls : ''} "${(el.textContent || '').trim().slice(0, 30)}" right=${Math.round(r.right)}`);
    if (out.length >= 8) break;
  }
  return out;
}""" % (TOLERANCE, TOLERANCE)

TAPS_JS = """(min) => {
  const sel = ['#officerTabs a', '#askBtn', 'button[data-v]', 'button[data-tv]',
               '.decbtn', '.hoje-act'];
  const small = [];
  for (const s of sel) for (const el of document.querySelectorAll(s)) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;               // not rendered on this view
    if (r.width < min - 0.5 || r.height < min - 0.5)
      small.push(`${s} "${(el.textContent || '').trim().slice(0, 20)}" ${Math.round(r.width)}x${Math.round(r.height)}`);
  }
  return small.slice(0, 8);
}"""


def _measure(page, label: str, c: Checklist, *, taps: bool) -> None:
    vw = page.evaluate("() => window.innerWidth")
    sw = page.evaluate("() => document.documentElement.scrollWidth")
    offenders = [] if sw <= vw + TOLERANCE else page.evaluate(OFFENDERS_JS, vw)
    c.check(f"no_overflow:{label}", sw <= vw + TOLERANCE,
            f"scrollWidth={sw} innerWidth={vw} offenders={offenders}")
    if taps:
        small = page.evaluate(TAPS_JS, MIN_TAP)
        c.check(f"tap_targets:{label}", not small, f"under {MIN_TAP}px: {small}")


def _settle(page, *, max_ms: int = 12_000) -> None:
    """Wait until the layout stops changing (scrollWidth AND scrollHeight equal across two
    readings 600ms apart) — heavy views (/v2/admin renders ~570KB of text) keep growing well
    after `load`, and measuring early passes an overflow that appears a second later."""
    prev, waited = None, 0
    while waited < max_ms:
        cur = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.scrollHeight]")
        if cur == prev:
            return
        prev = cur
        page.wait_for_timeout(600)
        waited += 600


def _goto(page, url: str) -> None:
    page.goto(url, wait_until="load", timeout=45_000)
    page.wait_for_timeout(1200)       # boot() starts its fetches
    _settle(page)


def run(event: dict) -> dict:
    persona = event.get("persona", "admin")
    creds = config.qa_credentials()["personas"][persona]
    user, pw = config.basic_auth()
    site = config.SITE_URL
    run_id = event.get("run_id") or time.strftime("%Y%m%dT%H%M%SZ")
    c = Checklist()
    shots: list[str] = []

    # the Lambda runs Chromium; "webkit" is the local Safari-engine proxy for #200 (Linux WebKit
    # needs GTK/GStreamer libraries the Lambda image doesn't carry)
    name = event.get("browser", "chromium")
    with sync_playwright() as p:
        browser = engine(p, name).launch(headless=True, args=launch_args(name))
        # basic auth for /v2/admin and /entry; /exec itself no longer needs it (#161)
        mobile = {"is_mobile": True} if name != "firefox" else {}   # Firefox has no is_mobile
        ctx = browser.new_context(http_credentials={"username": user, "password": pw},
                                  viewport=PHONES["390"], has_touch=True,
                                  device_scale_factor=2, **mobile)
        page = ctx.new_page()
        try:
            storage = auth.login_with_password(page, f"{site}/exec", creds["username"], creds["password"])
            c.check("login", bool(storage))
        except Exception as exc:  # noqa: BLE001 - record and keep going
            c.check("login", False, str(exc))
        for wkey, vp in PHONES.items():
            page.set_viewport_size(vp)
            for theme in THEMES:
                page.evaluate("([k, t]) => localStorage.setItem(k, t)", [THEME_KEY, theme])
                # #166: phones open on "Hoje"; the full board ("Painel") must stay usable too
                for view in ("hoje", "painel"):
                    for officer in OFFICERS:
                        _goto(page, f"{site}/exec#{view}")
                        page.evaluate("(o) => { const a = document.querySelector(`#officerTabs a[data-officer=\"${o}\"]`); if (a) a.click(); }", officer)
                        page.wait_for_timeout(500)
                        _settle(page)
                        _measure(page, f"/exec#{view}[{officer}]@{wkey}/{theme}", c, taps=True)
                        if theme == "dark" and wkey == "390":
                            path = f"/tmp/phone-{name}-exec-{view}-{officer}.png"
                            page.screenshot(path=path)
                            shots.append(path)
                # /v2/admin in its real operator view (?admin=1&opkey=) — that's where the badge
                # overflow lived; without the key it only renders the login gate
                for path_, qs in (("/v2/admin/", f"?admin=1&opkey={config.operator_secret()}"),
                                  ("/entry/", "")):
                    _goto(page, f"{site}{path_}{qs}")
                    _measure(page, f"{path_}@{wkey}/{theme}", c, taps=False)
        browser.close()

    for path in shots:
        upload_artifact(ARTIFACTS_BUCKET, f"{run_id}/phone/{path.rsplit('/', 1)[-1]}", path=path)
    result = {"ok": c.all_ok, "checks": c.items, "run_id": run_id}
    upload_artifact(ARTIFACTS_BUCKET, f"{run_id}/phone-result.json", body=json.dumps(result).encode())
    c.raise_if_failed()
    return result
