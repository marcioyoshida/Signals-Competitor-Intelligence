"""CPO Product Radar (#159) — daily competitor product-change + app-quality radar.

Productionised from the #158 spike (``scripts/spikes/cpo_radar/``, findings in
``docs/2026-09-26-cpo-product-radar-spike.md``: narrow GO, 3/5). Two sources, one classifier,
one baseline:

* **Apple customer-reviews RSS** (public, keyless) — ``sortby=mostrecent``, 50/page, hard cap of
  10 pages (the 500 most recent). Pulled DAILY and paginated only until it overlaps what the
  previous run already stored, so a high-volume app (PicPay hit the cap in 29 days) never loses
  a day to the cap. A run that still reaches page 10 without overlapping is recorded as
  ``cap_hit`` — a truncated day is a finding, not a silent gap. Reviewer names are never stored.
* **YouTube Data API v3** (optional, own key) — official-channel uploads (``playlistItems``,
  1 unit/page) plus creator search (``search``, 100 units/page, ``regionCode=BR``,
  ``relevanceLanguage=pt``) over the last ``ONCA_CPO_YT_LOOKBACK_DAYS``, then ``videos``
  hydration. A hard unit budget (``ONCA_CPO_YT_QUOTA``, default 1,000/day) is enforced BEFORE
  each call. The key comes from ``ONCA_YOUTUBE_API_KEY`` or the ``YOUTUBE_API_KEY`` field of
  the ``signalscompetitor/onca/api-key`` secret, resolved once per container; with no key the
  leg is skipped and the output says ``youtube: "disabled: no key"``. The key is never logged.
* **Nova Lite classification** in Bluefin's ``analyze.py`` shape (relevant / event / sentiment /
  feature / why, with official-account anchors), plus the three fixes the spike called for:
  ``is_new_change`` (a dated change vs an evergreen tutorial or brand video — the spike's
  ``feature`` tag had single-digit precision without it), a pt-BR gate (deterministic language
  pre-filter + the model's ``pt_br`` flag: the spike leaked Spanish MX/AR Mercado Pago videos),
  and clustering of several creators' videos into ONE event before it reaches a card.
* **Per-product baseline** — daily App Store counts (``low_star`` ≤2★, deterministic; ``outage``
  and ``complaint``, classified) against the product's own prior 30 days. Alert when count ≥10
  AND z ≥3, carrying the dominant app version and the top error strings (the Inter v26.16
  AL-903 login outage of 2026-09-23 is the reference case: 49 low-star reviews vs 1–9/day).

Store (digests bucket)::

    product_radar/latest.json        the current radar (events + alerts + source status)
    product_radar/{YYYY-MM-DD}.json  that day's snapshot of latest.json (pruned after 30 days —
                                     YouTube API data must not be stored beyond 30 days)
    product_radar/state.json         rolling classified mentions (reviews 35 d, videos 30 d):
                                     the dedup set, the classification cache and the baseline

Standard library + boto3 only. ``lambda_handler`` event options (all optional):
``{"products": ["inter"], "local_out": "/path", "youtube": false, "today": "2026-09-26"}``.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import statistics
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from typing import Any, Callable

PREFIX = "product_radar/"
LATEST_KEY = PREFIX + "latest.json"
STATE_KEY = PREFIX + "state.json"
_DATED_RE = re.compile(r"^product_radar/(\d{4}-\d{2}-\d{2})\.json$")
SUBJECTS_PATH = pathlib.Path(__file__).with_name("cpo_radar_subjects.json")
_SECRET_ID = "signalscompetitor/onca/api-key"
UA = "onca-cpo-radar/1.0 (competitor product monitoring; read-only)"

RSS_URL = "https://itunes.apple.com/br/rss/customerreviews/page={page}/id={app}/sortby=mostrecent/json"
# Fallbacks when page 1 stays empty after retries. From the Lambda, Apple's edge served Neon an
# EMPTY page 1 on every run 09-27..09-29 (Santander 09-28/29) while the same URL returned 50
# reviews from elsewhere: a stale edge answer, so ask for the same feed under another cache key.
RSS_URL_FALLBACKS = (
    RSS_URL + "?nc={nonce}",
    "https://itunes.apple.com/rss/customerreviews/page={page}/id={app}/sortby=mostrecent/json?cc=br&nc={nonce}",
)
LOOKUP_URL = "https://itunes.apple.com/lookup"
STORE_URL = "https://apps.apple.com/br/app/id{app}?see-all=reviews"
RSS_MAX_PAGES = 10            # Apple's hard cap: 10 x 50 = the 500 most recent reviews
EMPTY_FIRST_PAGE_RETRIES = 2
EMPTY_RETRY_PAUSE_S = 3.0
YT_API = "https://www.googleapis.com/youtube/v3"
YT_COST = {"playlistItems": 1, "search": 100, "videos": 1}

MODEL_IDS = ("amazon.nova-lite-v1:0", "us.amazon.nova-lite-v1:0")
BATCH = 10
WORKERS = 4
# "corporate" (M&A, deal rumours, funding, earnings, executive/legal news) is a DECLARED sink, not a
# surfaced type: without it the classifier forced a Nubank-buys-Monzo rumour into "launch" (live
# 2026-09-26). Corporate news belongs to the main feed's lenses, not the CPO product radar.
EVENTS = ("launch", "feature", "price", "outage", "complaint", "praise", "corporate", "other")
SURFACE_EVENTS = {"launch", "feature", "price", "outage", "complaint"}
ALERT_METRICS = ("outage", "complaint")
FALLBACK_METRIC = "low_star"   # ≤2★, deterministic: a trigger only when the classifier was down
ALERT_MIN_COUNT = 10
ALERT_Z = 3.0
BASELINE_DAYS = 30
MIN_BASELINE_DAYS = 7
REVIEW_KEEP_DAYS = 35         # baseline (30) + evaluation lookback slack
YT_KEEP_DAYS = 30             # YouTube API Services Terms: delete stored API data within 30 days
EVENT_WINDOW_DAYS = 14        # events/alerts carried in latest.json
CLUSTER_SPAN_DAYS = 10
STORY_SPAN_DAYS = 2

BRT = dt.timezone(dt.timedelta(hours=-3))   # Brazil has had no DST since 2019

TYPE_LABEL = {"launch": "lançamento", "feature": "funcionalidade", "price": "preço/benefício",
              "outage": "instabilidade", "complaint": "reclamação"}


# ---------------------------------------------------------------------------------------------
# config + secrets
# ---------------------------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def load_seed_subjects(path: pathlib.Path | str | None = None) -> list[dict[str, Any]]:
    """The checked-in SEED file (cpo_radar_subjects.json). Seed-only: the registry is the
    source of truth — see seed_registry() and load_subjects()."""
    return json.loads(pathlib.Path(path or SUBJECTS_PATH).read_text(encoding="utf-8"))["products"]


#: Where the last load_subjects() call got its subjects ("registry" | "seed_fallback" | "seed").
SUBJECTS_SOURCE: dict[str, str] = {"v": "seed"}


def load_subjects(path: pathlib.Path | str | None = None, *, table: Any | None = None) -> list[dict[str, Any]]:
    """Radar subjects from the entity registry (``product_radar`` on each entity). An explicit
    ``path`` reads that file. With no registry configured (tests, local runs) → the seed file.
    If the registry is configured but unreachable or has no subjects, fall back to the seed
    file LOUDLY (printed + recorded in the output's sources) rather than run an empty radar."""
    if path is not None:
        SUBJECTS_SOURCE["v"] = "seed"
        return load_seed_subjects(path)
    if table is None and not os.environ.get("ONCA_ENTITIES_TABLE"):
        SUBJECTS_SOURCE["v"] = "seed"
        return load_seed_subjects()
    try:
        from src.synth import entity_registry

        subs = entity_registry.list_product_radar_subjects(table=table)
        if subs:
            SUBJECTS_SOURCE["v"] = "registry"
            return subs
        print("Warning: registry has no product_radar subjects; using the seed file")
    except Exception as exc:  # registry unreachable → still run, but say so
        print(f"Warning: registry subjects unavailable ({exc.__class__.__name__}); using the seed file")
    SUBJECTS_SOURCE["v"] = "seed_fallback"
    return load_seed_subjects()


def seed_registry(*, table: Any | None = None, source: str = "seed:cpo_radar_subjects.json",
                  path: pathlib.Path | str | None = None) -> dict[str, bool]:
    """Write the seed file's subjects onto their entities (idempotent; returns changed per id).
    Run once to migrate:  python -m src.ingest.cpo_radar --seed-registry"""
    from src.synth import entity_registry

    out: dict[str, bool] = {}
    for s in load_seed_subjects(path):
        ents = s.get("onca_entities") or [s["id"]]
        cfg = {k: s.get(k) for k in ("name", "aliases", "search_query", "namesake_risk",
                                     "apple_app_ids", "youtube_channels")}
        cfg["related_entities"] = list(ents[1:])
        out[ents[0]] = entity_registry.set_product_radar(ents[0], cfg, source=source, table=table)
    return out


_YT_KEY: dict[str, str | None] = {}
_REDACT: list[str] = []


def youtube_key(*, secrets_client: Any = None) -> str | None:
    """ONCA_YOUTUBE_API_KEY env, else ``YOUTUBE_API_KEY`` in the Onça api-key secret.
    Resolved ONCE per container; None when absent. The value is never printed or written."""
    if "v" in _YT_KEY:
        return _YT_KEY["v"]
    key = os.environ.get("ONCA_YOUTUBE_API_KEY") or None
    if not key:
        try:
            if secrets_client is None:
                import boto3

                secrets_client = boto3.client("secretsmanager")
            raw = secrets_client.get_secret_value(SecretId=_SECRET_ID)["SecretString"]
            key = (json.loads(raw) or {}).get("YOUTUBE_API_KEY") or None
        except Exception as exc:  # secret unavailable (locally / no grant) → leg disabled
            print(f"Warning: YouTube key lookup failed ({exc.__class__.__name__}); leg disabled")
            key = None
    if key:
        _REDACT.append(key)
    _YT_KEY["v"] = key
    return key


def _redact(s: str) -> str:
    for k in _REDACT:
        if k:
            s = s.replace(k, "<redacted>")
    return s


# ---------------------------------------------------------------------------------------------
# HTTP (ported from the spike's common.get_json: contact UA, backoff on 429/5xx + DNS errors)
# ---------------------------------------------------------------------------------------------

Fetch = Callable[[str, "dict[str, Any] | None"], Any]


def get_json(url: str, params: dict[str, Any] | None = None, *, tries: int = 4, pace: float = 0.4,
             timeout: int = 30) -> Any:
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    base = url.split("?")[0]
    last = ""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
            if pace:
                time.sleep(pace)
            return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (429, 500, 502, 503, 504) and i + 1 < tries:
                time.sleep(float(e.headers.get("Retry-After") or 2 ** i))
                continue
            raise RuntimeError(_redact(f"{last} for {base}")) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError) as e:
            last = e.__class__.__name__
            if i + 1 < tries:
                time.sleep(2 ** i)
    raise RuntimeError(_redact(f"giving up on {base}: {last}"))


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------

