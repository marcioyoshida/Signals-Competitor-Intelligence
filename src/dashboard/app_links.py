"""#289 (ADR 029 Decision 6): universal links for the store app.

`/.well-known/apple-app-site-association` is served by the OAuth Lambda, which already answers
every `/.well-known/*` path behind its CloudFront behavior (no edge gate, no extra behavior).

Only a card link opens the app: `/c/<16 hex>` (#289) or the older `/exec?c=<16 hex>` (#170). Everything else on onssa.org stays in
the browser, in particular the web sign-in return `/exec?code=…`, which arrives from
auth.onssa.org and would otherwise be handed to the app. The `?` component scopes it by query.
The Android twin (`/.well-known/assetlinks.json`) waits on Play App Signing (#253).
"""
from __future__ import annotations

import json
from typing import Any

APPLE_APP_ID = "AWSP6DBQ68.org.onssa.onca"     # Team ID . bundle id (app.json ios.bundleIdentifier)


def apple_app_site_association() -> dict[str, Any]:
    return {"applinks": {"details": [{
        "appIDs": [APPLE_APP_ID],
        "components": [
            {"/": "/c/????????????????", "comment": "a shared card (#289 path form)"},
            {"/": "/exec", "?": {"c": "????????????????"}, "comment": "a shared card (#170, older links)"},
        ],
    }]}}


def aasa_response() -> dict[str, Any]:
    return {"status": 200, "cookies": [], "body": json.dumps(apple_app_site_association()),
            "headers": {"Content-Type": "application/json", "Cache-Control": "public, max-age=3600"}}
