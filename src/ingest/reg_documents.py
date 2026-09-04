"""ADR 009 Phase B — versioned regulatory-document store (`regdocs/`), the diff enabler.

Phase A (`reg_change.py`) enumerates what an amending act SAYS it does, from the act's own
words. To determine the actual DELTA of a versioned document (§2 of the ADR) we first need
the full text of each version stored — that is this module: fetch a tracked instrument's
full text, persist it keyed by instrument + content-hash, and index the versions so a
re-fetch with an unchanged hash is a no-op (cost control). Nothing here diffs yet; it is
the durable enabler the section-diff + LLM change-record phases build on.

Full-text source: the **in.gov.br DOU** page for the act (server-rendered, whole text) —
the BCB `exibenormativo` page is a JS shell and its search API returns only metadata. The
instrument→URL map is derived from the reg-lifecycle threads' own citations (prefer DOU).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html as _html
import json
import re
from typing import Any, Callable

import boto3
import requests

REGDOCS_PREFIX = "regdocs/"
REGDOCS_INDEX_KEY = "regdocs/index.json"

_TAG_RX = re.compile(r"<[^>]+>")
# DOU wraps the norm body in <div class="texto-dou"> with <p class="dou-paragraph"> lines.
_PARA_RX = re.compile(r'<p[^>]*class="[^"]*dou-paragraph[^"]*"[^>]*>(.*?)</p>', re.I | re.S)
_SCRIPT_RX = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)


def _clean(fragment: str) -> str:
    return re.sub(r"[ \t ]+", " ", _html.unescape(_TAG_RX.sub("", fragment or ""))).strip()


def extract_text(html: str) -> str:
    """Readable norm text from a DOU page — the dou-paragraph body, or a tag-stripped
    fallback for a non-DOU page."""
    paras = _PARA_RX.findall(html or "")
    if paras:
        return "\n".join(p for p in (_clean(x) for x in paras) if p).strip()
    stripped = _SCRIPT_RX.sub(" ", html or "")
    text = _clean(stripped)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch_text(url: str, *, timeout: int = 30) -> str:
    """Fetch a document URL and extract its readable text (best full-text = in.gov.br DOU)."""
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    r.raise_for_status()
    return extract_text(r.text)


def content_hash(text: str) -> str:
    """Whitespace/case-insensitive content hash — a version key that ignores reformatting."""
    norm = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def load_index(bucket: str, *, s3: Any | None = None) -> dict[str, Any]:
    s3 = s3 or boto3.client("s3")
    try:
        body = s3.get_object(Bucket=bucket, Key=REGDOCS_INDEX_KEY)["Body"].read()
        idx = json.loads(body.decode("utf-8"))
        return idx.get("instruments", idx) if isinstance(idx, dict) else {}
    except Exception:
        return {}


def publish_index(index: dict[str, Any], bucket: str, *, s3: Any | None = None) -> str:
    s3 = s3 or boto3.client("s3")
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "instruments": index,
    }
    s3.put_object(
        Bucket=bucket, Key=REGDOCS_INDEX_KEY,
        Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json", CacheControl="no-cache",
    )
    return f"s3://{bucket}/{REGDOCS_INDEX_KEY}"


def store_document(
    bucket: str, instrument_key: str, text: str, *,
    url: str | None = None, label: str | None = None,
    index: dict[str, Any], s3: Any | None = None, as_of: str | None = None,
) -> dict[str, Any]:
    """Persist one version of an instrument's text, content-hash-cached. A re-fetch whose
    hash already exists for the instrument is a NO-OP (returns stored=False)."""
    s3 = s3 or boto3.client("s3")
    h = content_hash(text)
    rec = index.setdefault(instrument_key, {"label": label, "versions": []})
    if label and not rec.get("label"):
        rec["label"] = label
    if any(v.get("hash") == h for v in rec["versions"]):
        return {"stored": False, "hash": h, "reason": "unchanged"}
    prior = rec["versions"][-1] if rec["versions"] else None
    key = f"{REGDOCS_PREFIX}{instrument_key}/{h}.txt"
    s3.put_object(Bucket=bucket, Key=key, Body=str(text).encode("utf-8"),
                  ContentType="text/plain; charset=utf-8")
    entry: dict[str, Any] = {
        "hash": h, "key": key, "url": url, "chars": len(text or ""),
        "fetched": as_of or dt.date.today().isoformat(),
    }
    result = {"stored": True, "hash": h, "key": key}
    # ADR 009 §2: a NEW version over a prior one → compute + persist the section diff.
    if prior and prior.get("key"):
        try:
            from src.synth import reg_diff

            old_text = _load_doc(bucket, prior["key"], s3=s3)
            diff = reg_diff.diff_versions(old_text, text)
            diff_key = f"{REGDOCS_PREFIX}{instrument_key}/{prior['hash']}__{h}.diff.json"
            s3.put_object(Bucket=bucket, Key=diff_key,
                          Body=json.dumps(diff, ensure_ascii=False).encode("utf-8"),
                          ContentType="application/json")
            entry["diff"] = {"vs": prior["hash"], "key": diff_key, "summary": reg_diff.summarize_diff(diff),
                             **diff["summary"]}
            result["diff"] = entry["diff"]
        except Exception as exc:  # pragma: no cover - diff is best-effort, never blocks store
            print(f"Warning: regdoc diff {instrument_key} failed: {exc}")
    rec["versions"].append(entry)
    return result


def _load_doc(bucket: str, key: str, *, s3: Any | None = None) -> str:
    s3 = s3 or boto3.client("s3")
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8", "replace")


def sync_documents(
    targets: list[dict[str, Any]], bucket: str, *,
    s3: Any | None = None, fetch: Callable[[str], str] = fetch_text,
    min_chars: int = 200, max_docs: int = 60, as_of: str | None = None,
) -> dict[str, Any]:
    """Fetch + store the full text of each target instrument (content-hash-cached).

    targets: [{instrument_key, url, label}]. Bounded by max_docs; the hash cache makes
    steady-state runs cheap (only changed/new instruments write)."""
    s3 = s3 or boto3.client("s3")
    index = load_index(bucket, s3=s3)
    report: dict[str, Any] = {"targets": len(targets), "stored": [], "unchanged": 0,
                              "skipped": 0, "errors": []}
    for t in targets[:max_docs]:
        key, url = t.get("instrument_key"), t.get("url")
        if not key or not url:
            report["skipped"] += 1
            continue
        try:
            text = fetch(url)
            if not text or len(text) < min_chars:
                report["skipped"] += 1
                continue
            res = store_document(bucket, key, text, url=url, label=t.get("label"),
                                 index=index, s3=s3, as_of=as_of)
            if res["stored"]:
                report["stored"].append(key)
            else:
                report["unchanged"] += 1
        except Exception as exc:  # pragma: no cover - network best-effort
            report["errors"].append({"key": key, "error": str(exc)})
    publish_index(index, bucket, s3=s3)
    return report