def fold(s: Any) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s.lower()).strip()


def brt_day(ts: str | None) -> str:
    """ISO timestamp (Apple: Pacific offset; YouTube: Z) → the BRT calendar day."""
    if not ts:
        return ""
    try:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is None:
            return d.date().isoformat()
        return d.astimezone(BRT).date().isoformat()
    except ValueError:
        return str(ts)[:10]


def _days_back(today: dt.date, n: int) -> str:
    return (today - dt.timedelta(days=n)).isoformat()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------------------------
# Apple customer-reviews RSS
# ---------------------------------------------------------------------------------------------

def parse_rss_page(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """One RSS JSON page → review rows. Page 1 may lead with the app entry (no im:rating); a
    single-review page is a dict, not a list. The author is deliberately dropped (ADR-0003 #3)."""
    entries = ((doc or {}).get("feed") or {}).get("entry") or []
    if isinstance(entries, dict):
        entries = [entries]
    rows = []
    for e in entries:
        if not isinstance(e, dict) or "im:rating" not in e:
            continue
        try:
            ts = e["updated"]["label"]
            rows.append({"id": str(e["id"]["label"]), "ts": ts, "date": brt_day(ts),
                         "rating": int(e["im:rating"]["label"]),
                         "version": (e.get("im:version") or {}).get("label"),
                         "title": str((e.get("title") or {}).get("label") or "")[:140],
                         "text": str((e.get("content") or {}).get("label") or "")[:500]})
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def _flag(sources: dict[str, str], key: str, msg: str) -> None:
    """Accumulate "partial: a; b" instead of overwriting the previous app's problem."""
    cur = sources.get(key) or "ok"
    sources[key] = ("partial: " + msg) if cur == "ok" else (cur + "; " + msg if msg not in cur else cur)


def pull_reviews(app_id: str, *, seen: set[str], since: str, fetch: Fetch | None = None,
                 max_pages: int = RSS_MAX_PAGES) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Newest-first pagination that stops as soon as it reaches what we already hold:
    an empty page, a page containing an already-seen review, or a page whose oldest review
    predates ``since``. Returns (new rows, coverage). ``cap_hit`` = page ``max_pages`` was
    consumed without reaching the overlap, i.e. reviews between runs were lost to Apple's cap."""
    fetch = fetch or (lambda u, p=None: get_json(u, p, pace=1.0))
    url, via, nonce = RSS_URL, 0, int(time.time())
    new: list[dict[str, Any]] = []
    pages, stop_reason, error = 0, None, None
    for page in range(1, max_pages + 1):
        try:
            doc = fetch(url.format(page=page, app=app_id, nonce=nonce), None)
        except Exception as exc:  # noqa: BLE001 — a dead page ends the pull, never the run
            error = str(exc)[:160]
            stop_reason = "error"
            break
        rows = parse_rss_page(doc)
        if not rows and page == 1:
            # Apple's legacy RSS intermittently serves an EMPTY first page for an app that has
            # reviews (live 2026-09-26: Neon page 1 empty on Lambda, 50 entries seconds later).
            # An empty page 1 is suspect, not an answer: retry before concluding "no reviews".
            for _ in range(EMPTY_FIRST_PAGE_RETRIES):
                time.sleep(EMPTY_RETRY_PAUSE_S)
                try:
                    rows = parse_rss_page(fetch(url.format(page=page, app=app_id, nonce=nonce), None))
                except Exception:  # noqa: BLE001
                    rows = []
                if rows:
                    break
            for i, alt in enumerate(RSS_URL_FALLBACKS if not rows else ()):
                try:
                    rows = parse_rss_page(fetch(alt.format(page=page, app=app_id, nonce=nonce), None))
                except Exception:  # noqa: BLE001
                    rows = []
                if rows:
                    url, via = alt, i + 1        # keep paginating on the variant that answered
                    print(f"cpo_radar: app {app_id} page 1 empty on the primary RSS URL; fallback {via} answered")
                    break
            if not rows:
                stop_reason = "empty_first_page"
                break
        if not rows:
            stop_reason = "empty"
            break
        pages += 1
        overlap = False
        for r in rows:
            if r["id"] in seen:
                overlap = True
                continue
            if r["date"] >= since:
                new.append(r)
        if overlap:
            stop_reason = "overlap"
            break
        if min(r["date"] for r in rows) < since:
            stop_reason = "since"
            break
    cap_hit = stop_reason is None and pages >= max_pages
    dates = sorted(r["date"] for r in new)
    return new, {"pages": pages, "new": len(new), "stop": stop_reason or ("cap" if cap_hit else None),
                 "cap_hit": cap_hit, "oldest_new": dates[0] if dates else None,
                 "newest_new": dates[-1] if dates else None, "error": error, "via": via}


def app_lookup(app_id: str, *, fetch: Fetch | None = None) -> dict[str, Any]:
    fetch = fetch or (lambda u, p=None: get_json(u, p))
    try:
        res = (fetch(LOOKUP_URL, {"id": app_id, "country": "br"}) or {}).get("results") or []
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:160]}
    if not res:
        return {}
    x = res[0]
    return {"version": x.get("version"), "version_date": x.get("currentVersionReleaseDate"),
            "release_notes": str(x.get("releaseNotes") or "")[:600] or None}


