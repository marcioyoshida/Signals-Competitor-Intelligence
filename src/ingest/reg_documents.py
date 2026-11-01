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

#294: a DOU page is the act AS PUBLISHED, so it never changes and never yields a second
version. For BCB/CMN instruments the source is now BCB's content API, which serves the
CONSOLIDATED text (amendments folded in, `VersaoNormativo` bumped) plus `Atualizacoes`, the
regulator's own list of amending acts and the articles each changed. DOU stays the fallback
for the other regulators. Versions diff only within one source.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html as _html
import json
import re
import time
from typing import Any, Callable
from urllib.parse import urlencode

import boto3
import requests

REGDOCS_PREFIX = "regdocs/"
REGDOCS_INDEX_KEY = "regdocs/index.json"

_TAG_RX = re.compile(r"<[^>]+>")
# DOU wraps the norm body in <div class="texto-dou"> with <p class="dou-paragraph"> lines.
_PARA_RX = re.compile(r'<p[^>]*class="[^"]*dou-paragraph[^"]*"[^>]*>(.*?)</p>', re.I | re.S)
_SCRIPT_RX = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_BLOCK_RX = re.compile(r"</(?:p|div|li|h\d|tr)\s*>|<br\s*/?>", re.I)

_UA = {"User-Agent": "Onca-CI/1.0 (competitive-intelligence)"}   # as src/ingest/dou.py
BCB_API = "https://www.bcb.gov.br/api/conteudo/app/normativos/exibenormativo"
#: instrument-key prefix (src/synth/regulatory.py) -> the API's `p1`. Each probed live 2026-11-01.
BCB_TIPOS = {
    "res-cmn": "Resolução CMN",
    "res-bcb": "Resolução BCB",
    "in-bcb": "Instrução Normativa BCB",
    "circ": "Circular",
}


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
    r = requests.get(url, headers=_UA, timeout=timeout)
    r.raise_for_status()
    return extract_text(r.text)


def bcb_target(instrument_key: str) -> tuple[str, str] | None:
    """(p1 tipo, number) for an instrument BCB's API serves, else None."""
    prefix, _, num = str(instrument_key or "").rpartition("-")
    tipo = BCB_TIPOS.get(prefix)
    return (tipo, num) if tipo and num.isdigit() else None


def html_to_text(fragment: str) -> str:
    """Readable text from BCB's `Texto` HTML, one block per line. reg_diff segments by an
    "Art. N" at the start of a line, so the block breaks matter."""
    t = _BLOCK_RX.sub("\n", _SCRIPT_RX.sub(" ", fragment or ""))
    lines = (re.sub(r"\s+", " ", _html.unescape(_TAG_RX.sub("", ln))).replace("\u200b", "").strip()
             for ln in t.split("\n"))
    return "\n".join(ln for ln in lines if ln)


_EFFECTIVE_RX = re.compile(r"a partir de (\d{1,2})º?/(\d{1,2})/(\d{4})")


def parse_atualizacoes(raw: Any) -> list[dict[str, Any]]:
    """BCB's `Atualizacoes`: ";#"-separated "<ato> - [Alteração, a partir de d/m/a - ]<what>".
    Returns [{act, detail, effective}] in the regulator's (chronological) order."""
    out: list[dict[str, Any]] = []
    for entry in str(raw or "").split(";#"):
        entry = entry.strip()
        if not entry or " - " not in entry:
            continue
        act, rest = (x.strip() for x in entry.split(" - ", 1))
        effective = None
        m = _EFFECTIVE_RX.search(rest)
        if rest.lower().startswith("alteração") and " - " in rest:
            if m:
                effective = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
            rest = rest.split(" - ", 1)[1].strip()
        out.append({"act": act, "detail": rest.rstrip(" .")[:400], "effective": effective})
    return out


def bcb_page_url(tipo: str, numero: str) -> str:
    """The regulator's human page for the norm (the card's checking link)."""
    return ("https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?"
            + urlencode({"tipo": tipo, "numero": numero}))


