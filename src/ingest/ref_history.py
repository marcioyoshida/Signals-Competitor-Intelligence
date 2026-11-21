"""BCB Financial Stability Report (REF) editions into the KB (#337, ADR 031 Board M1).

Source: the half-yearly *Relatório de Estabilidade Financeira*, probed 2026-11-20:

    https://www.bcb.gov.br/content/publicacoes/ref/{YYYYMM}/RELESTAB{YYYYMM}-{suffix}.pdf

``suffix`` is ``refP`` up to 2015-03 and ``refPub`` from 2015-10. 37 editions answer, 2008-11 to
2026-10 (none before 2008-11 at this path). The folder month is close to, not always equal to,
the cover month (folder 202510 is the November 2025 edition), so the date comes from the cover.
robots.txt allows ``/content/``.

Each edition is cut into its **sections** (chapter or ``N.N`` subsection) by its printed table of
contents, because the KB cuts 300-token chunks and a chunk sees only its sidecar (the ADR 022
chunk-attribution trap): a chunk of "the capital ratio stays above the minimum in every scenario"
must still say *which* report, *which* date and that it is the stress-test section. The PDFs have
no bookmarks; the table of contents gives printed page numbers, and the running header ("Outubro
2015 | Relatório de Estabilidade Financeira | 33") maps printed pages to PDF pages. When either is
missing, the edition falls back to fixed page windows (``parse_mode: "windows"``).

Point in time: ``date`` is the **last day of the cover month**. The exact release day is not in
the PDF, and a board pack as of a date must never cite a report published after it, so the
conservative date is used.

Stores: the KB data-source bucket under ``BCB-REF/{edition}/{nn}-{section}.txt`` plus a
``.metadata.json`` sidecar, and the manifest ``board_history/ref/manifest.json`` in the digests
bucket (sections, PDF pages, stress flags) for the context pack. It makes no model calls; the
KB ingestion job embeds the text.
"""
from __future__ import annotations

import calendar
import collections
import io
import json
import re
import time
import unicodedata
from typing import Any, Callable, Iterable, Mapping

BASE = "https://www.bcb.gov.br/content/publicacoes/ref/{ym}/RELESTAB{ym}-{suffix}.pdf"
SUFFIXES = ("refPub", "refP")
FIRST_YM = "200811"
KB_PREFIX = "BCB-REF/"
MANIFEST_KEY = "board_history/ref/manifest.json"
SCHEMA = "onca/ref-manifest/v1"
SOURCE = "BCB-REF"
DOC_TYPE = "Relatório de Estabilidade Financeira"
WINDOW = 8
UA = {"User-Agent": "Mozilla/5.0 (compatible; onca-ingest)"}