# ---------------------------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------------------------

class QuotaExceeded(Exception):
    pass


class YouTubeClient:
    """Unit-metered client: a call that would exceed ``budget`` raises BEFORE it is made."""

    def __init__(self, key: str, *, budget: int = 1000, fetch: Fetch | None = None):
        self.key, self.budget = key, budget
        self.used: Counter = Counter()
        self._fetch = fetch or (lambda u, p=None: get_json(u, p))

    @property
    def units(self) -> int:
        return sum(self.used.values())

    def call(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        cost = YT_COST[endpoint]
        if self.units + cost > self.budget:
            raise QuotaExceeded(endpoint)
        self.used[endpoint] += cost
        return self._fetch(f"{YT_API}/{endpoint}", dict(params, key=self.key)) or {}

    def official_uploads(self, channel_id: str, since: str, max_pages: int = 3) -> list[str]:
        ids, token = [], None
        for _ in range(max_pages):
            p = {"part": "contentDetails", "playlistId": "UU" + channel_id[2:], "maxResults": 50}
            if token:
                p["pageToken"] = token
            d = self.call("playlistItems", p)
            older = False
            for it in d.get("items", []):
                cd = it.get("contentDetails") or {}
                pub = cd.get("videoPublishedAt") or ""
                if brt_day(pub) < since:
                    older = True
                    continue
                if cd.get("videoId"):
                    ids.append(cd["videoId"])
            token = d.get("nextPageToken")
            if older or not token:
                break
        return ids

    def creator_search(self, query: str, since: str, pages: int = 1) -> list[str]:
        ids, token = [], None
        for _ in range(pages):
            p = {"part": "snippet", "type": "video", "q": query, "regionCode": "BR",
                 "relevanceLanguage": "pt", "maxResults": 50, "order": "date",
                 "publishedAfter": f"{since}T00:00:00Z"}
            if token:
                p["pageToken"] = token
            d = self.call("search", p)
            ids += [it["id"]["videoId"] for it in d.get("items", [])
                    if (it.get("id") or {}).get("videoId")]
            token = d.get("nextPageToken")
            if not token:
                break
        return ids

    def hydrate(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), 50):
            d = self.call("videos", {"part": "snippet,statistics", "id": ",".join(ids[i:i + 50])})
            for it in d.get("items", []):
                s = it.get("snippet") or {}
                out[it["id"]] = {"video_id": it["id"], "channel_id": s.get("channelId"),
                                 "channel": s.get("channelTitle"), "title": str(s.get("title") or "")[:200],
                                 "description": str(s.get("description") or "")[:600],
                                 "published": s.get("publishedAt"), "date": brt_day(s.get("publishedAt")),
                                 "lang": s.get("defaultAudioLanguage") or s.get("defaultLanguage"),
                                 "views": int((it.get("statistics") or {}).get("viewCount") or 0),
                                 "url": f"https://www.youtube.com/watch?v={it['id']}"}
        return out


# Language / region gate. Channel language tags are unreliable (Portuguese creators ship "en"/
# "en-US"/"pt-PT"/None), so a non-Spanish tag defers to the text; a Spanish tag is dropped.
_ES_MARKERS = {"ahora", "cuenta", "tarjeta", "plata", "conviene", "gane", "perdi", "invirtiendo",
               "rendimiento", "dinero", "tambien", "pesos", "argentina", "mexico", "uala", "usted",
               "nuestro", "hoy", "cuanto", "regales", "mercadopago.com.ar", "mercadopago.com.mx",
               "cambios", "tasa", "sin", "gastar", "hack", "meses"}
_PT_MARKERS = {"voce", "nao", "cartao", "conta", "hoje", "dinheiro", "rendimento", "limite",
               "agora", "aumentar", "como", "para", "isso", "novo", "nova", "acabou", "chegar",
               "beneficios", "banco", "cashback", "pix", "fatura", "anuidade"}


def language_gate(item: dict[str, Any]) -> tuple[bool, str]:
    """(keep, reason). Drops Spanish-tagged videos and untagged/mis-tagged ones whose text is
    predominantly Spanish (the spike's Mercado Pago MX/AR leak)."""
    lang = str(item.get("lang") or "").lower()
    if lang.startswith("es"):
        return False, f"lang:{lang}"
    toks = set(re.findall(r"[a-z.]+", fold(f"{item.get('title', '')} {item.get('description', '')[:300]}")))
    es, pt = len(toks & _ES_MARKERS), len(toks & _PT_MARKERS)
    if es >= 2 and es > pt:
        return False, f"text:es({es}>{pt})"
    return True, "ok"


# ---------------------------------------------------------------------------------------------
# Nova Lite classification (Bluefin analyze.py shape + is_new_change + pt_br + change)
# ---------------------------------------------------------------------------------------------

def _anchor(s: dict[str, Any]) -> str:
    parts = [f"YouTube {ch['handle']} (channel id {ch['id']})" for ch in s.get("youtube_channels") or []]
    parts += [f"iOS app id{a['id']} ({a['name']})" for a in s.get("apple_app_ids") or []]
    return "; ".join(parts)


def _item_text(m: dict[str, Any]) -> str:
    if m.get("source") == "youtube":
        desc = re.sub(r"\s+", " ", str(m.get("description") or ""))[:500]
        return (f"[{m.get('kind')} video by {m.get('channel')}, published {m.get('date')}] "
                f"TITLE: {str(m.get('title'))[:200]} | DESCRIPTION: {desc}")
    return (f"[app review, {m.get('rating')} stars, app v{m.get('version')}, {m.get('date')}] "
            f"{str(m.get('title'))[:120]} | {str(m.get('text'))[:500]}")


