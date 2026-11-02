"""ADR 022 Phase 6 — Pilar 3 (Relatório de Gerenciamento de Riscos) risk-report text.

The real qualitative risk-posture corpus behind the numbers. Discovered via the **CVM IPE** feed
(`cvm_ipe`, already ingested) — listed institutions file their Pilar 3 as a *Comunicado ao Mercado*
whose `Assunto` carries both "Gerenciamento de Riscos" and "Pilar", with a direct `Link_Download`
(rad.cvm.gov.br). We download the PDF (the ENET endpoint mislabels it `text/html` but the body is a
real `%PDF`), extract the qualitative risk-narrative sentences, resolve to a tracked entity, and
persist a durable `pilar3/index.json` per-entity corpus.

This corpus is what ADR 022 Phase 5 (`financial_tone`) reads INSTEAD of the fact-paraphrases once
available — so a competitor's financial tone comes from its *own risk report's language*, not a
circular restatement of its Basileia.

**KB grounding (#299, the other half of Phase 6):** `promote_to_kb` writes each filing's risk-
management *prose* (`prose_text`: page headers, the table of contents and table rows dropped) to the
KB data-source bucket under `pilar3/<entity>/<filing date>.txt` + a metadata sidecar carrying the CVM
filing URL, so the agent can answer "how does Itaú describe its liquidity risk?" from the bank's own
filing and cite it. Seen-set = the object's existence: a filing is written once.

**Coverage note:** IPE covers institutions that file Pilar 3 through CVM (Itaú today). Others publish
on their IR sites (mziq/own CDN); those are additional per-source fetchers, not this module's job.
"""
from __future__ import annotations

import datetime as dt
import io
import re
from typing import Any, Callable

import requests

INDEX_KEY = "pilar3/index.json"
_RISK_TERMS = ("risco", "capital", "basileia", "liquidez", "crédito", "provis", "gestão",
               "gerenciamento", "exposição", "estresse", "estratég", "prudencial", "solvência")
_SENT = re.compile(r"[^.!?]{60,320}[.!?]")


def is_pilar3(subject: str) -> bool:
    s = (subject or "").lower()
    return "gerenciamento de risco" in s and "pilar" in s


def find_filings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Real Pilar 3 filings from cvm_ipe rows (both terms present — excludes 'Pilar S.A.' noise)."""
    return [r for r in rows if is_pilar3(r.get("subject") or "")]


def fetch_pdf_pages(url: str, *, max_pages: int = 20, timeout: int = 120) -> list[str]:
    """Download a Pilar 3 PDF (ENET mislabels content-type) and extract the leading pages' text."""
    from pypdf import PdfReader

    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    if b"%PDF" not in resp.content[:2000]:
        raise requests.RequestException("not a PDF payload")
    reader = PdfReader(io.BytesIO(resp.content))
    n = min(max_pages, len(reader.pages))
    return [reader.pages[i].extract_text() or "" for i in range(n)]


def fetch_pdf_text(url: str, *, max_pages: int = 20, timeout: int = 120) -> str:
    return "\n".join(fetch_pdf_pages(url, max_pages=max_pages, timeout=timeout))


_TEMPLATE_CODE = re.compile(r"^[A-Z]{2,6}\d{0,2}[a-z]?:\s")       # "CRA: Informações qualitativas…"
_TOC_RUN = re.compile(r"\s\d{1,3}\s[A-Z]{2,6}\d?:")                 # "… 37 CR7: Efeitos…"
_TOC_LINE = re.compile(r"\s\d{1,3}$")


