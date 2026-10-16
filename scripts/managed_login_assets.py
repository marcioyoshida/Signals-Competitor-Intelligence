"""Onça managed-login (auth.onssa.org) assets: the ONÇA wordmark as vector outlines (Inter 800,
so the logo never depends on the viewer's fonts), jaguar-rosette page backgrounds (light/dark) and
an SVG favicon. Writes infra/assets/managed_login/*.svg; the branding settings (colors, radii,
layout) are hand-kept in settings.json next to them. infra/app.py ships both as
CfnManagedLoginBranding. The SVGs are inlined into the CloudFormation template, so keep them small
(Cognito rejects <use>, so every rosette is inline: relative path data, one <path>).

    python3 scripts/managed_login_assets.py [path/to/InterVariable.ttf]   # needs fonttools
Inter (OFL): Debian/Ubuntu package fonts-inter-variable."""
import math, random
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen

import os, sys
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "infra", "assets", "managed_login") + "/"
FONT = sys.argv[1] if len(sys.argv) > 1 else "/usr/share/fonts/truetype/inter-vf/InterVariable.ttf"
vf = TTFont(FONT)
font = instantiateVariableFont(vf, {"wght": 800, "opsz": 32} if "opsz" in [a.axisTag for a in vf["fvar"].axes] else {"wght": 800})
gs, cmap, upm = font.getGlyphSet(), font.getBestCmap(), font["head"].unitsPerEm
asc = font["hhea"].ascent

def word(text, size, track, x0=0.0, y_base=0.0):
    """[(char, svg path d, advance)] laid out from x0 at baseline y_base."""
    s = size / upm; x = x0; out = []
    for ch in text:
        g = cmap[ord(ch)]; pen = SVGPathPen(gs)
        gs[g].draw(TransformPen(pen, (s, 0, 0, -s, x, y_base)))
        out.append((ch, pen.getCommands())); x += gs[g].width * s + track
    return out, x - track

COL = {"light": {"ink": "#1b2023", "teal": "#157a5a", "sub": "#6c6a62"},
       "dark":  {"ink": "#ecefeb", "teal": "#3ec39a", "sub": "#9aa49f"}}

# --- logo: ONÇA wordmark (Ç in brand teal), the same lockup as the app header -------------
for mode, c in COL.items():
    size, track = 64, 6
    glyphs, w = word("ONÇA", size, track, 0, 64)
    paths = "".join(f'<path fill="{c["teal"] if ch == "Ç" else c["ink"]}" d="{d}"/>' for ch, d in glyphs)
    H = 84   # room for the cedilla's tail below the baseline
    open(OUT + f"logo-{mode}.svg", "w").write(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-2 0 {w + 4:.0f} {H}" width="{w + 4:.0f}" height="{H}">{paths}</svg>')

# --- favicon: the app icon (teal Ç on near-black) ------------------------------------------
g, _ = word("Ç", 300, 0, 0, 0)
d = g[0][1]
cw = gs[cmap[ord("Ç")]].width * 300 / upm
open(OUT + "favicon.svg", "w").write(
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#0e1214"/>'
    f'<g transform="translate({(512 - cw) / 2:.0f},330)"><path fill="#3ec39a" d="{d}"/></g></svg>')

# --- page background: jaguar rosettes ------------------------------------------------------
# A rosette = a broken ring of 4-6 irregular blobs around an open centre, sometimes with a dot
# inside. Scattered with jitter on a loose grid, low contrast so the form stays the subject;
# denser/stronger toward the edges, fading behind the (centred) form.
N = 9   # points per spot


def blob(cx, cy, r, rnd, rot=None):
    """One spot as a closed, smoothed N-point outline: an absolute move, then relative quadratic
    segments (short integers — Cognito's SVG validator allows no <use>, so every spot is inline
    and its bytes count against the template)."""
    rot = math.radians(rnd.uniform(0, 360) if rot is None else rot)
    pts = []
    for k in range(N):
        a = k / N * 2 * math.pi; rr = r * (0.7 + 0.6 * rnd.random())
        x, y = rr * math.cos(a) * 1.85, rr * math.sin(a) * 0.62
        pts.append((round(cx + x * math.cos(rot) - y * math.sin(rot)), round(cy + x * math.sin(rot) + y * math.cos(rot))))
    mid = lambda i: (round((pts[i % N][0] + pts[(i + 1) % N][0]) / 2), round((pts[i % N][1] + pts[(i + 1) % N][1]) / 2))
    cur = mid(0); d = f"M{cur[0]} {cur[1]}"
    for i in range(1, N + 1):
        p, m = pts[i % N], mid(i)
        d += f"q{p[0] - cur[0]} {p[1] - cur[1]} {m[0] - cur[0]} {m[1] - cur[1]}"; cur = m
    return d + "z"

def rosette(cx, cy, R, rnd):
    """(centre-tint ellipse, path data of the broken ring of arcs [+ an inner dot])."""
    n = rnd.choice((4, 5, 5, 6)); a0 = rnd.uniform(0, 2 * math.pi)
    tint = f'<ellipse cx="{cx:.0f}" cy="{cy:.0f}" rx="{R * .78:.0f}" ry="{R * .7:.0f}"/>'
    d = ""
    for k in range(n):
        if rnd.random() < 0.12: continue                     # broken ring
        a = a0 + k / n * 2 * math.pi + rnd.uniform(-.3, .3)
        d += blob(cx + R * math.cos(a), cy + R * math.sin(a), R * rnd.uniform(.36, .48), rnd,
                  math.degrees(a) + 90 + rnd.uniform(-28, 28))   # arc along the ring
    if rnd.random() < 0.55:
        d += blob(cx + rnd.uniform(-R * .25, R * .25), cy + rnd.uniform(-R * .25, R * .25), R * .16, rnd)
    return tint, d

W, H = 1600, 1000
BG = {"light": ("#f5f3ee", "#e4dbc9", "#efe9dd"), "dark": ("#0e1214", "#1b2c2a", "#111b1b")}
for mode, (ground, spot, glow) in BG.items():
    rnd = random.Random(1872)   # stable layout across builds
    tints, d = [], ""; step = 200
    for gy in range(-step, H + step, step):
        for gx in range(-step, W + step, step):
            x = gx + (step / 2 if (gy // step) % 2 else 0) + rnd.uniform(-50, 50)
            y = gy + rnd.uniform(-45, 45)
            # fade toward the centre column where the form sits
            dx = abs(x - W / 2) / (W / 2); keep = 0.25 + 0.75 * min(1, dx * 1.6)
            R = 50 * rnd.uniform(.88, 1.24)
            if rnd.random() > keep: continue
            # skip spots the centre glow paints over anyway (bytes, not looks)
            if ((x - W / 2) / (W * .3)) ** 2 + ((y - H * .45) / (H * .3)) ** 2 < 1: continue
            t, rd = rosette(x, y, R, rnd); tints.append(t); d += rd
    open(OUT + f"bg-{mode}.svg", "w").write(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid slice">'
        f'<defs><radialGradient id="g" cx="50%" cy="45%" r="55%"><stop offset="0" stop-color="{ground}"/>'
        f'<stop offset=".55" stop-color="{ground}" stop-opacity=".85"/><stop offset="1" stop-color="{ground}" stop-opacity="0"/></radialGradient></defs>'
        f'<rect width="{W}" height="{H}" fill="{glow}"/><g fill="{spot}"><g fill-opacity=".3">{"".join(tints)}</g><path d="{d}"/></g>'
        f'<rect width="{W}" height="{H}" fill="url(#g)"/></svg>')
print("ok")