MONTHS = {"janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
          "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12}
ABBR = ["", "jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


def url(ym: str, suffix: str) -> str:
    return BASE.format(ym=ym, suffix=suffix)


def months(start: str, end: str) -> list[str]:
    y, m = int(start[:4]), int(start[4:])
    out = []
    while f"{y}{m:02d}" <= end:
        out.append(f"{y}{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def discover(candidates: Iterable[str], head: Callable[[str], int] | None = None,
             pause: float = 0.15) -> dict[str, str]:
    """{folder YYYYMM: PDF url} for the months that answer 200."""
    if head is None:
        import requests

        def head(u: str) -> int:
            return requests.head(u, headers=UA, timeout=30, allow_redirects=True).status_code
    out = {}
    for ym in candidates:
        for s in SUFFIXES:
            u = url(ym, s)
            try:
                code = head(u)
            except Exception:  # noqa: BLE001 - a flaky probe is a miss, retried next run
                code = 0
            time.sleep(pause)
            if code == 200:
                out[ym] = u
                break
    return out


def fetch_pdf(u: str, tries: int = 5) -> bytes:
    import requests
    last: Exception | None = None
    for i in range(tries):
        try:
            r = requests.get(u, headers=UA, timeout=180)
            r.raise_for_status()
            if not r.content.startswith(b"%PDF"):
                raise ValueError("not a PDF")
            return r.content
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(3 * (i + 1))
    raise last  # type: ignore[misc]


def pdf_pages(content: bytes) -> list[str]:
    """Text per page. A page whose font dictionary pypdf cannot read (seen in older editions:
    a Type0 font without /DescendantFonts) becomes "" instead of failing the edition."""
    from pypdf import PdfReader
    out = []
    for p in PdfReader(io.BytesIO(content)).pages:
        try:
            out.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001
            out.append("")
    return out


# --- text -------------------------------------------------------------------------------

def clean(text: str) -> str:
    """pypdf artefacts: tabs between words, split ligatures ("ﬁ nanceiro"), hyphenated breaks."""
    t = unicodedata.normalize("NFC", text.replace("\t", " "))
    t = re.sub(r"ﬁ\s?", "fi", t)
    t = re.sub(r"ﬂ\s?", "fl", t)
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)
    t = re.sub(r"[  ]+", " ", t)
    return t


def _first_line(text: str) -> str:
    for ln in clean(text).splitlines():
        if ln.strip():
            return ln.strip()
    return ""


def printed_number(text: str) -> int | None:
    """The page number in the running header, or None. Layouts: "12 | Relatório … | Novembro
    2008", "Março 2012 | Relatório … | 12", and 2026's "Anexo estatístico13"."""
    ln = _first_line(text)
    if "|" in ln:
        parts = [p.strip() for p in ln.split("|")]
        for p in (parts[0], parts[-1]):
            if re.fullmatch(r"\d{1,3}", p):
                return int(p)
        return None
    # glued to a letter ("estatístico13"); "Temas Selecionados, 59" is a contents line, not a header
    m = re.fullmatch(r"[^\d|,]{2,40}[^\W\d_](\d{1,3})", ln)
    return int(m.group(1)) if m else None


def page_offset(pages: list[str], min_votes: int = 3) -> int | None:
    """PDF index (0-based) = printed page + offset, by majority over the headers."""
    votes = collections.Counter(i - n for i, t in enumerate(pages) if (n := printed_number(t)) is not None)
    if not votes:
        return None
    off, n = votes.most_common(1)[0]
    return off if n >= min_votes else None


def strip_header(text: str) -> str:
    t = clean(text)
    lines = t.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip():
            if printed_number(ln) is not None or "Relatório de Estabilidade Financeira" in ln and "|" in ln:
                lines = lines[:i] + lines[i + 1:]
            break
    return "\n".join(lines).strip()


# --- cover ------------------------------------------------------------------------------

def cover_month(pages: list[str], folder: str) -> str:
    """'YYYY-MM' from the cover or the running header; the folder month when neither reads."""
    for t in pages[:3] + pages[5:15]:
        c = clean(t).lower()
        m = re.search(r"\b(" + "|".join(MONTHS) + r")\s+(?:de\s+)?(20\d\d)\b", c)
        if m:
            return f"{m.group(2)}-{MONTHS[m.group(1)]:02d}"
        m = re.search(r"\b(" + "|".join(ABBR[1:]) + r")\.\s+(20\d\d)\b", c)
        if m:
            return f"{m.group(2)}-{ABBR.index(m.group(1)):02d}"
    return f"{folder[:4]}-{folder[4:]}"


def month_end(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{ym}-{calendar.monthrange(y, m)[1]:02d}"


def volume(pages: list[str]) -> str | None:
    for t in pages[:4]:
        m = re.search(r"Volume\s+(\d+)\s*\|\s*Número\s+(\d+)", clean(t)) or \
            re.search(r"\bv\.\s*(\d+)\s*n[º°o]\s*(\d+)", clean(t))
        if m:
            return f"v. {m.group(1)}, n. {m.group(2)}"
    return None


# --- table of contents ------------------------------------------------------------------

_ENTRY = re.compile(r"^(?P<num>\d+(?:\.\d+)*)?\s*(?P<title>.*?[^\d\s,._])[\s,._]*(?P<page>\d{1,3})$")
_NUM = re.compile(r"^\d+(?:\.\d+)*\s")


def toc_pages(pages: list[str], scan: int = 8) -> list[int]:
    """The table-of-contents pages: one starting with "Sumário"/"Índice" in the first pages,
    plus the following pages while they carry no running header and still list entries."""
    for i in range(min(scan, len(pages))):
        head = " ".join(clean(pages[i]).split()[:3]).lower()
        if head.startswith(("sumário", "índice", "indice")):
            out = [i]
            j = i + 1
            while j < len(pages) and printed_number(pages[j]) is None and len(toc_entries([pages[j]])) >= 5:
                out.append(j)
                j += 1
            return out
    # no heading read (2024-11's contents page extracts without its top): a header-less early
    # page that lists entries
    for i in range(1, min(scan, len(pages))):
        if printed_number(pages[i]) is None and len(toc_entries([pages[i]])) >= 8:
            return [i]
    return []


def toc_entries(texts: Iterable[str]) -> list[dict[str, Any]]:
    """Entries with a trailing printed page. A line with no page number is the start of a
    wrapped title (joined to the next line), unless the next line starts a numbered entry."""
    out: list[dict[str, Any]] = []
    buf: list[str] = []
    for t in texts:
        for raw in clean(t).splitlines():
            ln = re.sub(r"_{2,}|\.{3,}", " ", raw).strip()
            if not ln or ln.lower() in ("sumário", "índice", "indice"):
                continue
            if _NUM.match(ln):
                buf = []
            joined = " ".join(buf + [ln]).strip()
            m = _ENTRY.match(joined)
            if m and not re.fullmatch(r"\d+", joined):
                title = re.sub(r"\s+", " ", m.group("title")).strip(" –-,")
                out.append({"num": m.group("num"), "title": title, "page": int(m.group("page"))})
                buf = []
            else:
                buf = (buf + [ln])[-2:]
    return out


def outline(entries: list[dict[str, Any]], n_pages: int, offset: int) -> list[dict[str, Any]]:
    """Section starts (PDF index) from the TOC: numbered entries to depth 2, plus unnumbered
    ones before the first or after the last numbered entry (preface, executive summary,
    annexes). Unnumbered entries inside the numbered span are boxes listed apart from their
    chapter; they would split a section under the wrong title, so they are dropped. A depth-2
    title keeps its chapter title in front."""
    numbered = [e for e in entries if e["num"]]
    if not numbered:
        return []
    lo, hi = min(e["page"] for e in numbered), max(e["page"] for e in numbered)
    chapters: dict[str, str] = {}
    keep = []
    extras = []          # stress sections below depth 2 or listed as boxes: bounded separately
    for e in entries:
        depth = e["num"].count(".") + 1 if e["num"] else 0
        if e["num"] and depth == 1:
            chapters[e["num"]] = e["title"]
        regular = (e["num"] and depth <= 2) or (not e["num"] and not lo <= e["page"] <= hi)
        title = re.sub(r"^Boxes\s+", "", e["title"])
        if not regular and not is_stress(title):
            continue
        idx = e["page"] + offset
        if not 0 <= idx < n_pages:
            continue
        label = f"{e['num']} {title}" if e["num"] else title
        ch = (e["num"] or "").split(".")[0]
        if e["num"] and depth >= 2 and chapters.get(ch):
            label = f"{ch} {chapters[ch]} › {label}"
        sec = {"num": e["num"], "label": label, "start": idx, "rank": 0 if regular else 1}
        (keep if regular else extras).append(sec)
    by_page = sorted({e["page"] + offset for e in entries})
    for x in extras:
        host = max((s for s in keep if s["rank"] == 0 and s["start"] <= x["start"]),
                   key=lambda s: s["start"], default=None)
        if host and is_stress(host["label"]):
            continue          # "2.6 Testes de estresse" already covers its 2.6.1, unsplit
        keep.append(x)
        nxt = next((p for p in by_page if p > x["start"]), None)
        if nxt is not None and nxt < n_pages and host:
            # the pages after the stress section belong to its host section again
            keep.append({"num": host["num"], "label": f"{host['label']} (continuação)", "start": nxt, "rank": 2})
    keep.sort(key=lambda s: (s["start"], s["rank"]))
    merged: list[dict[str, Any]] = []
    for s in keep:
        if merged and merged[-1]["start"] == s["start"]:
            prev = merged[-1]
            if s["rank"] == 1 or (s["rank"] == 0 and s["num"] and prev["rank"] == 0
                                  and (not prev["num"] or s["num"].startswith(prev["num"] + "."))):
                # "1 Ambiente…" and "1.1 Introdução" on the same page: the deeper one names it;
                # a stress section starting on its host's first page names that page
                merged[-1] = {**s, "label": s["label"] if "›" in s["label"] or s["rank"] == 1
                              else f"{prev['label']} › {s['label']}"}
            continue
        merged.append(s)
    for a, b in zip(merged, merged[1:] + [None]):
        a["end"] = (b["start"] if b else n_pages) - 1
    return [{k: v for k, v in s.items() if k != "rank"} for s in merged if s["end"] >= s["start"]]


def windows(start: int, end: int, size: int = WINDOW) -> list[dict[str, Any]]:
    """Fixed page windows over PDF indexes [start, end)."""
    return [{"num": None, "label": f"páginas {i + 1}–{min(i + size, end)}", "start": i,
             "end": min(i + size, end) - 1} for i in range(start, end, size)]


def is_stress(label: str) -> bool:
    return "estresse" in label.lower() or "stress" in label.lower()


_STRESS_TEXT = re.compile(r"testes? de estresse", re.I)


def stress_text(text: str, min_hits: int = 3) -> bool:
    """A window (no title to go by) that discusses stress tests: at least ``min_hits`` mentions."""
    return len(_STRESS_TEXT.findall(text)) >= min_hits


def sections(pages: list[str]) -> tuple[list[dict[str, Any]], str]:
    """(sections, parse_mode). ``toc``: the table of contents and the header page numbers both
    read. ``partial``: the contents page extracted only in part (2024-11 loses its first half),
    so the pages before the first listed section are fixed windows. ``windows``: neither read."""
    off = page_offset(pages)
    toc = toc_pages(pages)
    if off is not None and toc:
        secs = outline(toc_entries(pages[i] for i in toc), len(pages), off)
        if len(secs) >= 3:
            first = secs[0]["start"]
            if first <= 12:
                if first > 0:
                    secs.insert(0, {"num": None, "label": "Capa e sumário", "start": 0, "end": first - 1})
                return secs, "toc"
            return windows(0, first) + secs, "partial"
    return windows(0, len(pages)), "windows"


# --- edition -----------------------------------------------------------------------------

def _slug(s: str, n: int = 40) -> str:
    a = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", a).strip("-")[:n].strip("-") or "secao"


def edition(folder: str, pdf_url: str, pages: list[str]) -> dict[str, Any]:
    """One edition: cover month, conservative date, its sections with text and KB keys."""
    ym = cover_month(pages, folder)
    secs, mode = sections(pages)
    vol = volume(pages)
    when = f"{ABBR[int(ym[5:])]}/{ym[:4]}"
    out = []
    for n, s in enumerate(secs, 1):
        body = "\n".join(strip_header(pages[i]) for i in range(s["start"], s["end"] + 1)).strip()
        if len(body) < 200:
            continue
        key = f"{KB_PREFIX}{ym}/{n:02d}-{_slug(s['num'] or s['label'])}.txt"
        window = s["label"].startswith("páginas ")
        stress = is_stress(s["label"]) or (window and stress_text(body))
        out.append({"key": key, "num": s["num"], "label": s["label"], "pdf_pages": [s["start"] + 1, s["end"] + 1],
                    "stress": stress, "stress_basis": ("text" if window else "title") if stress else None,
                    "chars": len(body), "text": body,
                    "title": f"REF {when} (BCB) — {s['label']}"[:240]})
    return {"folder": folder, "edition": ym, "date": month_end(ym), "volume": vol, "url": pdf_url,
            "pages": len(pages), "parse_mode": mode, "sections": out}


def kb_document(ed: Mapping[str, Any], sec: Mapping[str, Any]) -> str:
    """Header naming the report, edition and section. Never starts with "From " (KB mailbox sniff)."""
    head = (f"Relatório de Estabilidade Financeira (REF) do Banco Central do Brasil — "
            f"edição {ed['edition']}{' (' + ed['volume'] + ')' if ed.get('volume') else ''}\n"
            f"Seção: {sec['label']}\n"
            f"Páginas do PDF: {sec['pdf_pages'][0]}–{sec['pdf_pages'][1]}\n"
            f"Fonte: {ed['url']}\n\n")
    return head + sec["text"]


def kb_metadata(ed: Mapping[str, Any], sec: Mapping[str, Any]) -> dict[str, Any]:
    """Small (S3 Vectors caps filterable metadata at 2 KB per vector). ``title`` names the
    edition and section, so a chunk of generic system prose keeps both."""
    meta: dict[str, Any] = {"source": SOURCE, "kind": "system", "doc_type": DOC_TYPE, "date": ed["date"],
                            "url": ed["url"], "edition": ed["edition"], "section": sec["num"] or sec["label"][:60],
                            "title": sec["title"], "name": "Banco Central do Brasil"}
    if sec["stress"]:
        meta["stress"] = True
    return meta


def manifest_entry(ed: Mapping[str, Any]) -> dict[str, Any]:
    return {**{k: v for k, v in ed.items() if k != "sections"},
            "sections": [{k: v for k, v in s.items() if k != "text"} for s in ed["sections"]]}


# --- store ------------------------------------------------------------------------------

def _keys(s3: Any, bucket: str, prefix: str) -> list[str]:
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        out += [o["Key"] for o in page.get("Contents", [])]
    return out


def write_edition(s3: Any, bucket: str, ed: Mapping[str, Any], *, force: bool = False) -> int:
    """Write an edition's sections + sidecars. An edition already present is skipped unless
    ``force``, which first removes that edition's own objects (a re-cut must not leave stale
    sections behind for the KB to keep serving). Returns the sections written."""
    prefix = f"{KB_PREFIX}{ed['edition']}/"
    old = _keys(s3, bucket, prefix)
    if old and not force:
        return 0
    for i in range(0, len(old), 1000):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in old[i:i + 1000]]})
    for sec in ed["sections"]:
        s3.put_object(Bucket=bucket, Key=sec["key"], Body=kb_document(ed, sec).encode("utf-8"),
                      ContentType="text/plain; charset=utf-8")
        s3.put_object(Bucket=bucket, Key=sec["key"] + ".metadata.json",
                      Body=json.dumps({"metadataAttributes": kb_metadata(ed, sec)}, ensure_ascii=False).encode("utf-8"),
                      ContentType="application/json")
    return len(ed["sections"])