def prose_text(pages: list[str], *, min_words: int = 20) -> str:
    """The report's risk-management prose, one paragraph per block.

    A Pilar 3 PDF is mostly BCB template tables. Dropped: lines repeated on most pages (title, bank
    name, rules), page numbers, table-of-contents lines, table-dense lines, template headings
    ("CR1: …"), and any block that is not sentence-shaped (≥ ``min_words`` words, ending in
    punctuation, ≤ 5% digits). Numbers live in KM1 (``bcb_km1``); this is the text an officer asks
    about. On the live Itaú 2T26 filing: 248k extracted chars → ~99k of prose."""
    from collections import Counter

    lines_per_page = [{ln.strip() for ln in p.splitlines() if ln.strip()} for p in pages]
    counts = Counter(ln for page in lines_per_page for ln in page)
    header = {ln for ln, c in counts.items() if len(pages) >= 4 and c >= len(pages) * 0.5}
    paras: list[str] = []
    for page in pages:
        buf: list[str] = []
        for raw in page.splitlines():
            ln = raw.strip()
            toc = (_TOC_LINE.search(ln) and len(ln) < 140 and ln[:1].isupper()
                   and not ln.endswith("."))
            if (not ln or ln in header or re.fullmatch(r"[_\-\s]+|\d{1,3}", ln) or toc
                    or sum(c.isdigit() for c in ln) > len(ln) * 0.15):
                if buf:
                    paras.append(" ".join(buf))
                    buf = []
                continue
            buf.append(ln)
            if ln.endswith((".", ":", ";")):
                paras.append(" ".join(buf))
                buf = []
        if buf:
            paras.append(" ".join(buf))
    out: list[str] = []
    seen: set[str] = set()
    for p in paras:
        p = re.sub(r"\s+", " ", p).strip()
        if (len(p.split()) < min_words or not p.endswith((".", ";", ":"))
                or sum(c.isdigit() for c in p) > len(p) * 0.05
                or _TEMPLATE_CODE.match(p) or _TOC_RUN.search(p)):
            continue
        if p[:80] in seen:
            continue
        seen.add(p[:80])
        out.append(p)
    return "\n\n".join(out)


def risk_sentences(text: str, *, limit: int = 8) -> list[str]:
    """Clean qualitative risk-narrative sentences: drop rule-lines/underscores and table-dense text,
    keep prose that carries risk-management language."""
    flat = re.sub(r"[_]{3,}", " ", text or "").replace("\n", " ")
    flat = re.sub(r"\s+", " ", flat)
    out: list[str] = []
    seen: set[str] = set()
    for m in _SENT.finditer(flat):
        s = m.group().strip()
        words = s.split()
        digits = sum(c.isdigit() for c in s)
        num_tokens = sum(1 for w in words if w.strip("().,%").isdigit())
        if len(words) < 12 or digits > len(s) * 0.12:          # skip short / table-dense
            continue
        if num_tokens > 2:                                     # skip TOC/index lines (page numbers)
            continue
        if not s[:1].isupper() or not any(k in s.lower() for k in _RISK_TERMS):
            continue                                            # real sentence starts capitalised
        key = s[:40].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def build_corpus(rows: list[dict[str, Any]], *, resolver: Callable[[dict[str, Any]], list[str]],
                 today: dt.date | None = None, max_entities: int = 25) -> dict[str, Any]:
    """Latest Pilar 3 filing per tracked entity → {entity: {sentences, subject, date, url}}."""
    today = today or dt.date.today()
    filings = sorted(find_filings(rows), key=lambda r: r.get("date") or "", reverse=True)
    records: dict[str, dict[str, Any]] = {}
    for r in filings:
        if len(records) >= max_entities:
            break
        try:
            ents = resolver({"source": "News", "title": r.get("company") or "",
                             "institution": r.get("company") or ""}) or []
        except Exception:  # pragma: no cover
            ents = []
        for eid in ents:
            if eid in records:      # keep the latest (already date-sorted)
                continue
            try:
                sents = risk_sentences(fetch_pdf_text(r.get("url") or ""))
            except Exception as exc:  # pragma: no cover - network/PDF
                print(f"Warning: pilar3 fetch failed for {eid}: {exc}")
                sents = []
            if sents:
                records[eid] = {"entity": eid, "sentences": sents, "company": r.get("company"),
                                "subject": r.get("subject"), "date": r.get("date"), "url": r.get("url")}
    return {"as_of": today.isoformat(), "count": len(records), "records": records}


def corpus_by_entity(index: dict[str, Any]) -> dict[str, list[str]]:
    """{entity_id: [sentences]} — what financial_tone reads as the real tone corpus."""
    return {eid: r.get("sentences") or []
            for eid, r in ((index or {}).get("records") or {}).items() if r.get("sentences")}


