"""Rating-action event store (#332, ADR 031 Board M1).

An event is ``entity, agency, date, action, old/new rating, outlook, source``. Two sources:

1. **The bank's own CVM filings (primary).** Read from the #331 IPE history: a material fact
   or market notice whose subject names a rating action. Probed 2026-11-16: 62 such filings
   from financial institutions, 2014–2026. Agency, action and, when the subject says so, the
   rating and outlook come from the subject line. The filing text itself is out of reach:
   CVM's document viewer is behind a CAPTCHA (never bypassed), and the direct download
   link returns an empty form. So ``needs_document`` marks events whose notch or outlook
   must be read by a person from the linked filing.
2. **Agency pointers (link-only).** S&P, Fitch and Moody's restrict reuse of their
   releases. A pointer is a curated row (``config/rating_pointers.json``): date, agency,
   entity, action, the notch if known, and the URL. **Nothing from an agency site is
   fetched or stored.** URLs must be on the agency's own domain.

Known gap: the large banks do not announce rating actions as CVM notices, so the 2015–16
sovereign-driven downgrades of Itaú, Bradesco, BB and Santander are absent from source 1.
Brazil's own sovereign history is published by the Treasury only under a robots-disallowed
path (``sisweb.tesouro.gov.br/apex/``), so it is not scraped either. Those episodes need
pointers.

Store: ``board_history/rating_events/events.json`` in the digests bucket.
"""
from __future__ import annotations

import html
import json
import re
import unicodedata
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

KEY = "board_history/rating_events/events.json"
SCHEMA = "onca/rating-events/v1"
ACTIONS = ("upgrade", "downgrade", "affirm", "assign", "outlook", "withdraw", "review", "report")

AGENCIES = (  # (id, pattern on the folded subject)
    ("sp", r"S\s*&\s*P|STANDARD\s*(?:AND|&)\s*POOR"),
    ("fitch", r"FITCH"),
    ("moodys", r"MOODY"),
    ("austin", r"AUSTIN"),
    ("lfrating", r"LF\s*RATING"),
    ("liberum", r"LIBERUM"),
    ("riskbank", r"RISKBANK"),
)
AGENCY_DOMAINS = {"sp": ("spglobal.com", "standardandpoors.com"), "fitch": ("fitchratings.com",),
                  "moodys": ("moodys.com", "moodyslocal.com", "moodyslocal.com.br")}

_RATING_CUE = re.compile(r"RATING|CLASSIFICACAO DE RISCO|\bIDRS?\b|NOTA DE CREDITO")
_NOT_AN_ACTION = re.compile(r"^LIVE\b|WEBINAR|TELECONFER|APRESENTACAO")
# An outlook CHANGE is a verb applied to the outlook ("elevação da perspectiva"); a subject
# that only mentions the outlook ("afirma …, perspectiva estável") keeps its main action.
_OUTLOOK_CHANGE = re.compile(r"(?<![A-Z])(?:ELEVA|REBAIX|ALTERA|REVIS|MUDANCA|REVISAO)\w*\s+"
                             r"(?:DA\s+|DE\s+|NA\s+)?(?:PERSPECTIVA|OUTLOOK)|"
                             r"(?<![A-Z])(?:PERSPECTIVA|OUTLOOK)\s+(?:ALTERADA|REVISADA|ELEVADA)|"
                             r"(?<![A-Z])(?:OBSERVACAO|WATCH)\b")
# Order matters: the first match wins. Every cue starts at a word start ("Fato RELEVAnte"
# must not read as an upgrade).
_ACTION_CUES = tuple((a, re.compile(r"(?<![A-Z])(?:" + p + ")")) for a, p in (
    ("withdraw", r"RETIRADA|RETIRA\b|WITHDRAW"),
    ("upgrade", r"ELEVA|ELEVOU|UPGRADE|AUMENT"),
    ("downgrade", r"REBAIX|DOWNGRADE|REDUZ|REDUCAO"),
    ("affirm", r"REAFIRM|AFIRM|CONFIRM|MANTEM|MANUTENCAO|RATIFIC"),
    ("assign", r"ATRIBU|RATING INICIAL|NOVO RATING|ASSIGN"),
    ("review", r"REVIS|ATUALIZ|MUDANCA|ALTERA"),
    ("report", r"RELATORIO|MONITORAMENTO|NOTA DE|CLASSIFICACAO"),
))
_OUTLOOK = (("stable", r"ESTAVEL|STABLE"), ("positive", r"POSITIV"), ("negative", r"NEGATIV"))
# National scale (brAA+, AA+(bra), A.br, Aa1.br) and global (BB-, Ba2); quoted or after "PARA"/"EM".
_RATING = re.compile(
    r"(?:'|\"|(?i:\bPARA\s+|\bEM\s+|\bE\s+))\s*("
    r"br(?:AAA|AA|A|BBB|BB|B|CCC|CC|C|D)[+-]?"
    r"|(?:AAA|AA|A|BBB|BB|B|CCC|CC|C|D)[+-]?\((?:bra|BRA)\)"
    r"|(?:Aaa|Aa[1-3]|A[1-3]?|Baa[1-3]|Ba[1-3]|B[1-3]|Caa[1-3]|AAA|AA|A|BBB|BB|B)[+-]?\.br"
    r"|(?:AAA|AA|BBB|BB|CCC|CC)[+-]?|(?:Aaa|Aa[1-3]|Baa[1-3]|Ba[1-3]|Caa[1-3])"
    r")(?=['\"\s,.;)]|$)")


def _fold(s: Any) -> str:
    # spacing accents from broken encodings ("Alterac¸a~o") go first: NFKD would turn each
    # into a space plus a combining mark and split the word
    t = re.sub("[¸~´`^¨]", "", html.unescape(str(s or "")))
    t = unicodedata.normalize("NFKD", t)
    return "".join(c for c in t if not unicodedata.combining(c)).upper()


