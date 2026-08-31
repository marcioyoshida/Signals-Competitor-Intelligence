"""Ingest CEIS/CNEP federal sanctions — counterparty-integrity signal (issue #60).

Two federal debarment registries, published tokenless as daily bulk downloads by the
Portal da Transparência:

  - **CEIS** — Cadastro de Empresas Inidôneas e Suspensas (barred/suspended from
    contracting with the public administration),
  - **CNEP** — Cadastro Nacional de Empresas Punidas (Lei Anticorrupção sanctions).

A sanctioned company appearing here is a hard integrity/counterparty-risk fact, and —
unlike the FS-only sources — it is **sector-agnostic**, so it is the cross-industry
integrity layer for the sectorial product.

Source (verified 2026-08-31):
  URL:  https://portaldatransparencia.gov.br/download-de-dados/{ceis,cnep}/YYYYMMDD
  body: ZIP of a single ``;``-delimited, latin-1, quoted CSV. Today/yesterday 403
        until published, so we walk back to the latest available day.

Resolution is **CNPJ-root only** (exact) — deliberately. A sanction is a legal fact
about a specific legal person, and fuzzy name-matching against 16k arbitrary company
razões sociais over-resolves on common tokens ("ASSOCIAÇÃO", "BRASIL") and would
mis-attribute a debarment to the wrong company — a defamation/LGPD risk this project
guards against. So a sanction resolves only when its CNPJ root matches a tracked
entity's ``cnpj_roots``; otherwise it is dropped. Only ``TIPO DE PESSOA == 'J'`` rows
carry a CNPJ; pessoa física sanctions are out of scope (no tracked entity).

Best-effort: any HTTP/parse issue degrades to ``[]``. ``downloader`` is injected in tests.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import zipfile
from typing import Any, Callable, Iterable

DOWNLOAD_URL = "https://portaldatransparencia.gov.br/download-de-dados/{kind}/{date}"
KINDS = ("ceis", "cnep")
INDEX_KEY = "sanctions/index.json"
PUBLIC_URL = "https://portaldatransparencia.gov.br/sancoes/{kind}"
_DIGITS = re.compile(r"\D+")


def _cnpj_root(raw: Any) -> str | None:
    """First 8 digits (raiz) of a 14-digit CNPJ; None for a CPF/short/blank value."""
    digits = _DIGITS.sub("", str(raw or ""))
    return digits[:8] if len(digits) >= 14 else None


def _iso_date(raw: Any) -> str | None:
    """DD/MM/YYYY -> YYYY-MM-DD; passthrough for already-ISO; None otherwise."""
    s = str(raw or "").strip()
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", s)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{mo}-{d}"
    return s[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", s) else None


def _default_downloader(kind: str, *, days_back: int = 12,
                        today: dt.date | None = None) -> tuple[str, str] | None:
    """Fetch the latest published {kind} bulk CSV. Returns (date_iso, csv_text)."""
    import requests

    today = today or dt.date.today()
    for delta in range(1, days_back + 1):
        day = today - dt.timedelta(days=delta)
        url = DOWNLOAD_URL.format(kind=kind, date=day.strftime("%Y%m%d"))
        try:
            resp = requests.get(
                url, timeout=60, headers={"User-Agent": "Onca-CI/1.0 (competitive-intelligence)"},
            )
        except requests.RequestException as exc:  # pragma: no cover - network best-effort
            print(f"Warning: {kind} download {day} failed: {exc}")
            continue
        if resp.status_code != 200 or not resp.content:
            continue
        try:
            zf = zipfile.ZipFile(io.BytesIO(resp.content))
            member = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
            if not member:
                continue
            text = zf.read(member).decode("latin-1")
        except (zipfile.BadZipFile, KeyError) as exc:  # pragma: no cover - defensive
            print(f"Warning: {kind} unzip {day} failed: {exc}")
            continue
        return day.isoformat(), text
    return None


def _parse_csv(kind: str, text: str, source_date: str) -> list[dict[str, Any]]:
    """Normalise a bulk CSV into per-sanction rows (pessoa jurídica only)."""
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    out: list[dict[str, Any]] = []
    for r in reader:
        if str(r.get("TIPO DE PESSOA") or "").strip().upper() != "J":
            continue
        root = _cnpj_root(r.get("CPF OU CNPJ DO SANCIONADO"))
        if not root:
            continue
        code = str(r.get("CÓDIGO DA SANÇÃO") or "").strip()
        name = (str(r.get("RAZÃO SOCIAL - CADASTRO RECEITA") or "").strip()
                or str(r.get("NOME DO SANCIONADO") or "").strip())
        out.append({
            "id": f"{kind}:{code}" if code else f"{kind}:{root}:{_iso_date(r.get('DATA INÍCIO SANÇÃO')) or source_date}",
            "source": kind.upper(),
            "kind": "sanction",
            "cadastro": kind.upper(),
            "cnpj_root": root,
            "company": name,
            "category": str(r.get("CATEGORIA DA SANÇÃO") or "").strip() or None,
            "orgao": str(r.get("ÓRGÃO SANCIONADOR") or "").strip() or None,
            "start": _iso_date(r.get("DATA INÍCIO SANÇÃO")),
            "end": _iso_date(r.get("DATA FINAL SANÇÃO")),
            "fundamentacao": (str(r.get("FUNDAMENTAÇÃO LEGAL") or "").strip() or None),
            "url": PUBLIC_URL.format(kind=kind),
            "date": source_date,
        })
    return out


def fetch_sanctions(
    kinds: Iterable[str] = KINDS,
    *,
    downloader: Callable[[str], tuple[str, str] | None] | None = None,
) -> list[dict[str, Any]]:
    """Latest published sanctions across the requested registries (pessoa jurídica)."""
    dl = downloader or _default_downloader
    rows: list[dict[str, Any]] = []
    for kind in kinds:
        try:
            got = dl(kind)
        except Exception as exc:  # pragma: no cover - per-registry best-effort
            print(f"Warning: {kind} fetch failed: {exc}")
            continue
        if not got:
            continue
        source_date, text = got
        rows.extend(_parse_csv(kind, text, source_date))
    return rows


def build_cnpj_index(entities: Iterable[dict[str, Any]]) -> dict[str, str]:
    """root(8) -> entity_id, from the registry. Enables exact CNPJ resolution."""
    idx: dict[str, str] = {}
    for e in entities or []:
        eid = e.get("entity_id")
        if not eid:
            continue
        for r in e.get("cnpj_roots") or []:
            root = str(r)[:8]
            if root:
                idx.setdefault(root, eid)
    return idx


def map_to_entities(
    rows: Iterable[dict[str, Any]],
    *,
    cnpj_index: dict[str, str] | None = None,
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Keep only sanctions whose CNPJ root matches a tracked entity, as signal records.

    CNPJ-root only, by design (see the module docstring): name matching would
    mis-attribute. Each record stamps ``_entities`` (CNPJ-anchored) so synth clustering
    binds the card to that exact entity and bypasses the free-text alias matcher (same
    as FIAGRO moves).
    """
    today = today or dt.date.today()
    cnpj_index = cnpj_index or {}
    out: list[dict[str, Any]] = []
    for row in rows or []:
        eid = cnpj_index.get(row.get("cnpj_root") or "")
        if not eid:
            continue
        title = f"{row['cadastro']}: {row.get('category') or 'sanção'} — {row.get('orgao') or ''}".strip(" —")
        out.append({
            **row,
            "id": f"sanctions:{eid}:{row['id']}",
            "entity": eid,
            "_entities": [eid],
            "title": title,
            "text": " | ".join(x for x in (
                row.get("company"), row.get("category"), row.get("orgao"),
                f"vigência {row.get('start') or '?'}→{row.get('end') or '?'}",
                row.get("fundamentacao")) if x),
            "mapped_at": today.isoformat(),
        })
    return out


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_entity: dict[str, int] = {}
    for r in records:
        by_entity[r["entity"]] = by_entity.get(r["entity"], 0) + 1
    return {
        "kind": "federal_sanctions",
        "source": "PortalTransparencia",
        "total": len(records),
        "entities": len(by_entity),
        "by_cadastro": {k: sum(1 for r in records if r.get("cadastro") == k)
                        for k in ("CEIS", "CNEP")},
        "top": sorted(({"entity": e, "count": c} for e, c in by_entity.items()),
                      key=lambda x: -x["count"])[:5],
    }


