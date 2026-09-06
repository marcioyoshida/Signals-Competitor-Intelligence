"""ADR 022 Phase 6 — Pilar 3 (Relatório de Gerenciamento de Riscos) risk-report text.

The real qualitative risk-posture corpus behind the numbers. Discovered via the **CVM IPE** feed
(`cvm_ipe`, already ingested) — listed institutions file their Pilar 3 as a *Comunicado ao Mercado*
whose `Assunto` carries both "Gerenciamento de Riscos" and "Pilar", with a direct `Link_Download`
(rad.cvm.gov.br). We download the PDF (the ENET endpoint mislabels it `text/html` but the body is a
real `%PDF`), extract the qualitative risk-narrative sentences, resolve to a tracked entity, and
persist a durable `pilar3/index.json` per-entity corpus.

This corpus is what ADR 022 Phase 5 (`financial_tone`) reads INSTEAD of the fact-paraphrases once
available — so a competitor's financial tone comes from its *own risk report's language*, not a
circular restatement of its Basileia. (Feeding the full text into the Bedrock KB for grounded Q&A is
the other half of Phase 6 — a separate ingestion job.)

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


def fetch_pdf_text(url: str, *, max_pages: int = 20, timeout: int = 120) -> str:
    """Download a Pilar 3 PDF (ENET mislabels content-type) and extract the leading pages' text."""
    from pypdf import PdfReader

    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    if b"%PDF" not in resp.content[:2000]:
        raise requests.RequestException("not a PDF payload")
    reader = PdfReader(io.BytesIO(resp.content))
    n = min(max_pages, len(reader.pages))
    return "\n".join((reader.pages[i].extract_text() or "") for i in range(n))


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
                records[eid] = {"entity": eid, "sentences": sents,
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
    s3.put_object(Bucket=bucket, Key=INDEX_KEY,
                  Body=json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8"),
                  ContentType="application/json")
    return f"s3://{bucket}/{INDEX_KEY}"


def run(bucket: str | None = None, *, year: int | None = None, today: dt.date | None = None) -> dict[str, Any]:
    from src.ingest import cvm_ipe
    from src.synth.entities import resolve_entities

    year = year or (today or dt.date.today()).year
    rows = cvm_ipe.fetch_material_facts(year)
    idx = build_corpus(rows, resolver=resolve_entities, today=today)
    if bucket and idx.get("count"):
        publish(idx, bucket, s3=None)
    return {"status": "ok", "filings": len(find_filings(rows)), "entities": idx.get("count")}


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    import json
    import os

    bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    return {"statusCode": 200, "body": json.dumps(run(bucket), ensure_ascii=False)}