def load_index(bucket: str, *, s3: Any | None = None) -> dict[str, Any]:
    import json

    import boto3

    s3 = s3 or boto3.client("s3")
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=INDEX_KEY)["Body"].read())
    except Exception:  # pragma: no cover
        return {}


def publish(index: dict[str, Any], bucket: str, *, s3: Any | None = None) -> str:
    import json

    import boto3

    s3 = s3 or boto3.client("s3")
    # In-memory fields (``_prose``) never reach the index: the KB holds the text.
    clean = {**index, "records": {e: {k: v for k, v in r.items() if not k.startswith("_")}
                                  for e, r in (index.get("records") or {}).items()}}
    s3.put_object(Bucket=bucket, Key=INDEX_KEY,
                  Body=json.dumps(clean, ensure_ascii=False, indent=2).encode("utf-8"),
                  ContentType="application/json")
    return f"s3://{bucket}/{INDEX_KEY}"


KB_PREFIX = "pilar3/"
KB_SOURCE = "CVM-Pilar3"
KB_DOC_TYPE = "Pilar 3 — Gerenciamento de Riscos e Capital"
KB_MAX_CHARS = 150_000
KB_MAX_PAGES = 200


def kb_key(rec: dict[str, Any]) -> str:
    if rec.get("kind") == "dasfn":         # annual tables: keyed by year, so a capped date is stable
        return f"{KB_PREFIX}{rec['entity']}/dasfn-{rec.get('year')}.txt"
    date = (rec.get("date") or "")[:10] or "sem-data"
    return f"{KB_PREFIX}{rec['entity']}/{date}.txt"


def kb_document(rec: dict[str, Any], prose: str, *, max_chars: int = KB_MAX_CHARS) -> str:
    """Header + prose. The header names the bank and the filing, so every chunk the KB cuts keeps
    its subject in the sidecar metadata, and the document never starts with "From " (the KB's
    mailbox sniff rejects such a .txt)."""
    who = rec.get("company") or rec.get("entity")
    title = ("Pilar 3 — tabelas qualitativas de gerenciamento de riscos" if rec.get("kind") == "dasfn"
             else "Relatório de Gerenciamento de Riscos e Capital (Pilar 3)")
    head = (f"{title} — {who}\n"
            f"Documento: {rec.get('subject') or KB_DOC_TYPE}\n"
            f"Data de entrega à CVM: {(rec.get('date') or '')[:10]}\n"
            f"Fonte: {rec.get('url') or ''}\n\n")
    return head + prose[:max_chars]


def promote_to_kb(index: dict[str, Any], *, bucket: str, s3: Any | None = None,
                  fetch_pages: Callable[..., list[str]] | None = None) -> list[str]:
    """Write each filing in the Pilar 3 index not yet in the KB bucket (+ metadata sidecar).
    Returns the keys written; the caller starts one ingestion job when the list is non-empty.
    A filing already present is skipped (a re-filed quarter carries a new date, so a new key)."""
    import json

    if s3 is None:
        import boto3
        s3 = boto3.client("s3")
    fetch_pages = fetch_pages or (lambda url: fetch_pdf_pages(url, max_pages=KB_MAX_PAGES))
    written: list[str] = []
    # One document per FILING: the registry can resolve one bank to several ids (itau and
    # itau-unibanco both matched Itaú's 2T26 filing on 2026-11-02). Key it to the first id in sort
    # order, so the choice is the same every run and the existence seen-set holds.
    by_url: dict[str, dict[str, Any]] = {}
    for rec in sorted(((index or {}).get("records") or {}).values(), key=lambda r: r.get("entity") or ""):
        if rec.get("entity") and rec.get("url"):
            by_url.setdefault(rec["url"], rec)
    for rec in by_url.values():
        dasfn = rec.get("kind") == "dasfn"
        if dasfn and not rec.get("_prose"):
            continue                       # a stored DASFN record: its prose is only in this run
        key = kb_key(rec)
        try:
            s3.head_object(Bucket=bucket, Key=key)
            continue                                         # seen: already in the KB bucket
        except Exception:
            pass
        try:
            prose = rec["_prose"] if dasfn else prose_text(fetch_pages(rec["url"]))
        except Exception as exc:  # pragma: no cover - network/PDF
            print(f"Warning: pilar3 KB fetch failed for {rec['entity']}: {exc}")
            continue
        if not prose:
            continue
        from src.ingest.pilar3_dasfn import DOC_TYPE as _DASFN_DOC_TYPE
        meta = {"source": "BCB-DASFN-Pilar3" if dasfn else KB_SOURCE, "kind": "competitor",
                "doc_type": _DASFN_DOC_TYPE if dasfn else KB_DOC_TYPE,
                "date": (rec.get("date") or "")[:10], "url": rec["url"], "entity": rec["entity"],
                "name": rec.get("company") or rec["entity"], "title": rec.get("subject")}
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=kb_document(rec, prose).encode("utf-8"),
                          ContentType="text/plain; charset=utf-8")
            s3.put_object(Bucket=bucket, Key=key + ".metadata.json",
                          Body=json.dumps({"metadataAttributes": {k: v for k, v in meta.items() if v}},
                                          ensure_ascii=False).encode("utf-8"),
                          ContentType="application/json")
        except Exception as exc:  # pragma: no cover - write best-effort
            print(f"Warning: pilar3 KB write failed for {key}: {exc}")
            continue
        written.append(key)
    return written


