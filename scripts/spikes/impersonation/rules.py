"""Alert rule for an ACTIONABLE impersonation (#160). Fixed BEFORE any data was pulled.

Ported from Bluefin `creator_radar/impersonation.py` (ADR 0004): official-set diff, deterministic
and explainable, every hit a PROPOSAL until a human verifies it. Not imported across repos.

A channel is an **active impersonation** only if ALL of:
  L  look-alike: NOT in the issuer's official allowlist, AND its title or handle contains an issuer
     brand alias (accent/case/space-insensitive) or has name similarity >= 0.75 to the official
     channel title / App Store title;
  H  at least one active harm signal:
       H1 offer terms in the channel description or an in-window video title/description:
          suporte, atendimento, central (de ajuda/atendimento), desbloqueio, empréstimo, Pix
          offered as a service (e.g. "pix na hora", "receba via pix"), "fale conosco",
          "recuperar conta", "liberar limite/conta"
       H2 a phone / WhatsApp number or wa.me / api.whatsapp link
       H3 a link to a domain that is neither the issuer's official domain nor a mainstream
          platform (YouTube/Instagram/TikTok/Facebook/X/LinkedIn/app stores); shorteners count
       H4 upload surge: >= 3 public uploads in the window from a channel with 0 uploads before it
          (or created in the window);
  W  in the 30-day window: the harm-bearing content (a video) was published in the window, or the
     channel was created in the window. A harm signal that exists only in an undated channel
     description of an older channel is reported as "active look-alike, undated" and NOT counted.
  V  hand-verified: the channel presents itself AS the issuer or its support (not an independent
     creator/news/reviewer who merely names the brand), and the harm signal is quoted from an API
     fetch. Unverified = miss.

Anything with L but not H+W+V is a "look-alike" (or "dormant" if 0 videos / 0 subscribers).
Kill criterion (issue #160, fixed before building): fewer than 2 active impersonations across the
5 retail issuers in the 30-day window -> NO-GO.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from urllib.parse import urlparse

OFFICIAL_DOMAINS = {
    "nubank": {"nubank.com.br", "nu.com.br", "nubank.com", "nu.com.mx", "nu.com.co", "blog.nubank.com.br"},
    "inter": {"bancointer.com.br", "inter.co", "interinvest.com.br"},
    "picpay": {"picpay.com", "picpay.com.br"},
    "mercado_pago": {"mercadopago.com.br", "mercadopago.com", "mercadolivre.com.br", "mercadolibre.com"},
    "c6": {"c6bank.com.br", "c6bank.com", "c6investimentos.com.br"},
}
PLATFORM_DOMAINS = {"youtube.com", "youtu.be", "instagram.com", "tiktok.com", "facebook.com", "fb.com",
                    "x.com", "twitter.com", "linkedin.com", "apps.apple.com", "itunes.apple.com",
                    "play.google.com", "threads.net", "spotify.com", "open.spotify.com", "kwai.com"}
SHORTENERS = {"bit.ly", "tinyurl.com", "cutt.ly", "encurtador.com.br", "l1nk.dev", "linktr.ee", "is.gd",
              "t.ly", "rb.gy", "abre.ai", "encr.pw"}

OFFER_RE = re.compile(
    r"\b(suporte|atendimento|central de (?:ajuda|atendimento|relacionamento)|desbloque\w*|"
    r"empr[eé]stimo\w*|fale conosco|recupera(?:r|ção de) (?:conta|acesso)|liberar (?:limite|conta|pix)|"
    r"pix na hora|receba (?:via|no|por) pix|chave pix)\b", re.IGNORECASE)
PHONE_RE = re.compile(r"(?:\+?55\s*)?\(?\b[1-9]{2}\)?\s*9?\s?\d{4}[-.\s]?\d{4}\b|0800[\s-]?\d{3}[\s-]?\d{4}")
WA_RE = re.compile(r"(wa\.me/|api\.whatsapp\.com|whatsapp|\bzap\b)", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s)\]>\"']+", re.IGNORECASE)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def is_lookalike(subject: dict, title: str, handle: str = "", app_title: str = "") -> tuple[bool, float]:
    t, h = norm(title), norm(handle)
    aliases = [norm(a) for a in subject["aliases"] if len(norm(a)) >= 5]
    if any(a in t or (h and a in h) for a in aliases):
        return True, 1.0
    anchors = [norm(ch["title"]) for ch in subject["youtube_channels"]] + ([norm(app_title)] if app_title else [])
    sim = max(difflib.SequenceMatcher(None, t, a).ratio() for a in anchors if a)
    return sim >= 0.75, round(sim, 2)


def _dom(u: str) -> str:
    d = (urlparse(u).hostname or "").lower()
    return d[4:] if d.startswith("www.") else d


def harm_signals(subject_id: str, text: str) -> list[dict]:
    """H1-H3 on one text blob. Returns [{signal, match}] (the quoted evidence)."""
    hits = []
    for m in OFFER_RE.finditer(text or ""):
        hits.append({"signal": "H1_offer", "match": m.group(0)})
    for m in WA_RE.finditer(text or ""):
        hits.append({"signal": "H2_whatsapp", "match": m.group(0)})
    for m in PHONE_RE.finditer(text or ""):
        hits.append({"signal": "H2_phone", "match": m.group(0)})
    off = OFFICIAL_DOMAINS.get(subject_id, set())
    for u in URL_RE.findall(text or ""):
        d = _dom(u)
        if not d or any(d == o or d.endswith("." + o) for o in off):
            continue
        if any(d == p or d.endswith("." + p) for p in PLATFORM_DOMAINS) and d not in SHORTENERS:
            continue
        hits.append({"signal": "H3_offdomain", "match": u[:120]})
    return hits
