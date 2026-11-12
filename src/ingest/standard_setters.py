"""International standard-setters for the CRO regulatory board (#382, epic #384).

BCB/CVM/SUSEP rules often implement a global standard months or years after it is published.
Watching the standard-setters gives early warning: "Basel finalised X → expect a BCB consulta".

Sources (probed live 2026-11-12; headline + link + date only — syndication posture):

* **BIS media releases** — ``/api/document_lists/press_releases.json`` (full list, ~1.4k rows,
  each tagged with its issuing body: BCBS, CPMI, BIS Innovation Hub…). The public RSS keeps one
  item, so the JSON list is the route. robots.txt does not disallow ``/api/``.
* **FSB publications** — ``fsb.org/feed/?post_type=publications`` (RSS 2.0).

Rejected (recorded on #382): FATF (Cloudflare bot challenge — never bypassed) and IOSCO (403 to
non-browser clients). FATF plenary outcomes reach Onça via the Brazilian press and COAF/DOU.

Each item is tagged by topic (title keywords) → the Brazilian implementer and the licensed
modules it concerns. Untagged items and institutional noise (appointments, statements on
people, committee meetings) are dropped. State in ``reference/standard_setters.json``; a source
whose fetch fails keeps its previous items.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import re
from typing import Any, Callable
from xml.etree import ElementTree as ET

STATE_KEY = "reference/standard_setters.json"
BIS_JSON = "https://www.bis.org/api/document_lists/press_releases.json"
BIS_BASE = "https://www.bis.org"
FSB_RSS = "https://www.fsb.org/feed/?post_type=publications"
KEEP_DAYS = 400
FEED_DAYS = 365

_UA = {"User-Agent": "Mozilla/5.0 (compatible; OncaBot/1.0; +https://onssa.org)"}

BODIES = {"BCBS": "Comitê de Basileia (BCBS)", "CPMI": "CPMI · pagamentos (BIS)",
          "BIS Innovation Hub": "BIS Innovation Hub", "Markets Committee": "BIS Markets Committee",
          "IAIS": "IAIS · seguros", "Bank for International Settlements": "BIS", "FSB": "FSB"}

# topic → (pt-BR label, title regex, Brazilian implementer(s), licensed modules concerned)
_BANKS = ["banking", "investment-banking"]
TOPICS: list[tuple[str, str, str, list[str], list[str]]] = [
    ("crypto", "Cripto & stablecoins", r"crypto|stablecoin|tokeni[sz]|digital asset",
     ["BCB", "CVM"], ["crypto", "fintech", "banking"]),
    ("payments", "Pagamentos & infraestrutura", r"payment|\bfmis?\b|financial market infrastructure"
     r"|cross-border|clearing|settlement|\bccps?\b|fast payment|margin requirement|derivative",
     ["BCB", "CVM"],
     ["acquiring", "fintech", "banking"]),
    ("capital", "Capital (Basileia)", r"capital|basel iii|leverage ratio|risk-based|g-sib|"
     r"\bd-sib|systemically important|window-dressing|synthetic risk transfer|market risk|"
     r"credit risk|output floor|disclosure|basel committee .*(guideline|standard|framework)",
     ["BCB"], _BANKS),
    ("liquidity", "Liquidez", r"liquidity|\blcr\b|\bnsfr\b|interest rate risk", ["BCB"], _BANKS),
    ("resolution", "Resolução & crises", r"resolution|recovery|crisis|\btlac\b|bail-in|"
     r"deposit insurance", ["BCB", "FGC"], _BANKS),
    ("nbfi", "Intermediação não bancária", r"non-bank|\bnbfi\b|money market fund|"
     r"open-ended fund|private credit|leverage in", ["CVM"],
     ["asset-management", "private-markets", "wealth-management"]),
    ("aml", "PLD/FT", r"money laundering|\baml\b|terrorist financing|sanction",
     ["COAF", "BCB", "CVM"], ["banking", "fintech", "crypto", "acquiring", "betting"]),
    ("tech", "Risco operacional, cyber & IA", r"cyber|operational resilience|\bict\b|"
     r"information and communication technology|third-party|artificial intelligence|\bai\b",
     ["BCB", "CVM", "SUSEP"], ["banking", "fintech", "insurance"]),
    ("climate", "Clima", r"climate|nature-related|sustainab|transition plan", ["BCB", "CVM", "SUSEP"],
     ["banking", "insurance", "asset-management"]),
    ("insurance", "Seguros", r"insur|\biais\b", ["SUSEP"], ["insurance"]),
    ("implementation", "Implementação & avaliações", r"implementation|peer review|"
     r"\brcap\b|compliant|consistency assessment|monitoring review", ["BCB", "CVM"], _BANKS),
]
_NOISE = re.compile(r"appoint|nominat|statement on |statement by |extends term|"
                    r"\bmeets\b|\bmeet in\b|consultative group|vacanc|annual general meeting|"
                    r"retire|condolence|welcome[s]? .* as ", re.I)


def tag(title: str) -> list[str]:
    t = title or ""
    return [k for k, _l, rx, _i, _m in TOPICS if re.search(rx, t, re.I)]


def is_noise(title: str) -> bool:
    return bool(_NOISE.search(title or ""))


def _item(source: str, body: str, title: str, url: str, date: str | None) -> dict[str, Any] | None:
    title = re.sub(r"\s+", " ", title or "").strip()
    if not title or not url or not date or is_noise(title):
        return None
    topics = tag(title)
    if not topics:
        return None
    meta = {k: (l, i, m) for k, l, _rx, i, m in TOPICS}
    implementers = list(dict.fromkeys(x for t in topics for x in meta[t][1]))
    industries = list(dict.fromkeys(x for t in topics for x in meta[t][2]))
    return {"id": url, "source": source, "body": body, "body_label": BODIES.get(body, body),
            "title": title, "url": url, "date": date, "topics": topics,
            "topic_labels": [meta[t][0] for t in topics], "implementers": implementers,
            "industries": industries, "brazil": bool(re.search(r"brazil|brasil", title, re.I))}


# --- parsers (pure) -----------------------------------------------------------------------
def parse_bis(doc: dict[str, Any], *, since: str) -> list[dict[str, Any]]:
    out = []
    for path, v in ((doc or {}).get("list") or {}).items():
        date = str(v.get("content_date") or "")[:10]
        if date < since:
            continue
        srcs = v.get("sources") or []
        body = next((s for s in srcs if s != "Bank for International Settlements"),
                    srcs[0] if srcs else "Bank for International Settlements")
        it = _item("BIS", body, v.get("title") or "", BIS_BASE + path, date)
        if it:
            out.append(it)
    return out


def parse_fsb(xml: bytes, *, since: str) -> list[dict[str, Any]]:
    out = []
    root = ET.fromstring(xml)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        try:
            date = email.utils.parsedate_to_datetime(it.findtext("pubDate") or "").date().isoformat()
        except (TypeError, ValueError):
            continue
        if date < since:
            continue
        row = _item("FSB", "FSB", title, link, date)
        if row:
            out.append(row)
    return out


# --- fetch + state ------------------------------------------------------------------------
def _get(url: str, get: Callable[..., Any] | None) -> Any:
    import requests

    resp = (get or requests.get)(url, headers=_UA, timeout=(10, 60))
    resp.raise_for_status()
    return resp


def update_state(bucket: str, *, s3: Any | None = None, get: Callable[..., Any] | None = None,
                 today: dt.date | None = None) -> dict[str, Any]:
    if s3 is None:
        import boto3

        s3 = boto3.client("s3")
    try:
        prev = json.loads(s3.get_object(Bucket=bucket, Key=STATE_KEY)["Body"].read())
    except Exception:  # noqa: BLE001 - first run
        prev = {}
    today = today or dt.date.today()
    since = (today - dt.timedelta(days=KEEP_DAYS)).isoformat()
    items = {i["id"]: i for i in prev.get("items") or [] if str(i.get("date") or "") >= since}
    errors: dict[str, str] = {}
    fetched: dict[str, int] = {}
    for name, fn in (("bis", lambda: parse_bis(_get(BIS_JSON, get).json(), since=since)),
                     ("fsb", lambda: parse_fsb(_get(FSB_RSS, get).content, since=since))):
        try:
            rows = fn()
            fetched[name] = len(rows)
            for r in rows:
                r["first_seen"] = (items.get(r["id"]) or {}).get("first_seen") or today.isoformat()
                items[r["id"]] = r
        except Exception as exc:  # noqa: BLE001 - keep previous items of that source
            errors[name] = str(exc)[:300]
    doc = {"as_of": today.isoformat(), "errors": errors,
           "items": sorted(items.values(), key=lambda r: r["date"], reverse=True)}
    s3.put_object(Bucket=bucket, Key=STATE_KEY, ContentType="application/json",
                  Body=json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    return {"items": len(doc["items"]), "fetched": fetched, "errors": sorted(errors)}


# --- feed projection + scoping (pure) -----------------------------------------------------
def for_feed(doc: dict[str, Any] | None, *, today: dt.date | None = None) -> dict[str, Any]:
    if not doc:
        return {}
    cut = ((today or dt.date.today()) - dt.timedelta(days=FEED_DAYS)).isoformat()
    return {"as_of": doc.get("as_of"),
            "items": [{k: v for k, v in i.items() if k != "first_seen"}
                      for i in doc.get("items") or [] if str(i.get("date") or "") >= cut]}


def scope(block: dict[str, Any] | None, industries: Any) -> dict[str, Any]:
    """Industry-level items: visible only to tenants licensed for an industry they concern."""
    if not block:
        return {}
    keep = {str(i).strip().lower() for i in (industries or [])}
    return {**block, "items": [i for i in block.get("items") or []
                               if {str(x).lower() for x in i.get("industries") or []} & keep]}