# --- durable store --------------------------------------------------------

def merge(existing: dict[str, Any] | None, records: list[dict[str, Any]], *,
          today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.date.today()
    store: dict[str, dict[str, Any]] = dict((existing or {}).get("records") or {})
    for r in records:
        store[r["id"]] = r
    return {"as_of": today.isoformat(), "count": len(store), "records": store}


def load_index(bucket: str, *, s3: Any | None = None) -> dict[str, Any]:
    import boto3
    s3 = s3 or boto3.client("s3")
    try:
        body = s3.get_object(Bucket=bucket, Key=INDEX_KEY)["Body"].read()
        data = json.loads(body)
        return data if isinstance(data, dict) else {}
    except Exception:  # pragma: no cover - first run
        return {}


def publish(index: dict[str, Any], bucket: str, *, s3: Any | None = None) -> str:
    import boto3
    s3 = s3 or boto3.client("s3")
    s3.put_object(
        Bucket=bucket, Key=INDEX_KEY,
        Body=json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{bucket}/{INDEX_KEY}"


def list_records(index: dict[str, Any]) -> list[dict[str, Any]]:
    recs = list((index.get("records") or {}).values())
    recs.sort(key=lambda r: str(r.get("start") or ""), reverse=True)
    return recs


def update_store(records: list[dict[str, Any]], bucket: str, *,
                 s3: Any | None = None, today: dt.date | None = None) -> dict[str, Any]:
    index = load_index(bucket, s3=s3)
    merged = merge(index, records, today=today)
    publish(merged, bucket, s3=s3)
    return {"updated": len(records), "records": merged.get("count", 0)}


def run(bucket: str | None = None, *, today: dt.date | None = None) -> dict[str, Any]:
    """Fetch → map → persist. Standalone/scheduled entrypoint."""
    from src.synth.entity_registry import list_entities
    rows = fetch_sanctions()
    idx = build_cnpj_index(list_entities())
    recs = map_to_entities(rows, cnpj_index=idx, today=today)
    if bucket and recs:
        update_store(recs, bucket, today=today)
    return {"rows": len(rows), "mapped": len(recs), **summarize(recs)}


if __name__ == "__main__":
    import os
    print(json.dumps(run(os.environ.get("ONCA_DIGESTS_BUCKET")), ensure_ascii=False))
