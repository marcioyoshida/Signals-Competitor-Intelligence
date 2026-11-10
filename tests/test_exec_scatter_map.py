"""Mapa Competitivo (/exec CSO): zoom, hover readout, no redundant arrows (owner ask 2026-11-08)."""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "src" / "dashboard" / "site" / "v3" / "index.html").read_text(encoding="utf-8")
JS = PAGE[PAGE.index("const SC = { seq: 0, maps: {} };"):PAGE.index("// Threat timeline (v1 → CSO)")]


def test_the_redundant_delta_arrows_are_gone():
    # momentum IS recent − prior, so a prior→recent arrow only restated the x-axis sign.
    assert "sc-whisker" not in PAGE and "traço = variação" not in PAGE
    assert "momentum = ameaça média dos últimos 7 dias − 7 dias anteriores" in JS


def test_zoom_rerenders_in_data_space_with_controls():
    for k in ('data-sc-zoom="in"', 'data-sc-zoom="out"', 'data-sc-zoom="reset"'):
        assert k in JS
    assert "function scZoom(" in JS and "function scClamp(" in JS
    assert 'root.querySelector(".sc-plot").innerHTML = scSvg(' in JS        # not a CSS transform
    # a plain wheel keeps scrolling the page; only Ctrl/⌘ (and trackpad pinch) zooms
    assert "if (!(e.ctrlKey || e.metaKey)) return;" in JS and "{ passive: false }" in JS
    assert "clip-path=\"url(#${clip})\"" in JS                               # dots clipped to the frame


def test_hover_and_tap_show_the_measurements():
    assert '<div class="sc-tip" hidden></div>' in JS
    assert ".sc-tip[hidden] { display:none; }" in PAGE                      # a display:flex class beats [hidden]
    for field in ("Ameaça <strong>", "semana anterior", "Momentum <strong", "Ativos R$", "sem dado (IF.data/CVM)"):
        assert field in JS, field
    assert "function scCross(" in JS and "sc-bx" in JS and "sc-by" in JS    # axis read-outs
    assert "touch has no hover" in JS                                       # tap = same readout


def test_axes_carry_tick_values_and_all_four_quadrants_are_named():
    assert "function scTicks(" in JS and 'class="sc-tick"' in JS
    for q in ("ameaça alta · acelerando", "ameaça alta · recuando", "ameaça baixa · acelerando", "ameaça baixa · recuando"):
        assert q in JS, q


def test_trails_are_real_trajectories_from_last_weeks_position():
    # (prior_momentum, prior) -> (momentum, recent): drawn only when the feed carries the history.
    assert "x(d.prior_momentum), py = y(d.prior)" in JS and 'class="sc-trail${focus' in JS
    assert "d.prior_momentum != null" in JS and "list.some(d => d.prior_momentum != null)" in JS
    assert "data-sc-trail" in JS and "rastro = posição há uma semana" in JS and "Há uma semana:" in JS


# --- #376: collision layout, pull-aside, cluster zoom, size key, focused trails ----------------
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

import pytest  # noqa: E402


def _fn(name):
    """Source of a top-level `function name(` in the map JS (brace-matched)."""
    i = JS.index(f"function {name}(")
    j = JS.index("{", i)
    depth = 0
    for k in range(j, len(JS)):
        depth += JS[k] == "{"
        depth -= JS[k] == "}"
        if depth == 0:
            return JS[i:k + 1]
    raise AssertionError(name)


def test_trails_only_for_focus_and_top_movers_with_halo():
    assert ".slice(0, full ? 5 : 8)" in JS                 # the biggest movers, not every entity
    assert 'class="sc-halo"' in JS and "sc-trail--focus" in JS
    assert "${trails}${anchors}${dots}${focusTrail}${labels}" in JS  # focused trail above the dots


def test_tooltip_and_crosshair_read_the_anchor_not_the_nudged_bubble():
    assert 'data-ax="' in JS and 'data-ay="' in JS
    assert 'Number(el.getAttribute("data-ax")), Number(el.getAttribute("data-ay"))' in JS


def test_pull_aside_springs_back_and_never_persists():
    assert "pull-aside" in JS and "sc-return" in JS and 'n.style.transform = ""' in JS
    assert "localStorage" not in JS                         # nothing about positions is saved
    assert ".sc-return { transition:transform" in PAGE and "prefers-reduced-motion" in PAGE
    assert "swallowClick" in JS                             # the release does not also count as a tap


