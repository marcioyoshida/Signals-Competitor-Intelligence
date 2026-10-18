"""What the CloudFront basic-auth function lets through WITHOUT the shared password (#161).

`/exec` (the officer suite) is signed in with each officer's own Cognito account, so the shared
prototype password in front of it adds no per-person security — and on a phone it is a native
password dialog before the real login, which an installed PWA can't get past at all.

The exemption is an EXACT set of object keys (checked against the URI *after* the clean-route
rewrite, so `/exec` → `/v3/index.html`): the page shell and the static files it loads. None of
them carries data — the page renders `Ctx.mountGate` until a verified JWT fetches the scoped
feed from `/api/feed`. `tests/test_edge_policy.py` pins this set, so widening it (a feed, an
`/api/*` path, a `/v2/*` page) fails CI instead of silently publishing it.
"""
from __future__ import annotations

EXEC_PUBLIC_URIS: tuple[str, ...] = (
    "/v3/index.html",                 # /exec and /executivo after the rewrite
    "/v2/app.css",                    # shared design tokens
    "/v2/app.js",                     # OncaUI helpers
    "/v2/context.js",                 # Ctx: Cognito PKCE login + scoped feed loader
    "/v2/signin.css",                 # #277 own sign-in form (attached by context.js)
    "/exec-sw.js",                    # #162 service worker (root path so it can scope /exec)
    "/v3/manifest.webmanifest",       # #162 web app manifest
    "/v3/icons/icon-192.png",
    "/v3/icons/icon-512.png",
    "/v3/icons/icon-maskable-512.png",
    "/v3/icons/apple-touch-icon.png",
    "/v3/og.png",                     # #170 generic share preview image
    # #221: the branded error pages (static, no data) — the public /docs and /sample behaviors
    # redirect unknown paths to /404.html, and a visitor there has no shared password.
    "/404.html",
    "/403.html",
)


def exec_exemption_js() -> str:
    """The CloudFront Function snippet: return the request untouched (no basic auth) when the
    rewritten URI is one of EXEC_PUBLIC_URIS. Exact-match only — no prefixes."""
    entries = ", ".join(f'"{u}": 1' for u in EXEC_PUBLIC_URIS)
    return (
        "  // #161: /exec signs in with Cognito per officer; its shell + static assets skip the\n"
        "  // shared basic-auth prompt (exact keys only — infra/edge_policy.py, pinned by tests).\n"
        f"  var execPublic = {{ {entries} }};\n"
        "  if (execPublic[r.uri] === 1) { return r; }\n"
    )


# --- #216: directory redirects + branded 404 (default behavior only) --------------------------
# S3 behind OAC answers a missing key with a raw `<Error><Code>AccessDenied</Code>` XML 403. A
# distribution-wide custom error response would also rewrite the /api/* Lambdas' own 403/404
# JSON, so this is handled in the viewer-request function instead, from the site's real layout
# (computed at synth): an extensionless path that names a directory with an index.html gets a
# 301 to its trailing-slash form; any other extensionless path, or a directory without an index
# (`/v2/`), goes to /404.html. Paths with a file extension are left to S3.
NOT_FOUND_PAGE = "/404.html"


def site_index_dirs(site_root) -> list[str]:
    """Every directory (as '/a/b/') under the site that serves an index.html, root included."""
    from pathlib import Path

    root = Path(site_root)
    out = []
    for p in sorted(root.rglob("index.html")):
        rel = p.parent.relative_to(root).as_posix()
        out.append("/" if rel == "." else f"/{rel}/")
    return out


