"""Shared helpers for the CPO Product Radar spike (#158). Read-only, offline.

Patterns ported from Bluefin (Signals-Creator-Radar), not imported: the key resolution mirrors
`creator_radar/keys.py::youtube_key()` (Secrets Manager JSON field `YOUTUBE_KEY`), and the HTTP
helper follows ADR-0003 item 2 (contact-bearing UA, backoff, pacing). The YouTube key is held in
memory only; it is never printed, logged or written. Error messages have the key stripped.
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
WINDOW_END = "2026-09-26"     # inclusive
UA = "onca-cpo-radar-spike/0.1 (+https://github.com/; research, read-only)"
PROFILE = os.environ.get("AWS_PROFILE", "my2027")

_KEY: list[str] = []


def subjects() -> list[dict]:
    return json.loads((HERE / "subjects.json").read_text(encoding="utf-8"))["products"]


def youtube_key() -> str:
    """Same secret + field Bluefin resolves in keys.youtube_key(). Never print the return value."""
    if not _KEY:
        import boto3
        sm = boto3.Session(profile_name=PROFILE).client("secretsmanager")
        doc = json.loads(sm.get_secret_value(SecretId="creatorradar/bluefin/api-key")["SecretString"])
        _KEY.append(doc.get("YOUTUBE_KEY", ""))
    return _KEY[0]


def _redact(s: str) -> str:
    return s.replace(_KEY[0], "<redacted>") if _KEY and _KEY[0] else s


def get_json(url: str, params: dict | None = None, tries: int = 5, pace: float = 0.4,
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