def merge(existing: dict[str, Any] | None, fresh: dict[str, Any]) -> dict[str, Any]:
    """New filings replace an entity's older one; an entity absent from this window keeps its last
    filing (a half-yearly filer must not vanish between reports)."""
    records = dict((existing or {}).get("records") or {})
    for eid, rec in (fresh.get("records") or {}).items():
        if (rec.get("date") or "") >= ((records.get(eid) or {}).get("date") or ""):
            records[eid] = rec
    return {"as_of": fresh.get("as_of"), "count": len(records), "records": records}


def run(bucket: str | None = None, *, kb_bucket: str | None = None, lookback_days: int = 200,
        today: dt.date | None = None, s3: Any | None = None, dasfn: bool = True) -> dict[str, Any]:
    """Refresh the tone corpus from CVM IPE (the full Pilar 3 PDF) and from the DASFN qualitative
    tables (#301, the other banks), then (when ``kb_bucket`` is set) promote any new filing's prose
    into the KB data source. The caller starts the ingestion job."""
    from src.ingest import cvm_ipe
    from src.synth.entities import resolve_entities

    # Pilar 3 is quarterly (some banks half-yearly): 200 days covers the latest filing. This used to
    # pass the YEAR as the lookback (2026 days), scanning five years of IPE filings.
    rows = cvm_ipe.fetch_material_facts(lookback_days, today=today)
    fresh = build_corpus(rows, resolver=resolve_entities, today=today)
    n_dasfn = 0
    if dasfn:
        from src.ingest import pilar3_dasfn
        try:
            extra = pilar3_dasfn.build(pilar3_dasfn.list_qualitative(pilar3_dasfn.fetch_catalog()),
                                       resolver=resolve_entities)
        except Exception as exc:  # pragma: no cover - the catalog is best-effort
            print(f"Warning: DASFN Pilar 3 failed: {exc}")
            extra = {}
        n_dasfn = len(extra)
        # The full IPE report wins over the DASFN tables for the same bank (merge keeps the newer).
        fresh = merge({"records": extra}, fresh)
        fresh["count"] = len(fresh["records"])
    idx = merge(load_index(bucket, s3=s3) if bucket else {}, fresh)
    if bucket and fresh.get("count"):
        publish(idx, bucket, s3=s3)
    kb = promote_to_kb(idx, bucket=kb_bucket, s3=s3) if kb_bucket else []
    return {"status": "ok", "filings": len(find_filings(rows)), "dasfn_entities": n_dasfn,
            "entities": idx.get("count"), "kb_written": len(kb), "kb_keys": kb}


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    import json
    import os

    bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    return {"statusCode": 200, "body": json.dumps(run(bucket), ensure_ascii=False)}
