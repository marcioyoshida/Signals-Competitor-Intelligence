"""CVM IPE history for financial institutions (#331, ADR 031 Board M1).

The live reader (``cvm_ipe.py``) keeps a 45-day window of Fato Relevante and Comunicado ao
Mercado. Historic replay (Board M2) needs the years before, and an answer key: what each
bank's board actually decided. Both come from the same CVM dataset, one ZIP per year:

* ``fato_relevante`` and ``comunicado``: the material facts, the triggers of an episode.
* ``ata_rca`` and ``sumario_rca``: board-of-directors minutes (category "Reunião da
  Administração", type "Conselho de Administração", kind "Ata" or "Sumário das Decisões").
  These are the answer key. Probed 2026-11-16: 6,643 Atas and 144 Sumários across all
  companies in 2024. Fiscal council, audit committee and executive-board minutes are
  other types and are left out.

Scope is financial institutions: a filer is kept when its 8-digit CNPJ root is in the
dated entity map (#330, every BCB-supervised root since 2000), or (#388) is an insurer: a
root the registry files under an ``insurance`` entity, or one on SUSEP's company list.
Insurer rows carry ``regulator: "susep"``. A root in the dated insurer map (#389) is
attributed by date like a bank; a holding SUSEP does not supervise (BB Seguridade, Porto
Seguro SA) carries today's registry entity (``attribution: "registry"``). Each record carries the
registry entity that owned the root **on the meeting (reference) date**, through
``entity_at``. HSBC Bank Brasil's 2015 minutes therefore belong to HSBC, not to Bradesco.

Store: ``board_history/cvm_ipe/{year}.json.gz`` in the digests bucket, one immutable file
per year, plus ``manifest.json``. The live feed is unchanged: board minutes are an
evaluation answer key, not a feed signal.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import re
import zipfile
from typing import Any, Callable, Iterable, Mapping

from src.ingest import bcb_entity_history as eh
from src.ingest import cvm_ipe

FIRST_YEAR = 2014
PREFIX = "board_history/cvm_ipe/"
MANIFEST_KEY = f"{PREFIX}manifest.json"
SCHEMA = "onca/cvm-ipe-history/v1"

MATERIAL = {"fato relevante": "fato_relevante", "comunicado ao mercado": "comunicado"}
BOARD_CATEGORY = "reunião da administração"
BOARD_TYPE = "conselho de administração"
BOARD_KINDS = {"ata": "ata_rca", "sumário das decisões": "sumario_rca"}
CLASSES = ("fato_relevante", "comunicado", "ata_rca", "sumario_rca")
ANSWER_KEY = frozenset({"ata_rca", "sumario_rca"})

# A foreign parent's US filings relayed to CVM as "Comunicado ao Mercado", with the SEC form
# as the whole subject. Deutsche Bank SA filed 2,684 of these in 2019 alone (Form 4, 8-K,
# 425), which would drown the Brazilian bank's own events in a replay.
SEC_FORM_SUBJECT = re.compile(
    r"(?:FORM\s*)?(?:3|4|5|144|6-?K|8-?K|10-?[KQ]|11-?K|20-?F|40-?F|425|FWP|SC\s*13[DG](?:/A)?|"
    r"S-\d+(?:ASR)?|F-\d+(?:ASR)?|424B\d|(?:DEF|PRE|DEFA)\s*14A|13F(?:-HR)?)(?:/A)?\.?", re.I)
# The same filer acting as BDR depositary: document dumps and dividend pass-through fee
# notices (1,786 "Diversos Documentos" in 2020, 1,019 "Docs" in 2021). Not the bank's events.
DEPOSITARY_SUBJECT = re.compile(
    r"(?:DIVERSOS\s+)?DOC(?:UMENTO)?S?\.?|.*TAXA\.?\s*(?:DE\s+)?REPASSE.*|ALTTAXAREPASSE.*|"
    r"REDU[CÇ][AÃ]O\s+D[AE]\s+TARIFA.*DISTRIBUI.*", re.I)


# Filers whose Comunicados are BDR depositary work for OTHER issuers (dividend notices,
# programme migrations, issuer changes), never the bank's own events. Probed 2026-11-16:
# every 2018 Comunicado from Deutsche Bank SA - Banco Alemão (477) was depositary paperwork.
# Its board minutes and material facts are still kept.
DEPOSITARY_ROOTS = frozenset({"62331228"})


def is_relayed_sec_form(rec: Mapping[str, Any]) -> bool:
    """A notice that is a foreign parent's SEC filing or a BDR depositary's paperwork."""
    subj = str(rec.get("subject") or "").strip()
    return bool(SEC_FORM_SUBJECT.fullmatch(subj) or DEPOSITARY_SUBJECT.fullmatch(subj))


def doc_class(rec: Mapping[str, Any]) -> str | None:
    """Which of the four kept classes a normalized IPE record is, or None (dropped)."""
    cat = str(rec.get("category") or "").strip().lower()
    if cat in MATERIAL:
        return MATERIAL[cat]
    if cat == BOARD_CATEGORY and str(rec.get("tipo") or "").strip().lower() == BOARD_TYPE:
        return BOARD_KINDS.get(str(rec.get("especie") or "").strip().lower())
    return None


def fi_roots(entity_map: Mapping[str, Any]) -> frozenset[str]:
    """Every CNPJ root the dated entity map knows: the financial-institution filter."""
    return frozenset((entity_map.get("roots") or {}).keys())


def _version(rec: Mapping[str, Any]) -> int:
    try:
        return int(str(rec.get("version") or "0").strip() or 0)
    except ValueError:
        return 0


def insurer_roots(registry_roots: Mapping[str, str], insurance_ids: Iterable[str],
                  susep_roots: Iterable[str] = ()) -> dict[str, str | None]:
    """#388: the SUSEP-side filers the BCB map cannot see, as {root: registry entity or None}.

    A root is kept when the registry files it under an ``insurance`` entity, or when it is on
    SUSEP's supervised-company list (then it carries whatever entity the registry has for it,
    possibly none). There is no dated insurer map yet (#389), so these rows are attributed to
    today's registry entity and say so with ``attribution: "registry"``."""
    ins = set(insurance_ids)
    out: dict[str, str | None] = {r: e for r, e in registry_roots.items() if e in ins}
    for r in susep_roots:
        if r and len(r) == 8:
            out.setdefault(r, registry_roots.get(r))
    return out


def select(records: Iterable[Mapping[str, Any]], entity_map: Mapping[str, Any],
           extra_roots: Mapping[str, str | None] | None = None,
           insurer_map: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Keep the four classes for financial-institution filers, one row per protocol (the
    latest version), each attributed to its owner at the meeting date. Sorted by date.

    ``extra_roots`` (#388) adds insurers: roots outside the BCB map, attributed to their
    registry entity. A root in both keeps the dated BCB attribution. ``insurer_map`` (#389,
    the SUSEP dated map) dates an insurer root it knows, exactly as #330 dates a bank's;
    holdings SUSEP does not supervise stay on the registry entity."""
    roots = fi_roots(entity_map)
    extra = {r: e for r, e in (extra_roots or {}).items() if r not in roots}
    best: dict[str, dict[str, Any]] = {}
    for rec in records:
        cls = doc_class(rec)
        root = (rec.get("cnpj") or "")[:8]
        if not cls or len(root) != 8 or (root not in roots and root not in extra):
            continue
        if cls == "comunicado" and (root in DEPOSITARY_ROOTS or is_relayed_sec_form(rec)):
            continue
        as_of = rec.get("reference_date") or rec.get("date")
        out = {k: rec.get(k) for k in ("id", "protocol", "version", "date", "reference_date",
                                       "company", "cnpj", "category", "tipo", "especie",
                                       "subject", "url", "governance")}
        out.update({"doc_class": cls, "answer_key": cls in ANSWER_KEY, "cnpj_root": root})
        if root in roots:
            out.update({"regulator": "bcb", "attribution": "dated",
                        "entity_id": eh.entity_at(entity_map, root, as_of) if as_of else None,
                        "direct_entity_id": (eh.entity_at(entity_map, root, as_of, level="direct")
                                             if as_of else None)})
        elif insurer_map and root in (insurer_map.get("roots") or {}):
            out.update({"regulator": "susep", "attribution": "dated",
                        "entity_id": eh.entity_at(insurer_map, root, as_of) if as_of else None,
                        "direct_entity_id": (eh.entity_at(insurer_map, root, as_of, level="direct")
                                             if as_of else None)})
        else:
            out.update({"regulator": "susep", "attribution": "registry",
                        "entity_id": extra[root], "direct_entity_id": extra[root]})
        key = str(rec.get("protocol") or rec.get("id"))
        if key not in best or _version(rec) >= _version(best[key]):
            best[key] = out
    return sorted(best.values(), key=lambda r: (r.get("reference_date") or r.get("date") or "",
                                                r.get("date") or "", r.get("id") or ""))


def year_records(content: bytes) -> Iterable[dict[str, Any]]:
    """Rows of one yearly IPE ZIP, normalized as the live reader does plus the two fields the
    history needs (type, to find board minutes; version, to keep the latest per protocol).
    Kept here so the live reader and the deployed Lambdas are unchanged."""
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for name in zf.namelist():
            if not name.lower().endswith(".csv"):
                continue
            with zf.open(name) as fh:
                for raw in csv.DictReader(io.TextIOWrapper(fh, encoding="latin-1", newline=""),
                                          delimiter=";"):
                    rec = cvm_ipe._normalize(raw)
                    rec["tipo"] = (raw.get("Tipo") or "").strip() or None
                    rec["version"] = (raw.get("Versao") or "").strip() or None
                    yield rec


def summarize(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    by = {c: 0 for c in CLASSES}
    for r in rows:
        by[r["doc_class"]] = by.get(r["doc_class"], 0) + 1
    return {"records": len(rows), "by_class": by,
            "filers": len({r["cnpj_root"] for r in rows}),
            "attributed": sum(1 for r in rows if r.get("entity_id")),
            "by_regulator": {g: sum(1 for r in rows if r.get("regulator", "bcb") == g)
                             for g in ("bcb", "susep")}}


# --- store ------------------------------------------------------------------------

def year_key(year: int) -> str:
    return f"{PREFIX}{year}.json.gz"


def put_year(s3: Any, bucket: str, year: int, rows: list[dict[str, Any]], *,
             source_bytes: int) -> dict[str, Any]:
    doc = {"schema": SCHEMA, "year": year, "source": cvm_ipe.ZIP_URL_TMPL.format(year=year),
           "source_bytes": source_bytes, **summarize(rows), "rows": rows}
    s3.put_object(Bucket=bucket, Key=year_key(year), ContentType="application/json",
                  ContentEncoding="gzip",
                  Body=gzip.compress(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()))
    return {k: v for k, v in doc.items() if k != "rows"}


def get_year(s3: Any, bucket: str, year: int) -> dict[str, Any] | None:
    try:
        raw = s3.get_object(Bucket=bucket, Key=year_key(year))["Body"].read()
    except Exception as exc:  # NoSuchKey or a transient read error: the caller refetches
        if "NoSuchKey" in type(exc).__name__ or "NoSuchKey" in str(exc):
            return None
        raise
    return json.loads(gzip.decompress(raw))


def put_manifest(s3: Any, bucket: str, years: Mapping[int, Mapping[str, Any]], built_at: str) -> None:
    doc = {"schema": SCHEMA, "built_at": built_at, "first_year": min(years), "last_year": max(years),
           "years": {str(y): dict(v) for y, v in sorted(years.items())}}
    s3.put_object(Bucket=bucket, Key=MANIFEST_KEY, ContentType="application/json",
                  Body=json.dumps(doc, ensure_ascii=False, indent=1).encode())


def events_for(rows: Iterable[Mapping[str, Any]], entity_id: str, start: str, end: str,
               classes: Iterable[str] = CLASSES) -> list[Mapping[str, Any]]:
    """Replay helper (M2): one entity's filings between two ISO dates (inclusive), by the
    owner at the time. ``classes`` picks triggers (material facts) or the answer key."""
    want = set(classes)
    return [r for r in rows if r.get("entity_id") == entity_id and r["doc_class"] in want
            and start <= (r.get("reference_date") or r.get("date") or "") <= end]

