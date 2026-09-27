"""Shared helpers for the brand-impersonation spike (#160). Read-only.

Same shape as ../cpo_radar/common.py (#158), but on Onça's OWN YouTube key
(Secrets Manager `signalscompetitor/onca/api-key`, field `YOUTUBE_API_KEY`), which is shared with
the live CPO radar (~510 units/day at 08:30 UTC). Every YouTube call is metered into
out/quota.json (search = 100 units, channels/playlistItems/videos = 1) and the spike refuses to
spend past HARD_CAP. The key is held in memory only: never printed, logged or written.
"""
from __future__ import annotations

import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)

WINDOW_START = "2026-08-27"   # inclusive; 30 days ending 2026-09-26
WINDOW_END = "2026-09-26"
UA = "onca-impersonation-spike/0.1 (+https://github.com/; research, read-only)"
PROFILE = os.environ.get("AWS_PROFILE", "my2027")
API = "https://www.googleapis.com/youtube/v3"
HARD_CAP = 5000               # spike ceiling (issue brief); daily key quota is 10,000
COST = {"search": 100, "channels": 1, "playlistItems": 1, "videos": 1}

_KEY: list[str] = []
_QFILE = OUT / "quota.json"


def subjects() -> list[dict]:
    return json.loads((HERE.parent / "cpo_radar" / "subjects.json").read_text(encoding="utf-8"))["products"]


def youtube_key() -> str:
    if not _KEY:
        import boto3
        sm = boto3.Session(profile_name=PROFILE).client("secretsmanager")
        doc = json.loads(sm.get_secret_value(SecretId="signalscompetitor/onca/api-key")["SecretString"])
        _KEY.append(doc.get("YOUTUBE_API_KEY", ""))
    return _KEY[0]


def _redact(s: str) -> str:
    return s.replace(_KEY[0], "<redacted>") if _KEY and _KEY[0] else s


def quota() -> dict:
    return json.loads(_QFILE.read_text()) if _QFILE.exists() else {"calls": {}, "units": 0, "log": []}


def _meter(endpoint: str, note: str) -> None:
    q = quota()
    cost = COST[endpoint]
    if q["units"] + cost > HARD_CAP:
        raise RuntimeError(f"quota cap: {q['units']}+{cost} > {HARD_CAP}")
    q["units"] += cost
    q["calls"][endpoint] = q["calls"].get(endpoint, 0) + 1
    q["log"].append({"t": dt_now(), "endpoint": endpoint, "units": cost, "note": note})
    _QFILE.write_text(json.dumps(q, ensure_ascii=False, indent=1))


def get_json(url: str, params: dict | None = None, tries: int = 5, pace: float = 0.5,
             timeout: int = 60) -> dict:
    """GET JSON with retry on DNS/connection errors and 429/5xx (WSL DNS is flaky in bulk)."""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
            time.sleep(pace)
            return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(float(e.headers.get("Retry-After") or 2 ** i))
                continue
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                detail = ""
            raise RuntimeError(_redact(f"{last} for {url.split('?')[0]}: {detail}")) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = str(e)
            time.sleep(2 ** i)
    raise RuntimeError(_redact(f"giving up on {url.split('?')[0]}: {last}"))


def yt(endpoint: str, note: str = "", **params) -> dict:
    """Metered YouTube Data API call. Meter BEFORE the call so a failure still counts."""
    _meter(endpoint, note)
    return get_json(f"{API}/{endpoint}", dict(params, key=youtube_key()))


def channels(ids: list[str], note: str = "") -> dict[str, dict]:
    out = {}
    for i in range(0, len(ids), 50):
        d = yt("channels", note, part="snippet,statistics,contentDetails,brandingSettings,status,topicDetails",
               id=",".join(ids[i:i + 50]), maxResults=50)
        for it in d.get("items", []):
            out[it["id"]] = it
    return out


def in_window(date_iso: str) -> bool:
    d = (date_iso or "")[:10]
    return WINDOW_START <= d <= WINDOW_END


def dump(name: str, obj) -> pathlib.Path:
    p = OUT / name
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def load(name: str):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def dt_now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