def test_cluster_zoom_button_and_real_size_key():
    assert "function scClusterBtns(" in JS and "⊕ grupo de" in JS and "data-sc-cluster" in JS
    assert "function scSizeLegend(" in JS and "function scFitKey(" in JS
    assert "sem dado" in JS and "R$ ${v.toLocaleString" in JS


NODE = shutil.which("node")


def _run(js):
    out = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_layout_separates_a_stacked_cluster_deterministically():
    js = "const SC={};" + _fn("scLayout") + r"""
      const box = {padL: 50, padT: 18, iw: 652, ih: 328};
      const list = Array.from({length: 30}, (_, k) => ({momentum: (k % 5) * 0.05, recent: 50 + (k % 3) * 0.1, size_bi: (k % 7) * 300}));
      const x = m => box.padL + (m + 25) / 50 * box.iw, y = a => box.padT + (1 - a / 100) * box.ih;
      const rFor = d => 5 + Math.sqrt((d.size_bi || 0) / 1800) * 19;
      const painted = list.map((d, i) => [d, i]);
      const st1 = {view: {x0: -25, x1: 25, y0: 0, y1: 100}}, st2 = {view: {x0: -25, x1: 25, y0: 0, y1: 100}};
      const L1 = scLayout(st1, painted, x, y, rFor, box), L2 = scLayout(st2, painted, x, y, rFor, box);
      let worst = 0, outside = 0, far = 0;
      for (let i = 0; i < list.length; i++) {
        const a = L1.pos[i], ri = rFor(list[i]);
        if (a.x - ri < box.padL - 0.01 || a.x + ri > box.padL + box.iw + 0.01 || a.y - ri < box.padT - 0.01 || a.y + ri > box.padT + box.ih + 0.01) outside++;
        far = Math.max(far, Math.hypot(a.x - x(list[i].momentum), a.y - y(list[i].recent)));
        for (let j = i + 1; j < list.length; j++) {
          const b = L1.pos[j]; worst = Math.max(worst, ri + rFor(list[j]) - Math.hypot(a.x - b.x, a.y - b.y));
        }
      }
      console.log(JSON.stringify({worst, outside, far, same: JSON.stringify(L1.pos) === JSON.stringify(L2.pos), clusters: L1.clusters.length, cached: scLayout(st1, painted, x, y, rFor, box) === L1}));
    """
    r = _run(js)
    assert r["worst"] < 0.5, r                  # no visible overlap left
    assert r["outside"] == 0 and r["same"] and r["cached"]
    assert r["clusters"] >= 1                   # a stacked group earns a "zoom here" button
    assert r["far"] < 200                       # displacement stays local to the anchor


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_labels_never_sit_on_another_bubble():
    js = _fn("scSegHitsCircle") + _fn("scPlaceLabels") + r"""
      const bubbles = [{i: 0, p: {x: 200, y: 150}, r: 20}, {i: 1, p: {x: 245, y: 150}, r: 20}, {i: 2, p: {x: 222, y: 112}, r: 14},
                       {i: 3, p: {x: 222, y: 190}, r: 14}, {i: 4, p: {x: 160, y: 150}, r: 12}];
      const cands = bubbles.map(b => ({...b, text: "Instituição " + b.i}));
      const out = scPlaceLabels(cands, bubbles, {x0: 50, y0: 18, x1: 700, y1: 346});
      const bad = [];
      for (const l of out) {
        const w = l.text.length * 6.1, bx = l.anchor === "start" ? l.x : l.anchor === "end" ? l.x - w : l.x - w / 2, by = l.y - 10;
        for (const o of bubbles) { if (o.i === l.i) continue;
          const nx = Math.max(bx, Math.min(o.p.x, bx + w)), ny = Math.max(by, Math.min(o.p.y, by + 12));
          if ((nx - o.p.x) ** 2 + (ny - o.p.y) ** 2 < o.r ** 2) bad.push([l.i, o.i]); }
      }
      console.log(JSON.stringify({placed: out.length, bad}));
    """
    r = _run(js)
    assert r["bad"] == [] and r["placed"] >= 3, r