def build_prompt(s: dict[str, Any], batch: list[dict[str, Any]]) -> str:
    items = "\n".join(f"{i}. {_item_text(m)}" for i, m in enumerate(batch))
    return (
        "You are a competitor product-monitoring classifier for a Brazilian bank's Chief Product Officer.\n"
        f"SUBJECT PRODUCT: {s['name']} (Brazilian digital bank / fintech app)"
        f" (also: {', '.join(s.get('aliases') or [])})\nPUBLISHES AT: {_anchor(s)}\n"
        f"NAMESAKE NOTE: {s.get('namesake_risk') or 'none'}\n\n"
        f"ITEMS:\n{items}\n\n"
        "Return ONLY a JSON array, one object per item in order, keys: "
        '"i" (item number), '
        '"relevant" (true only if the item is substantively about the SUBJECT\'s own products/app, '
        'not a namesake, not a passing mention in a list of banks), '
        '"event" ("launch" new product customers can now use | "feature" new/changed feature | '
        '"price" fee/rate/cashback/limit/benefit change | "outage" app/service failure | '
        '"complaint" user problem | "praise" | "corporate" company news that is not a customer-facing '
        'product change: M&A, acquisition talks or rumours, funding, earnings, dividends/JCP, share '
        'price, executives, lawsuits | '
        '"other"), '
        '"sentiment" ("positive"|"neutral"|"negative"), '
        '"feature" (the specific product or feature named, e.g. "Pix parcelado", else ""), '
        '"is_new_change" (true ONLY if the item reports a specific, recent, dated change by the SUBJECT: '
        "a launch, a new/removed feature, a fee/rate/benefit/limit-policy change, or a current outage. "
        "false for evergreen how-to/tutorials (\"como aumentar limite\", \"como pagar boleto\"), reviews of "
        "a product that has not changed, brand ads, sponsorships, speculation and general opinion, "
        "and for livestreams or event invitations whose title only says the channel is live), "
        '"change" (if is_new_change: a short canonical Portuguese name for the change, e.g. '
        '"fim do Priority Pass", "Central de Cashback"; else ""), '
        '"pt_br" (true if the item is in Portuguese and aimed at Brazil; false for Spanish or '
        "Mexico/Argentina content), "
        '"why" (<=15 words, IN BRAZILIAN PORTUGUESE even when the item is in English, what happened).'
    )


# Nova Lite sometimes answers "why" in English despite the prompt (live 2026-09-27: "Mercado Pago
# is live.", "Card approval and high initial limit."). The digest goes to a Brazilian CPO.
_EN_WORDS = frozenset("the is are and of with for about this new live improvements approval "
                      "announcement users many by has have was its s update updates launches "
                      "evolution suspended banned card offers".split())
_PT_WORDS = frozenset("de da do das dos com para no na em que um uma os as ao pelo pela nova novo "
                      "sobre".split())
# A generic "<brand> está ao vivo" livestream title reports nothing dated.
_LIVE_ONLY = re.compile(r"^\W*[\w\s.&'-]{0,40}?\b(?:est[aá]\s+)?ao\s+vivo\W*$", re.I)


def _looks_english(text: str) -> bool:
    toks = re.findall(r"[a-záéíóúâêôãõç]+", str(text or "").lower())
    return bool(toks) and bool(_EN_WORDS.intersection(toks)) and not _PT_WORDS.intersection(toks)


def parse_response(text: str | None) -> list[dict[str, Any]]:
    """Model text → list of result dicts. Tolerates prose around the array and a truncated
    array (salvages every complete ``{...}`` object) — the spike lost 14 items to partial JSON."""
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            got = json.loads(m.group(0))
            if isinstance(got, list):
                return [x for x in got if isinstance(x, dict)]
        except json.JSONDecodeError:
            pass
    out = []
    for obj in re.findall(r"\{[^{}]*\}", text):
        try:
            x = json.loads(obj)
        except json.JSONDecodeError:
            continue
        if isinstance(x, dict):
            out.append(x)
    return out


def _as_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return {"true": True, "false": False}.get(v.strip().lower())
    return None


def apply_result(m: dict[str, Any], x: dict[str, Any] | None) -> None:
    if x is None:
        m.update(provenance="unscored", relevant=None, event=None, sentiment=None, feature="",
                 why="", is_new_change=None, pt_br=None, change="")
        return
    ev = str(x.get("event") or "other").lower()
    m.update(provenance="llm", relevant=bool(_as_bool(x.get("relevant"))),
             event=ev if ev in EVENTS else "other",
             sentiment=str(x.get("sentiment") or "neutral")[:10],
             feature=str(x.get("feature") or "")[:80], why=str(x.get("why") or "")[:160],
             is_new_change=bool(_as_bool(x.get("is_new_change"))),
             pt_br=_as_bool(x.get("pt_br")), change=str(x.get("change") or "")[:80])
    if _looks_english(m["why"]):
        m["why"] = (m["change"] or str(m.get("title") or ""))[:160]
    if m.get("source") == "youtube" and _LIVE_ONLY.match(str(m.get("title") or "")):
        m["is_new_change"] = False


Converser = Callable[[str, int], "tuple[str | None, dict[str, int]]"]


def bedrock_converser(*, region: str | None = None) -> Converser:
    """Nova Lite via Converse; falls back to the US inference profile when on-demand is refused.
    Model id overridable with ONCA_CPO_RADAR_MODEL_ID."""
    import boto3

    rt = boto3.client("bedrock-runtime", region_name=region or os.environ.get("AWS_REGION") or "us-east-1")
    env_model = os.environ.get("ONCA_CPO_RADAR_MODEL_ID")
    models = [env_model] if env_model else list(MODEL_IDS)
    chosen: list[str] = []

    def converse(prompt: str, max_tokens: int) -> tuple[str | None, dict[str, int]]:
        for model in (chosen[:1] or models):
            try:
                r = rt.converse(modelId=model, messages=[{"role": "user", "content": [{"text": prompt}]}],
                                inferenceConfig={"maxTokens": max_tokens, "temperature": 0})
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).lower()
                if not chosen and ("on-demand" in msg or "inference profile" in msg):
                    continue
                print(f"Warning: cpo_radar classify call failed: {str(exc)[:160]}")
                return None, {}
            if not chosen:
                chosen.append(model)
            u = r.get("usage") or {}
            parts = ((r.get("output") or {}).get("message") or {}).get("content") or []
            return ("".join(p.get("text", "") for p in parts) or None,
                    {"input_tokens": int(u.get("inputTokens") or 0),
                     "output_tokens": int(u.get("outputTokens") or 0)})
        return None, {}

    return converse


def classify(subject: dict[str, Any], items: list[dict[str, Any]], converse: Converser,
             *, usage: dict[str, Any] | None = None, workers: int = WORKERS) -> None:
    """Classify ``items`` in place (BATCH per call, ≤``workers`` threads). Unscored items keep
    ``provenance="unscored"`` and are retried on the next run."""
    usage = usage if usage is not None else {}
    lock = threading.Lock()
    batches = [items[i:i + BATCH] for i in range(0, len(items), BATCH)]

    def run(batch: list[dict[str, Any]]) -> None:
        text, u = converse(build_prompt(subject, batch), 110 * len(batch) + 120)
        res = {}
        for x in parse_response(text):
            try:
                res[int(x.get("i", -1))] = x
            except (TypeError, ValueError):
                continue
        for i, m in enumerate(batch):
            apply_result(m, res.get(i))
        with lock:
            usage["calls"] = usage.get("calls", 0) + 1
            usage["input_tokens"] = usage.get("input_tokens", 0) + u.get("input_tokens", 0)
            usage["output_tokens"] = usage.get("output_tokens", 0) + u.get("output_tokens", 0)
            if text is None:
                usage["failed_batches"] = usage.get("failed_batches", 0) + 1

    if not batches:
        return
    with cf.ThreadPoolExecutor(max_workers=max(1, min(workers, len(batches)))) as pool:
        list(pool.map(run, batches))


# ---------------------------------------------------------------------------------------------
# clustering (several creators → one event)
# ---------------------------------------------------------------------------------------------

_CLUSTER_STOP = {
    "cartao", "conta", "banco", "app", "novo", "nova", "novos", "novas", "fim", "para", "com",
    "que", "dos", "das", "nos", "nas", "sem", "mais", "seu", "sua", "como", "clientes", "cliente",
    "mudanca", "mudancas", "muda", "tudo", "the", "and", "agora", "vale", "pena", "oficial",
}