def fetch_bcb(tipo: str, numero: str, *, timeout: int = 20) -> dict[str, Any]:
    """BCB's consolidated text of one norm: {text, versao, amendments, revoked, url}."""
    r = requests.get(BCB_API, params={"p1": tipo, "p2": numero}, headers=_UA, timeout=timeout)
    r.raise_for_status()
    body = r.json()
    c = body.get("conteudo") if isinstance(body, dict) else None
    c = c[0] if isinstance(c, list) and c else c
    if not isinstance(c, dict) or not c.get("Texto"):
        return {"text": "", "versao": None, "amendments": [], "revoked": False,
                "url": bcb_page_url(tipo, numero)}
    return {
        "text": html_to_text(c.get("Texto")),
        "versao": str(c.get("VersaoNormativo") or "") or None,
        "amendments": parse_atualizacoes(c.get("Atualizacoes")),
        "revoked": bool(c.get("Revogado") or c.get("Cancelado")),
        "url": bcb_page_url(tipo, numero),
    }


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
    source: str = "dou", versao: str | None = None,
) -> dict[str, Any]:
    """Persist one version of an instrument's text, content-hash-cached. A re-fetch whose
    hash already exists for the instrument is a NO-OP (returns stored=False).

    `source` is "dou" (the act as published) or "bcb" (the consolidated text). A diff is
    computed only against the prior version FROM THE SAME SOURCE: the two texts differ in
    layout and in what they contain, so a cross-source diff would be noise (#294)."""
    s3 = s3 or boto3.client("s3")
    h = content_hash(text)
    rec = index.setdefault(instrument_key, {"label": label, "versions": []})
    if label and not rec.get("label"):
        rec["label"] = label
    if any(v.get("hash") == h for v in rec["versions"]):
        return {"stored": False, "hash": h, "reason": "unchanged"}
    same = [v for v in rec["versions"] if (v.get("source") or "dou") == source]
    prior = same[-1] if same else None
    key = f"{REGDOCS_PREFIX}{instrument_key}/{h}.txt"
    s3.put_object(Bucket=bucket, Key=key, Body=str(text).encode("utf-8"),
                  ContentType="text/plain; charset=utf-8")
    entry: dict[str, Any] = {
        "hash": h, "key": key, "url": url, "chars": len(text or ""),
        "fetched": as_of or dt.date.today().isoformat(), "source": source,
    }
    if versao:
        entry["versao"] = versao
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
    fetch_bcb_fn: Callable[[str, str], dict[str, Any]] = fetch_bcb,
    min_chars: int = 200, max_docs: int = 60, as_of: str | None = None,
    budget_s: float = 120.0, pause_s: float = 0.5,
    clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch + store the full text of each target instrument (content-hash-cached).

    targets: [{instrument_key, url, label}]. An instrument BCB's API serves (`bcb_target`) is
    fetched there (consolidated text); the rest from `url` (DOU). Bounded by max_docs and by
    budget_s of wall time, so it never runs the regulatory Lambda out of time; whatever is
    left waits for the next run. The hash cache makes steady-state runs cheap.

    Every BCB fetch also refreshes the instrument's `bcb` block in the index (version,
    amendments, revoked), even when the text is unchanged: that is what the card shows."""
    s3 = s3 or boto3.client("s3")
    index = load_index(bucket, s3=s3)
    report: dict[str, Any] = {"targets": len(targets), "stored": [], "unchanged": 0,
                              "skipped": 0, "deferred": 0, "errors": []}
    start = clock()
    first = True
    for t in targets[:max_docs]:
        key, url = t.get("instrument_key"), t.get("url")
        bcb = bcb_target(key or "")
        if not key or not (url or bcb):
            report["skipped"] += 1
            continue
        if clock() - start > budget_s:
            report["deferred"] += 1
            continue
        if not first and pause_s:
            sleep(pause_s)
        first = False
        try:
            source, versao, page = "dou", None, url
            if bcb:
                doc = fetch_bcb_fn(*bcb)
                text, source, versao, page = doc.get("text") or "", "bcb", doc.get("versao"), doc.get("url")
                if text:
                    index.setdefault(key, {"label": t.get("label"), "versions": []})["bcb"] = {
                        "versao": versao, "amendments": doc.get("amendments") or [],
                        "revoked": bool(doc.get("revoked")), "url": page,
                        "fetched": as_of or dt.date.today().isoformat()}
                elif url:   # the API doesn't know it: fall back to the DOU page
                    source, versao, page = "dou", None, url
                    text = fetch(url)
            else:
                text = fetch(url)
            if not text or len(text) < min_chars:
                report["skipped"] += 1
                continue
            res = store_document(bucket, key, text, url=page, label=t.get("label"),
                                 index=index, s3=s3, as_of=as_of, source=source, versao=versao)
            if res["stored"]:
                report["stored"].append(key)
            else:
                report["unchanged"] += 1
        except Exception as exc:  # pragma: no cover - network best-effort
            report["errors"].append({"key": key, "error": str(exc)[:200]})
    publish_index(index, bucket, s3=s3)
    report["index"] = index
    return report
