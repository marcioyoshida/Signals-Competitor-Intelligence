"""#289 (ADR 029 Decision 6): universal links for the store app.

`/.well-known/apple-app-site-association` is served by the OAuth Lambda, which already answers
every `/.well-known/*` path behind its CloudFront behavior (no edge gate, no extra behavior).

Only a card link opens the app: `/c/<16 hex>` (#289) or the older `/exec?c=<16 hex>` (#170). Everything else on onssa.org stays in
the browser, in particular the web sign-in return `/exec?code=…`, which arrives from
auth.onssa.org and would otherwise be handed to the app. The `?` component scopes it by query.
The Android twin `/.well-known/assetlinks.json` (#316) lists the SHA-256 fingerprints of the
certificates that sign the app as Google Play delivers it (Play App Signing key, plus the EAS upload
key for internal installs), from `ONCA_ANDROID_CERT_SHA256` (cdk.json context
`android_cert_sha256`). Until the Play app exists it is empty and the file answers 404, so Android
never verifies a link it can't honour. The intent filter covers `/c/` only (app.json).
"""
from __future__ import annotations

import json
import os
import re
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


ANDROID_PACKAGE = "org.onssa.onca"                # app.json android.package
_FINGERPRINT = re.compile(r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")


def android_fingerprints(raw: str | None = None) -> list[str]:
    raw = os.environ.get("ONCA_ANDROID_CERT_SHA256", "") if raw is None else raw
    return [f.strip().upper() for f in raw.split(",") if _FINGERPRINT.match(f.strip().upper())]


def assetlinks(fingerprints: list[str]) -> list[dict[str, Any]]:
    return [{"relation": ["delegate_permission/common.handle_all_urls"],
             "target": {"namespace": "android_app", "package_name": ANDROID_PACKAGE,
                        "sha256_cert_fingerprints": fingerprints}}]


def assetlinks_response() -> dict[str, Any]:
    fps = android_fingerprints()
    if not fps:
        return {"status": 404, "cookies": [], "body": "[]",
                "headers": {"Content-Type": "application/json", "Cache-Control": "public, max-age=300"}}
    return {"status": 200, "cookies": [], "body": json.dumps(assetlinks(fps)),
            "headers": {"Content-Type": "application/json", "Cache-Control": "public, max-age=3600"}}