def _cluster_tokens(m: dict[str, Any], subject: dict[str, Any]) -> set[str]:
    brand = set()
    for a in [subject.get("name", ""), *(subject.get("aliases") or [])]:
        brand |= set(re.findall(r"[a-z0-9]+", fold(a)))
    text = fold(f"{m.get('change') or ''} {m.get('feature') or ''}")
    return {t for t in re.findall(r"[a-z0-9]+", text)
            if len(t) >= 3 and t not in _CLUSTER_STOP and t not in brand}


# Deterministic backstops for what Nova Lite marks ``is_new_change`` anyway (first live digest,
# 2026-09-28: ~10 of 54 week events were how-tos, "Tutorial sobre novo recurso"; Bradesco
# dividends came through as a "price" change). Matched on folded (accent-free, lower) text.
_HOWTO_LEAD = re.compile(r"^\W*(como|tutorial|passo a passo|dicas?|aprenda|veja como|saiba como"
                         r"|how[\s-]to|explica(ndo)? como)\b")
_HOWTO_ANY = re.compile(r"\b(tutorial|passo a passo|how[\s-]to)\b")
_CORPORATE = re.compile(r"\b(dividendos?|jcp|juros sobre (o )?capital( proprio)?|recompra de acoes"
                        r"|lucro liquido|balanco|resultado trimestral|ipo|acoes (do|da|de)|cotacao)\b")
# a change name made only of these (after stop words and the brand) says nothing: "mudança que
# teve", "Atualização sobre o plano Bradesco"
_VAGUE = {
    "atualizacao", "atualizacoes", "sobre", "plano", "planos", "noticia", "noticias", "comentario",
    "comentarios", "recente", "recentes", "teve", "novidade", "novidades", "anuncio", "informacao",
    "informacoes", "politica", "alteracao", "alteracoes", "servico", "servicos", "produto",
    "produtos", "opcao", "opcoes", "aviso", "importante", "urgente", "atencao", "recurso",
    "recursos", "funcao", "funcionalidade", "video", "hoje", "ontem", "semana",
}


# a creator filming a how-to of something NEW still reports the change ("LEIA QUALQUER CHAVE PIX
# COM A CÂMERA (NOVA FUNÇÃO)", "ITAÚ LIBERA O CARTÃO THE ESSENTIAL")
_NEWS_TITLE = re.compile(r"\b(nova funcao|novo recurso|novidade|lancou|lanca|lancamento|chegou"
                         r"|libera|liberou|acabou|acaba|fim d[oa])\b")


def is_howto(m: dict[str, Any]) -> bool:
    """Evergreen how-to: a change named "como …", or a how-to reason/title with no news signal."""
    change, why, title = (fold(m.get(k)) for k in ("change", "why", "title"))
    if _HOWTO_LEAD.match(change):
        return True
    howto = bool(_HOWTO_LEAD.match(why) or _HOWTO_LEAD.match(title) or _HOWTO_ANY.search(f"{change} {why}"))
    return howto and not _NEWS_TITLE.search(title)


def is_corporate(m: dict[str, Any]) -> bool:
    return bool(_CORPORATE.search(fold(f"{m.get('change') or ''} {m.get('feature') or ''} {m.get('title') or ''}")))


def surfaceable(m: dict[str, Any]) -> bool:
    """The card gate: relevant, pt-BR, a DATED change (not evergreen), and a change-type event."""
    return (m.get("provenance") == "llm" and bool(m.get("relevant")) and m.get("pt_br") is not False
            and bool(m.get("is_new_change")) and m.get("event") in SURFACE_EVENTS
            and language_gate(m)[0] and not is_howto(m) and not is_corporate(m))


def is_specific(m: dict[str, Any], subject: dict[str, Any]) -> bool:
    """The change name carries at least one token beyond the brand, stop words and filler."""
    return bool(_cluster_tokens(m, subject) - _VAGUE)


def event_candidates(subject: dict[str, Any], videos: list[dict[str, Any]], start: str) -> list[dict[str, Any]]:
    return [v for v in videos if surfaceable(v) and is_specific(v, subject) and (v.get("date") or "") >= start]


_STORY_STOP = _CLUSTER_STOP | _VAGUE | {"fazer", "milhoes", "bilhoes", "reais", "voce", "voces",
                                         "urgente", "agora", "pessoal", "galera", "banco", "bancos"}


def _story_tokens(m: dict[str, Any], subject: dict[str, Any]) -> set[str]:
    brand = set()
    for a in [subject.get("name", ""), *(subject.get("aliases") or [])]:
        brand |= set(re.findall(r"[a-z0-9]+", fold(a)))
    text = fold(f"{m.get('change') or ''} {m.get('feature') or ''} {m.get('title') or ''}")
    return {t for t in re.findall(r"[a-z0-9]+", text)
            if len(t) >= 5 and t not in _STORY_STOP and t not in brand}


