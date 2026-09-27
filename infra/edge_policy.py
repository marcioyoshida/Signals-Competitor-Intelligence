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
    "/exec-sw.js",                    # #162 service worker (root path so it can scope /exec)
    "/v3/manifest.webmanifest",       # #162 web app manifest
    "/v3/icons/icon-192.png",
    "/v3/icons/icon-512.png",
    "/v3/icons/icon-maskable-512.png",
    "/v3/icons/apple-touch-icon.png",
    "/v3/og.png",                     # #170 generic share preview image
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