def put_manifest(s3: Any, bucket: str, entries: Iterable[Mapping[str, Any]], built_at: str) -> dict[str, Any]:
    eds = sorted(entries, key=lambda e: e["edition"])
    doc = {"schema": SCHEMA, "built_at": built_at, "source": BASE, "editions": eds}
    s3.put_object(Bucket=bucket, Key=MANIFEST_KEY, ContentType="application/json",
                  Body=json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return doc


def load_manifest(s3: Any, bucket: str) -> dict[str, Any] | None:
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=MANIFEST_KEY)["Body"].read())
    except Exception:  # noqa: BLE001
        return None


# --- read -------------------------------------------------------------------------------

def edition_at(manifest: Mapping[str, Any], as_of: str) -> dict[str, Any] | None:
    """The latest edition whose conservative date is on or before ``as_of`` (no look-ahead)."""
    eds = [e for e in (manifest or {}).get("editions") or [] if e["date"] <= as_of[:10]]
    return max(eds, key=lambda e: e["date"]) if eds else None


def stress_at(manifest: Mapping[str, Any], as_of: str) -> dict[str, Any] | None:
    """The stress-test sections of the edition in force at ``as_of``: what the context pack
    cites for system-level stress on that date (KB keys, titles, PDF pages)."""
    ed = edition_at(manifest, as_of)
    if not ed:
        return None
    return {"edition": ed["edition"], "date": ed["date"], "url": ed["url"],
            "sections": [s for s in ed["sections"] if s.get("stress")]}