def cluster_mentions(subject: dict[str, Any], mentions: list[dict[str, Any]],
                     *, span_days: int = CLUSTER_SPAN_DAYS, story_days: int = STORY_SPAN_DAYS
                     ) -> list[list[dict[str, Any]]]:
    """Single-link clustering on the change/feature name: two mentions join when their
    distinctive tokens overlap by ≥50% of the smaller set and they are ≤``span_days`` apart.
    Creators name the same story differently ("irregularidades em contratos" / "C6 Bank proibido
    de fazer consignado"), so two mentions of the same event type ≤``story_days`` apart also join
    when change+title share ≥2 distinctive words of 5+ letters."""
    toks = [_cluster_tokens(m, subject) for m in mentions]
    story = [_story_tokens(m, subject) for m in mentions]
    parent = list(range(len(mentions)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(mentions)):
        for j in range(i + 1, len(mentions)):
            a, b = toks[i], toks[j]
            try:
                gap = abs((dt.date.fromisoformat(mentions[i]["date"])
                           - dt.date.fromisoformat(mentions[j]["date"])).days)
            except (KeyError, ValueError):
                gap = 0
            by_name = bool(a and b) and len(a & b) / min(len(a), len(b)) >= 0.5 and gap <= span_days
            by_story = (len(story[i] & story[j]) >= 2 and gap <= story_days
                        and mentions[i].get("event") == mentions[j].get("event"))
            if by_name or by_story:
                parent[find(i)] = find(j)
    groups: dict[int, list[dict[str, Any]]] = {}
    for i, m in enumerate(mentions):
        groups.setdefault(find(i), []).append(m)
    return list(groups.values())


def _event_id(*parts: str) -> str:
    return "radar:" + hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def cluster_to_event(subject: dict[str, Any], group: list[dict[str, Any]]) -> dict[str, Any]:
    group = sorted(group, key=lambda m: (m.get("kind") != "official", m.get("date") or "", -(m.get("views") or 0)))
    lead = group[0]
    channels = {m.get("channel_id") or m.get("channel") for m in group}
    official = any(m.get("kind") == "official" for m in group)
    etype = Counter(m.get("event") for m in group).most_common(1)[0][0]
    title = lead.get("change") or lead.get("feature") or lead.get("title")
    dates = sorted(m.get("date") or "" for m in group)
    n_ch = len(channels)
    confidence = "oficial" if official else ("corroborado" if n_ch >= 2 else "criador único")
    first = min(group, key=lambda m: m.get("video_id") or "")
    return {
        "id": _event_id(subject["id"], "youtube", first.get("video_id") or title or ""),
        "product": subject["id"], "product_label": subject["name"],
        "entity": (subject.get("onca_entities") or [None])[0],
        "source": "youtube", "type": etype, "type_label": TYPE_LABEL.get(etype, etype),
        "date": dates[0], "last_seen": dates[-1], "title": str(title)[:120],
        # a mention scored before the English guard can still carry an English "why"
        "reason": (lead.get("why") if not _looks_english(lead.get("why") or "") else "")
                  or lead.get("title"),
        "confidence": confidence, "official": official, "n_sources": len(group), "n_channels": n_ch,
        "url": lead.get("url"),
        "sources": [{"url": m.get("url"), "title": m.get("title"), "channel": m.get("channel"),
                     "kind": m.get("kind"), "date": m.get("date"), "views": m.get("views")}
                    for m in group[:5]],
    }


# ---------------------------------------------------------------------------------------------
# baseline + alerts (App Store)
# ---------------------------------------------------------------------------------------------

def _review_metrics(r: dict[str, Any]) -> set[str]:
    out = set()
    if (r.get("rating") or 5) <= 2:
        out.add("low_star")
    if r.get("provenance") == "llm" and r.get("relevant") and r.get("event") in ("outage", "complaint"):
        out.add(r["event"])
    return out


def daily_counts(reviews: list[dict[str, Any]]) -> dict[str, Counter]:
    out: dict[str, Counter] = {}
    for r in reviews:
        c = out.setdefault(r.get("date") or "", Counter())
        c["total"] += 1
        for k in _review_metrics(r):
            c[k] += 1
    return out


_CODE_RE = re.compile(r"\b([A-Z]{1,4}[-_ ]?\d{2,5})\b")
_QUOTE_RE = re.compile(r"[\"“”«]([^\"“”«»]{4,60})[\"“”»]")
_CODE_STOP = {"IOS", "CPF", "CDI", "PIX", "IPHONE"}


def top_error_strings(reviews: list[dict[str, Any]], n: int = 5) -> list[dict[str, Any]]:
    """Error codes (``AL-903``) and quoted on-screen messages (“Test Mode em produção”)."""
    cnt: Counter = Counter()
    shown: dict[str, str] = {}
    for r in reviews:
        text = f"{r.get('title') or ''} {r.get('text') or ''}"
        found = set()
        for code in _CODE_RE.findall(text):
            if re.sub(r"[-_ \d]", "", code) in _CODE_STOP:
                continue
            found.add(("code", re.sub(r"[_ ]", "-", code)))
        for q in _QUOTE_RE.findall(text):
            found.add(("quote", q.strip()))
        for _kind, s in found:
            k = fold(s)
            cnt[k] += 1
            shown.setdefault(k, s)
    return [{"text": shown[k], "count": c} for k, c in cnt.most_common(n)]


def detect_alerts(subject: dict[str, Any], reviews: list[dict[str, Any]], *, eval_days: list[str],
                  coverage_since: str | None, min_count: int = ALERT_MIN_COUNT, z_min: float = ALERT_Z,
                  baseline_days: int = BASELINE_DAYS, app: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Per product-day alert when outage or complaint has count ≥``min_count`` AND z ≥``z_min``
    against the product's OWN prior ``baseline_days`` (zero days are data; days before
    ``coverage_since`` are not). Needs ≥MIN_BASELINE_DAYS covered prior days.

    ``low_star`` (≤2★) is always reported beside a fired alert as context, and becomes a trigger
    only on a day the classifier mostly failed (>50% unscored) — so a Bedrock outage does not
    blind the radar. It is NOT a trigger otherwise: on Inter's data it fires on 2026-09-18
    (10 diffuse complaints, no single cause), which the spike's hand check rejected."""
    counts = daily_counts(reviews)
    alerts = []
    for day in eval_days:
        d = dt.date.fromisoformat(day)
        prior = [(d - dt.timedelta(days=k)).isoformat() for k in range(1, baseline_days + 1)]
        prior = [p for p in prior if not coverage_since or p >= coverage_since]
        if len(prior) < MIN_BASELINE_DAYS:
            continue
        def stat(metric: str) -> dict[str, Any]:
            n = counts.get(day, Counter()).get(metric, 0)
            series = [counts.get(p, Counter()).get(metric, 0) for p in prior]
            mu = statistics.mean(series)
            z = (n - mu) / max(statistics.pstdev(series), 1.0)
            return {"count": n, "mean": round(mu, 2), "z": round(z, 2), "baseline_days": len(prior),
                    "baseline_range": [min(series), max(series)]}

        day_rows = [r for r in reviews if r.get("date") == day]
        unscored = sum(1 for r in day_rows if r.get("provenance") != "llm")
        triggers = list(ALERT_METRICS)
        if day_rows and unscored / len(day_rows) > 0.5:
            triggers.append(FALLBACK_METRIC)
        fired = {}
        for metric in triggers:
            st = stat(metric)
            if st["count"] >= min_count and st["z"] >= z_min:
                fired[metric] = st
        if not fired:
            continue
        context = {FALLBACK_METRIC: stat(FALLBACK_METRIC)}
        bad = [r for r in reviews if r.get("date") == day and _review_metrics(r)]
        vers = Counter(r.get("version") for r in bad if r.get("version"))
        dom = vers.most_common(1)[0] if vers else None
        etype = "outage" if "outage" in fired else "complaint"
        head = fired.get(etype) or next(iter(fired.values()))
        low = context[FALLBACK_METRIC]
        errs = top_error_strings(bad)
        app_id = ((subject.get("apple_app_ids") or [{}])[0]).get("id")
        noun = {"outage": "relatos de falha", "complaint": "reclamações"}.get(
            next(iter(fired)), "avaliações ≤2★")
        reason = (f"{head['count']} {noun} no app iOS em {day} "
                  f"(média {str(head['mean']).replace('.', ',')}/dia, z={head['z']}; "
                  f"{low['count']} avaliações ≤2★)"
                  + (f" · {dom[1]} na v{dom[0]}" if dom else "")
                  + (f" · “{errs[0]['text']}”" if errs else ""))
        alerts.append({
            "id": _event_id(subject["id"], "appstore", day),
            "product": subject["id"], "product_label": subject["name"],
            "entity": (subject.get("onca_entities") or [None])[0],
            "source": "appstore", "type": etype, "type_label": TYPE_LABEL[etype], "date": day,
            "last_seen": day, "title": "Pico de avaliações negativas no app iOS",
            "reason": reason, "confidence": "baseline", "official": False,
            "metrics": fired, "context": context,
            "dominant_version": {"version": dom[0], "reviews": dom[1],
                                 "share": round(dom[1] / len(bad), 2)} if dom else None,
            "error_strings": errs,
            "samples": [f"{r.get('title')}: {str(r.get('text') or '')[:140]}" for r in
                        sorted(bad, key=lambda r: r.get("rating") or 5)[:3]],
            "app": {k: (app or {}).get(k) for k in ("version", "version_date")},
            "url": STORE_URL.format(app=app_id) if app_id else None,
            "sources": ([{"url": STORE_URL.format(app=app_id), "title": "App Store (avaliações)"},
                         {"url": RSS_URL.format(page=1, app=app_id), "title": "RSS de avaliações"}]
                        if app_id else []),
            "n_sources": len(bad),
        })
    return alerts


# ---------------------------------------------------------------------------------------------
# storage (S3 or local directory)
# ---------------------------------------------------------------------------------------------

class S3Store:
    def __init__(self, bucket: str, s3: Any = None):
        import boto3

        self.bucket, self.s3 = bucket, s3 or boto3.client("s3")

    def get(self, key: str) -> Any:
        try:
            return json.loads(self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read())
        except Exception:  # absent store → cold start
            return None

    def put(self, key: str, obj: Any) -> str:
        self.s3.put_object(Bucket=self.bucket, Key=key, ContentType="application/json",
                           Body=json.dumps(obj, ensure_ascii=False).encode("utf-8"))
        return f"s3://{self.bucket}/{key}"

    def keys(self, prefix: str) -> list[str]:
        out, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            r = self.s3.list_objects_v2(**kw)
            out += [o["Key"] for o in r.get("Contents") or []]
            token = r.get("NextContinuationToken")
            if not token:
                return out

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=key)