def site_files(site_root) -> list[str]:
    """Every static file the site deploys, as '/path' (#220). The bucket holds exactly these plus
    the pipeline-written feed*.json, which is why .json is exempt from the unknown-file rule."""
    from pathlib import Path

    root = Path(site_root)
    return sorted("/" + p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


FAVICON_TARGET = "/v3/icons/icon-192.png"


# #282: the apex is the operator war-room (v1) behind the shared password, so a stranger at
# onssa.org/ got a native password dialog and a blank page. A request WITHOUT the password is
# sent to the public sales page; extensionless aliases of public pages and the favicon never
# hit the challenge. The operator still reaches the war-room at /index.html (challenged).
APEX_PUBLIC_TARGET = "/pricing.html"
PUBLIC_ALIASES: dict[str, str] = {
    "/pricing": "/pricing.html",
    "/precos": "/pricing.html",
    "/privacidade": "/docs/privacy.html",
    "/privacy": "/docs/privacy.html",
    "/favicon.ico": FAVICON_TARGET,
}


def apex_public_js() -> str:
    """Snippet run BEFORE the basic-auth check (needs `r`, `h`, `key`, `expected` in scope)."""
    entries = ", ".join(f'"{k}": "{v}"' for k, v in PUBLIC_ALIASES.items())
    return (
        "  // #282: public aliases + the apex for visitors without the shared password\n"
        "  // (infra/edge_policy.py apex_public_js).\n"
        f"  var pubAlias = {{ {entries} }};\n"
        "  if (pubAlias[key]) { return { statusCode: 301, statusDescription: 'Moved Permanently',\n"
        "    headers: { location: { value: pubAlias[key] } } }; }\n"
        '  if (key === "/" && (!h.authorization || h.authorization.value !== expected)) {\n'
        "    return { statusCode: 302, statusDescription: 'Found',\n"
        f"      headers: {{ location: {{ value: \"{APEX_PUBLIC_TARGET}\" }}, 'cache-control': {{ value: 'no-store' }} }} }};\n"
        "  }\n"
    )


def site_paths_js(site_root) -> str:
    """Snippet: sets `redir` (a Location) for a directory-without-slash or an unknown path.
    Needs `r`, `key` and `routes` in scope; must run on the RAW uri, BEFORE the clean-route and
    trailing-slash rewrites (clean routes and /exec are skipped); the caller returns the
    redirect only after basic auth, so an unauthenticated probe still gets the 401."""
    entries = ", ".join(f'"{d}": 1' for d in site_index_dirs(site_root))
    files = ", ".join(f'"{f}": 1' for f in site_files(site_root))
    return (
        "  // #216: directory redirects + branded 404 (infra/edge_policy.py site_paths_js).\n"
        f"  var siteDirs = {{ {entries} }};\n"
        "  // #220: the real static files; any other path WITH an extension (not .json — the\n"
        "  // pipeline writes feed*.json) used to get S3's raw AccessDenied XML.\n"
        f"  var siteFiles = {{ {files} }};\n"
        "  var redir = null;\n"
        "  function qs(q) { var o = []; for (var k in q) { o.push(k + (q[k].value !== '' ? '=' + q[k].value : '')); }\n"
        "    return o.length ? '?' + o.join('&') : ''; }\n"
        "  var ru = r.uri;\n"
        '  if (ru.indexOf("/api/") !== 0 && !routes[key] && key !== "/exec" && key !== "/executivo") {\n'
        '    var last = ru.substring(ru.lastIndexOf("/") + 1);\n'
        "    // /v2 has no index of its own: send it to the profile-driven router, not the 404.\n"
        '    if (ru === "/v2" || ru === "/v2/") { redir = "/v2/app/" + qs(r.querystring || {}); }\n'
        '    else if (ru.charAt(ru.length - 1) === "/") {\n'
        f'      if (!siteDirs[ru]) {{ redir = "{NOT_FOUND_PAGE}"; }}\n'
        '    } else if (last.indexOf(".") === -1) {\n'
        f'      redir = siteDirs[ru + "/"] ? ru + "/" + qs(r.querystring || {{}}) : "{NOT_FOUND_PAGE}";\n'
        f'    }} else if (ru === "/favicon.ico") {{ redir = "{FAVICON_TARGET}";\n'
        '    } else if (!siteFiles[ru] && last.substring(last.lastIndexOf(".")) !== ".json") {\n'
        f'      redir = "{NOT_FOUND_PAGE}";\n'
        "    }\n"
        "  }\n"
    )


def public_paths_js(site_root, prefixes=("/docs", "/sample")) -> str:
    """#221: the auth-free public function (/docs*, /sample*) had only the index rewrite, so an
    unknown public path got S3's raw AccessDenied XML. Same rule as site_paths_js, scoped to the
    public prefixes: a real file passes; an extensionless path whose `.html` exists gets a 301 to
    it (/docs/privacy -> /docs/privacy.html); anything else under the prefixes -> /404.html.
    Needs `r` in scope; returns early with the redirect itself (no auth step on this side)."""
    pre = tuple(prefixes)
    # the root discovery .txt files ride the same function on the public "/*.txt" behavior:
    # a real one passes, any other *.txt gets the branded 404 instead of S3's raw 403.
    files = [f for f in site_files(site_root)
             if f.startswith(tuple(p + "/" for p in pre)) or f.endswith(".txt")]
    entries = ", ".join(f'"{f}": 1' for f in files)
    conds = " || ".join([f'u === "{p}" || u.indexOf("{p}/") === 0' for p in pre]
                        + ['u.slice(-4) === ".txt"'])
    return (
        "  // #221: unknown public paths -> branded 404 (infra/edge_policy.py public_paths_js).\n"
        f"  var pubFiles = {{ {entries} }};\n"
        "  var u = r.uri;\n"
        f"  if (({conds}) && !pubFiles[u]) {{\n"
        '    var loc = pubFiles[u + ".html"] ? u + ".html" : "' + NOT_FOUND_PAGE + '";\n'
        "    return { statusCode: loc === \"" + NOT_FOUND_PAGE + "\" ? 302 : 301, statusDescription: 'Redirect',\n"
        "      headers: { location: { value: loc } } };\n"
        "  }\n"
    )