def scale_of(rating: str | None) -> str | None:
    if not rating:
        return None
    return "national" if ("br" in rating.lower()) else "global"


def parse_subject(subject: Any) -> dict[str, Any] | None:
    """Agency, action, ratings and outlook named in a filing subject, or None when the subject
    is not a rating action. Pure function: the subject line is the only input."""
    raw = html.unescape(str(subject or "")).strip()
    f = _fold(raw)
    agency = next((a for a, p in AGENCIES if re.search(p, f)), None)
    if not (_RATING_CUE.search(f) or agency) or _NOT_AN_ACTION.search(f):
        return None
    action = ("outlook" if _OUTLOOK_CHANGE.search(f)
              else next((a for a, p in _ACTION_CUES if p.search(f)), None))
    if action is None:
        action = "report" if _RATING_CUE.search(f) else None
    if action is None:
        return None
    outlook = next((o for o, p in _OUTLOOK if re.search(p, f)), None)
    ratings = _RATING.findall(raw)
    return {"agency": agency, "action": action, "new_rating": ratings[-1] if ratings else None,
            "ratings_named": ratings, "outlook": outlook,
            "scale": scale_of(ratings[-1]) if ratings else None}


def from_ipe(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rating events from the #331 IPE history rows (material facts and market notices only)."""
    out = []
    for r in rows:
        if r.get("doc_class") not in ("fato_relevante", "comunicado"):
            continue
        p = parse_subject(r.get("subject"))
        if not p:
            continue
        out.append({
            "id": f"rating:cvm:{r.get('protocol') or r.get('id')}",
            "source": "cvm-ipe", "date": r.get("reference_date") or r.get("date"),
            "filed": r.get("date"), "entity_id": r.get("entity_id"),
            "direct_entity_id": r.get("direct_entity_id"), "cnpj_root": r.get("cnpj_root"),
            "company": r.get("company"), **p, "old_rating": None,
            "subject": html.unescape(str(r.get("subject") or "")),
            "url": r.get("url"), "protocol": r.get("protocol"),
            # The notch/outlook are only in the filing text, which needs a person (CAPTCHA).
            "needs_document": p["action"] in ("upgrade", "downgrade", "affirm", "assign", "outlook",
                                              "review") and not (p["new_rating"] and p["outlook"]),
        })
    return out


def validate_pointer(p: Mapping[str, Any]) -> list[str]:
    errs = []
    for k in ("entity_id", "agency", "date", "action", "url"):
        if not p.get(k):
            errs.append(f"missing {k}")
    if p.get("agency") and p["agency"] not in AGENCY_DOMAINS:
        errs.append(f"agency must be one of {sorted(AGENCY_DOMAINS)}")
    if p.get("action") and p["action"] not in ACTIONS:
        errs.append(f"action must be one of {ACTIONS}")
    if p.get("date") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(p["date"])):
        errs.append("date must be YYYY-MM-DD")
    host = (urlparse(str(p.get("url") or "")).hostname or "").lower()
    doms = AGENCY_DOMAINS.get(p.get("agency"), ())
    if p.get("url") and not any(host == d or host.endswith("." + d) for d in doms):
        errs.append("url must be on the agency's own domain (link-only pointer)")
    extra = set(p) - {"entity_id", "agency", "date", "action", "old_rating", "new_rating",
                      "outlook", "url", "note"}
    if extra:
        errs.append(f"unexpected fields {sorted(extra)}: pointers carry no agency text")
    return errs


def from_pointers(pointers: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i, p in enumerate(pointers):
        errs = validate_pointer(p)
        if errs:
            raise ValueError(f"rating pointer {i}: {'; '.join(errs)}")
        out.append({"id": f"rating:pointer:{p['agency']}:{p['entity_id']}:{p['date']}",
                    "source": "agency-pointer", "date": p["date"], "filed": None,
                    "entity_id": p["entity_id"], "agency": p["agency"], "action": p["action"],
                    "old_rating": p.get("old_rating"), "new_rating": p.get("new_rating"),
                    "outlook": p.get("outlook"), "scale": scale_of(p.get("new_rating")),
                    "url": p["url"], "note": p.get("note"), "needs_document": False})
    return out


def build(ipe_rows: Iterable[Mapping[str, Any]], pointers: Iterable[Mapping[str, Any]],
          built_at: str) -> dict[str, Any]:
    events = sorted(from_ipe(ipe_rows) + from_pointers(pointers),
                    key=lambda e: (e.get("date") or "", e["id"]))
    by: dict[str, int] = {}
    for e in events:
        by[e["action"]] = by.get(e["action"], 0) + 1
    return {"schema": SCHEMA, "built_at": built_at, "count": len(events), "by_action": by,
            "needs_document": sum(1 for e in events if e["needs_document"]), "events": events}


def ratings_for(store: Mapping[str, Any], entity_id: str, start: str, end: str) -> list[Mapping[str, Any]]:
    """Replay helper (M2): one entity's rating events between two ISO dates, inclusive."""
    return [e for e in store.get("events") or []
            if e.get("entity_id") == entity_id and start <= (e.get("date") or "") <= end]


def put(s3: Any, bucket: str, store: Mapping[str, Any]) -> None:
    s3.put_object(Bucket=bucket, Key=KEY, ContentType="application/json",
                  Body=json.dumps(store, ensure_ascii=False, indent=1).encode())


def load(s3: Any, bucket: str) -> dict[str, Any] | None:
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=KEY)["Body"].read())
    except Exception as exc:
        if "NoSuchKey" in type(exc).__name__ or "NoSuchKey" in str(exc):
            return None
        raise