class LocalStore:
    """Same interface over a local directory (``local_out``) — smoke tests and dev runs."""

    def __init__(self, root: str | pathlib.Path):
        self.root = pathlib.Path(root)

    def _p(self, key: str) -> pathlib.Path:
        return self.root / key

    def get(self, key: str) -> Any:
        p = self._p(key)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def put(self, key: str, obj: Any) -> str:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        return str(p)

    def keys(self, prefix: str) -> list[str]:
        base = self._p(prefix)
        if not base.exists():
            return []
        return [f"{prefix}{p.name}" for p in base.iterdir() if p.is_file()]

    def delete(self, key: str) -> None:
        self._p(key).unlink(missing_ok=True)


def load_radar(bucket: str, *, s3: Any = None) -> dict[str, Any]:
    """Read ``product_radar/latest.json`` (feed_builder). {} if absent."""
    return S3Store(bucket, s3=s3).get(LATEST_KEY) or {}


def prune_dated(store: Any, today: dt.date, keep_days: int = YT_KEEP_DAYS) -> list[str]:
    cutoff = _days_back(today, keep_days)
    gone = []
    for k in store.keys(PREFIX):
        m = _DATED_RE.match(k)
        if m and m.group(1) < cutoff:
            store.delete(k)
            gone.append(k)
    return gone


# ---------------------------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------------------------

