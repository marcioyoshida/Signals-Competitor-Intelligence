"""Store app health and version policy (#312, #318; ADR 029 / ADR 030).

`POST /api/session/app` (session broker route: same CSRF header, no token). The installed app
and `/exec` in app mode report a few events, and the reply carries the minimum supported app
version for the caller's platform.

Events (`kind`):
  start   once per app launch (the WebView's first page); the denominator of crash-free sessions
  fatal   the shell's JS crashed (recorded on the device, sent on the next launch)
  error   an uncaught page error, or an /api/* call that failed (5xx or network)
  probe   the ADR 030 canary: the policy reply, no metric

Privacy: only enums, a version, a build number and an /api route + status code are accepted.
No message text, URL query, token, email or finding content is read or logged; anything else in
the body is ignored. Metrics are CloudWatch embedded-metric log lines (`Onca/App`, dimension
`Kind`), so this path makes no API call and needs no IAM.

Version policy: `ONCA_APP_MIN_VERSIONS` = {"ios": {"soft": "1.0.0", "hard": "0.9.0"}, ...}
(cdk.json context `app_min_versions`). Below `hard` the page blocks with an update screen;
below `soft` it shows a dismissible banner. An unknown version (shells before bridge 4 don't
report one) is never hard-blocked.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

KINDS = ("start", "fatal", "error", "probe")
PLATFORMS = ("ios", "android")
STORE_URLS = {"ios": "https://apps.apple.com/app/id6821796703",
              "android": "https://play.google.com/store/apps/details?id=org.onssa.onca"}
_VERSION = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
_ROUTE = re.compile(r"^/api/[a-z0-9/_-]{1,60}$")


def parse_version(v: Any) -> tuple[int, int, int] | None:
    s = str(v or "")
    if not _VERSION.match(s):
        return None
    a, b, c = (int(x) for x in s.split("."))
    return a, b, c


def min_versions() -> dict[str, dict[str, str]]:
    try:
        raw = json.loads(os.environ.get("ONCA_APP_MIN_VERSIONS") or "{}")
    except ValueError:
        return {}
    out: dict[str, dict[str, str]] = {}
    for p in PLATFORMS:
        levels = raw.get(p) if isinstance(raw, dict) else None
        if isinstance(levels, dict):
            out[p] = {k: str(levels[k]) for k in ("soft", "hard") if parse_version(levels.get(k))}
    return out


def verdict(platform: str, version: Any, policy: dict[str, dict[str, str]] | None = None) -> str:
    """'ok' | 'soft' | 'hard' for this app. Unknown version: at most 'soft' (never strand an old shell)."""
    levels = (policy if policy is not None else min_versions()).get(platform) or {}
    have = parse_version(version)
    hard, soft = parse_version(levels.get("hard")), parse_version(levels.get("soft"))
    if have is None:
        return "soft" if soft and soft > (0, 0, 0) else "ok"
    if hard and have < hard:
        return "hard"
    if soft and have < soft:
        return "soft"
    return "ok"


def clean_event(body: dict[str, Any]) -> dict[str, Any] | None:
    kind = body.get("kind")
    if kind not in KINDS:
        return None
    platform = body.get("platform") if body.get("platform") in PLATFORMS else "other"
    ev: dict[str, Any] = {"kind": kind, "platform": platform}
    if parse_version(body.get("version")):
        ev["version"] = str(body["version"])
    for k, hi in (("build", 100_000), ("bridge", 100), ("status", 599)):
        v = body.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= hi:
            ev[k] = v
    route = str(body.get("route") or "").split("?")[0].split("#")[0]
    if _ROUTE.match(route):
        ev["route"] = route
    if body.get("source") in ("page", "api", "shell"):
        ev["source"] = body["source"]
    return ev


def emit(ev: dict[str, Any]) -> None:
    """One embedded-metric line: Onca/App AppEvents{Kind}; the other fields ride as log properties
    for Logs Insights (by version, route), not as dimensions (each dimension value is a billed metric)."""
    print(json.dumps({"_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
        "Namespace": "Onca/App", "Dimensions": [["Kind"]],
        "Metrics": [{"Name": "AppEvents", "Unit": "Count"}]}]},
        "Kind": ev["kind"], "AppEvents": 1, **{k: v for k, v in ev.items() if k != "kind"}}))


def handle(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    ev = clean_event(body)
    if ev is None:
        return 400, {"error": "unknown kind"}
    if ev["kind"] != "probe":
        emit(ev)
    if ev["kind"] in ("start", "probe"):
        p = ev["platform"]
        levels = min_versions().get(p) or {}
        return 200, {"ok": True, "update": verdict(p, ev.get("version")), "min": levels,
                     "store": STORE_URLS.get(p)}
    return 200, {"ok": True}