def _subject_youtube(subject: dict[str, Any], yt: YouTubeClient, *, since: str, known: set[str],
                     search_pages: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    official = {ch["id"] for ch in subject.get("youtube_channels") or []}
    status: dict[str, Any] = {}
    ids: list[str] = []
    try:
        for ch in subject.get("youtube_channels") or []:
            ids += yt.official_uploads(ch["id"], since)
        ids += yt.creator_search(subject.get("search_query") or subject["name"], since, pages=search_pages)
    except QuotaExceeded as exc:
        status["quota"] = f"budget reached before {exc}"
    except Exception as exc:  # noqa: BLE001
        status["error"] = str(exc)[:160]
    todo = [i for i in dict.fromkeys(ids) if i not in known]
    meta: dict[str, dict[str, Any]] = {}
    if todo:
        try:
            meta = yt.hydrate(todo)
        except QuotaExceeded:
            status["quota"] = "budget reached before videos"
        except Exception as exc:  # noqa: BLE001
            status["error"] = str(exc)[:160]
    rows, dropped = [], Counter()
    for vid in todo:
        m = meta.get(vid)
        if not m or (m.get("date") or "") < since:
            continue
        m.update(source="youtube", product=subject["id"], fetched_at=_now(),
                 kind="official" if m.get("channel_id") in official else "creator")
        keep, why = language_gate(m)
        if not keep:
            dropped[why.split(":")[0]] += 1
            continue
        rows.append(m)
    status.update(found=len(set(ids)), new=len(rows), dropped_language=sum(dropped.values()))
    return rows, status


def run(store: Any | None, *, today: dt.date | None = None, subjects: list[dict[str, Any]] | None = None,
        products: list[str] | None = None, fetch: Fetch | None = None,
        converse: Converser | None = None, yt_key: str | None | bool = None,
        youtube: bool = True, reclassify_youtube: bool = False) -> dict[str, Any]:
    """One daily radar run. ``store`` None = compute only (no persistence).
    ``yt_key``: None → resolve (env/secret); False/"" → treat as absent.
    ``reclassify_youtube``: re-score every STORED video (after a prompt/taxonomy change) — they
    re-enter the classify step as unscored; reviews are untouched (they drive the baseline)."""
    today = today or dt.datetime.now(BRT).date()
    subjects_source = "given" if subjects else None
    subjects = subjects or load_subjects()
    subjects_source = subjects_source or SUBJECTS_SOURCE["v"]
    if products:
        subjects = [s for s in subjects if s["id"] in set(products)]
    state = (store.get(STATE_KEY) if store else None) or {}
    st_reviews: dict[str, dict[str, Any]] = state.get("appstore") or {}
    st_videos: dict[str, list[dict[str, Any]]] = state.get("youtube") or {}
    review_cut = _days_back(today, REVIEW_KEEP_DAYS)
    yt_cut = _days_back(today, YT_KEEP_DAYS)

    # --- YouTube key (optional) -------------------------------------------------------------
    key = None
    if youtube:
        key = youtube_key() if yt_key is None else (yt_key or None)
    yt = YouTubeClient(key, budget=_env_int("ONCA_CPO_YT_QUOTA", 1000), fetch=fetch) if key else None
    sources: dict[str, Any] = {"appstore": "ok",
                               "youtube": "ok" if yt else ("disabled: no key" if youtube else "disabled: run option"),
                               "classifier": "ok"}
    if converse is None:
        try:
            converse = bedrock_converser()
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: Bedrock unavailable ({exc.__class__.__name__}); items stay unscored")
            converse = lambda p, n: (None, {})  # noqa: E731
    usage: dict[str, Any] = {}
    max_classify = _env_int("ONCA_CPO_MAX_CLASSIFY", 800)
    lookback = _env_int("ONCA_CPO_YT_LOOKBACK_DAYS", 2)
    search_pages = _env_int("ONCA_CPO_YT_SEARCH_PAGES", 1)
    coverage: dict[str, Any] = {}
    products_out, events, alerts = [], [], []
    eval_days = [_days_back(today, k) for k in range(EVENT_WINDOW_DAYS - 1, -1, -1)]

    for s in subjects:
        pid = s["id"]
        cov: dict[str, Any] = {}
        # --- App Store -----------------------------------------------------------------------
        prev = st_reviews.get(pid) or {}
        reviews = [r for r in prev.get("reviews") or [] if (r.get("date") or "") >= review_cut]
        seen = {r["id"] for r in reviews}
        cov_since = prev.get("coverage_since")
        app_meta: dict[str, Any] = {}
        by_app: dict[str, Any] = {}
        for i, app in enumerate(s.get("apple_app_ids") or []):
            # `since` is PER APP: a product with two apps (Bradesco + next) must not cut the
            # quieter app's backfill at the busier app's newest review (live 2026-09-26: next
            # got 2 reviews instead of 30 days). Rows stored before app_id existed belong to
            # the first (primary) app.
            mine = [r for r in reviews if (r.get("app_id") or (s["apple_app_ids"][0]["id"])) == app["id"]]
            since = max((r["date"] for r in mine), default=None)
            since = _days_back(dt.date.fromisoformat(since), 1) if since else review_cut
            new, c = pull_reviews(app["id"], seen=seen, since=since, fetch=fetch)
            for r in new:
                r.update(source="appstore", product=pid, app_id=app["id"])
            reviews += new
            meta = app_lookup(app["id"], fetch=fetch)
            if i == 0:
                app_meta = meta        # alerts carry the PRIMARY app's version/release context
            by_app[app["id"]] = dict(c, app_id=app["id"])
            if i == 0:
                cov["appstore"] = dict(c, app_id=app["id"])
            if c.get("error"):
                _flag(sources, "appstore", "RSS error")
            if c.get("stop") == "empty_first_page":
                # every affected app is named: one overwritten string hid Neon's 3-day outage
                _flag(sources, "appstore", f"empty RSS for app {app['id']}")
            if not cov_since and new:
                # cold start: the oldest fetched day is partial unless we stopped on `since`
                oldest = min(r["date"] for r in new)
                cov_since = oldest if c.get("stop") == "since" else _days_back(dt.date.fromisoformat(oldest), -1)
            if c.get("cap_hit"):
                _flag(sources, "appstore", "500-review cap hit")
        cov_since = max(cov_since or review_cut, review_cut)
        # --- YouTube ---------------------------------------------------------------------------
        videos = [v for v in st_videos.get(pid) or [] if (v.get("date") or "") >= yt_cut]
        if yt:
            new_v, ycov = _subject_youtube(s, yt, since=_days_back(today, lookback),
                                           known={v["video_id"] for v in videos}, search_pages=search_pages)
            videos += new_v
            cov["youtube"] = ycov
            if ycov.get("error"):
                sources["youtube"] = "partial: API error"
            elif ycov.get("quota") and sources["youtube"] == "ok":
                sources["youtube"] = "partial: quota budget reached"
        if reclassify_youtube:
            for v in videos:
                v["provenance"] = "unscored"
        # --- classify (new + previously unscored) --------------------------------------------
        todo = [m for m in videos + reviews if m.get("provenance") != "llm"][:max_classify]
        for m in todo:
            m.setdefault("source", "youtube" if "video_id" in m else "appstore")
        # classify per source so a batch never mixes reviews and videos
        for src in ("youtube", "appstore"):
            classify(s, [m for m in todo if m.get("source") == src], converse, usage=usage)
        st_reviews[pid] = {"coverage_since": cov_since, "reviews": reviews}
        st_videos[pid] = videos
        # --- events ---------------------------------------------------------------------------
        cand = event_candidates(s, videos, eval_days[0])
        events += [cluster_to_event(s, g) for g in cluster_mentions(s, cand)]
        # no app answered today: yesterday's zero-review days are a GAP, not a quiet day, so a
        # z-score over them means nothing (Santander 09-28/29 kept stale reviews and read "quiet")
        stale = bool(by_app) and all(c.get("stop") == "empty_first_page" for c in by_app.values())
        pa = [] if stale else detect_alerts(s, reviews, eval_days=eval_days, coverage_since=cov_since, app=app_meta)
        if stale:
            cov["appstore_stale"] = True
        alerts += pa
        if len(by_app) > 1:
            cov["appstore_by_app"] = by_app
        coverage[pid] = cov
        products_out.append({"id": pid, "name": s["name"], "entity": (s.get("onca_entities") or [None])[0],
                             "app": app_meta, "reviews_held": len(reviews), "videos_held": len(videos),
                             "coverage_since": cov_since})

    if usage.get("calls") and usage.get("failed_batches") == usage.get("calls"):
        sources["classifier"] = "unavailable"
    elif usage.get("failed_batches"):
        sources["classifier"] = "partial"
    usage["usd"] = round(usage.get("input_tokens", 0) * 0.06e-6 + usage.get("output_tokens", 0) * 0.24e-6, 4)
    all_events = sorted(events + alerts, key=lambda e: (e.get("date") or "", e.get("source") == "appstore"),
                        reverse=True)
    radar = {
        "as_of": today.isoformat(), "generated_at": _now(), "window_days": EVENT_WINDOW_DAYS,
        "thresholds": {"alert_min_count": ALERT_MIN_COUNT, "alert_z": ALERT_Z, "baseline_days": BASELINE_DAYS},
        "sources": sources, "subjects_source": subjects_source, "coverage": coverage, "products": products_out,
        "events": all_events, "alerts": alerts,
        "youtube_quota": {"units": yt.units, "by_endpoint": dict(yt.used), "budget": yt.budget} if yt else None,
        "nova": usage,
    }
    summary: dict[str, Any] = {"as_of": radar["as_of"], "sources": sources, "subjects_source": subjects_source,
                               "n_subjects": len(subjects), "events": len(events),
                               "alerts": len(alerts), "youtube_units": yt.units if yt else 0,
                               "nova_calls": usage.get("calls", 0)}
    if store is not None:
        new_state = {"version": 1, "updated_at": radar["generated_at"], "appstore": st_reviews, "youtube": st_videos}
        store.put(STATE_KEY, new_state)
        store.put(f"{PREFIX}{radar['as_of']}.json", radar)
        summary["latest"] = store.put(LATEST_KEY, radar)
        summary["pruned"] = len(prune_dated(store, today))
    summary["radar"] = radar
    return summary


# ---------------------------------------------------------------------------------------------
# weekly CPO digest (pure) — delivered by src.dashboard.weekly_digest.send_cpo_digest
# ---------------------------------------------------------------------------------------------

def weekly_events(radar: dict[str, Any] | None, *, as_of: str | None = None, days: int = 7) -> list[dict[str, Any]]:
    radar = radar or {}
    end = dt.date.fromisoformat(str(as_of or radar.get("as_of") or dt.date.today().isoformat())[:10])
    start = (end - dt.timedelta(days=days - 1)).isoformat()
    return [e for e in radar.get("events") or []
            if start <= str(e.get("last_seen") or e.get("date") or "") <= end.isoformat()]


def render_weekly_digest(radar: dict[str, Any] | None, *, as_of: str | None = None,
                         days: int = 7, limit: int = 10) -> dict[str, Any]:
    """The last ``days`` of radar events as a digest: {title, headline, period, lines, events,
    text}. Fabricates nothing — every line is one stored event with its source link."""
    evs = weekly_events(radar, as_of=as_of, days=days)
    end = str(as_of or (radar or {}).get("as_of") or dt.date.today().isoformat())[:10]
    start = (dt.date.fromisoformat(end) - dt.timedelta(days=days - 1)).isoformat()
    n_alerts = sum(1 for e in evs if e.get("source") == "appstore")
    prods = sorted({e.get("product_label") or e.get("product") for e in evs})
    if evs:
        headline = (f"{len(evs)} evento(s) de produto em {len(prods)} concorrente(s) "
                    f"({', '.join(prods)})" + (f" · {n_alerts} alerta(s) de app" if n_alerts else ""))
    else:
        headline = "Sem mudanças de produto ou alertas de app detectados na semana."
    order = {"outage": 0, "complaint": 1, "price": 2, "launch": 3, "feature": 4}
    evs = sorted(evs, key=lambda e: (order.get(e.get("type"), 9), str(e.get("date") or "")))
    lines = [f"[{e.get('type_label') or e.get('type')}] {e.get('product_label')} — {e.get('date')}: "
             f"{e.get('title')}"
             + ("" if _looks_english(e.get("reason") or "") else f" — {e.get('reason')}")
             + (f" ({e['url']})" if e.get("url") else "")
             for e in evs[:limit]]
    text = f"Radar de produto do CPO — {start} a {end}\n{headline}\n" + "\n".join(f"- {l}" for l in lines)
    return {"title": "Radar de produto semanal do CPO", "headline": headline, "period": [start, end],
            "lines": lines, "events": evs[:limit], "text": text}


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    """Daily CPO Product Radar (#159). Writes to ONCA_DIGESTS_BUCKET, or to ``local_out``
    (event) / ONCA_CPO_RADAR_LOCAL_OUT (env) when set — never both."""
    event = event or {}
    local = event.get("local_out") or os.environ.get("ONCA_CPO_RADAR_LOCAL_OUT")
    if local:
        store: Any = LocalStore(local)
    else:
        bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
        store = S3Store(bucket) if bucket else None
    today = dt.date.fromisoformat(event["today"]) if event.get("today") else None
    out = run(store, today=today, products=event.get("products"), youtube=event.get("youtube", True) is not False,
              reclassify_youtube=bool(event.get("reclassify_youtube")))
    out.pop("radar", None)
    return {"statusCode": 200, "body": json.dumps(out, ensure_ascii=False)}


if __name__ == "__main__":  # pragma: no cover - operator CLI
    import sys

    if "--seed-registry" in sys.argv:
        print(json.dumps(seed_registry(), indent=1))
    else:
        print("usage: python -m src.ingest.cpo_radar --seed-registry")
