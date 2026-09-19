"""Ingest the ANS Índice Geral de Reclamações (IGR) — consumer reputation for saúde
suplementar (issue #140).

Extends the reputation signal beyond BCB-supervised banks. ``bcb_reclamacoes`` (#31) is
the pattern this mirrors: a per-institution complaints *ranking* folded into the same
reputation store, so the CCO panel and ``signal_state`` pick it up with no dashboard
change. #63 (consumidor.gov.br) was the original plan for cross-industry coverage; its
source host is dead, and ANS is live, monthly, and lands in a vertical already tracked.

**Resolution is by CNPJ, never by name.** The IGR file identifies an operadora only by
``REGISTRO_OPERADORA`` + ``RAZAO_SOCIAL``. Resolving on the razão social was measured
against the live file and reproduces #135's failure mode on real rows — "CAIXA DE
ASSISTÊNCIA DOS FUNCIONÁRIOS DO BANCO…" (CASSI, a Banco do Brasil fund) and "POSTAL
SAÚDE CAIXA DE ASSISTÊNCIA…" both match *caixa*; "SUL AMÉRICA PARANÁ CLÍNICAS…" matches
*parana*. Attributing complaint volumes to the wrong financial institution is the class
of mis-attribution #60 refused to risk for sanctions, so this module resolves ONLY
through the ANS ``Relatorio_cadop.csv`` bridge (``REGISTRO_OPERADORA`` → CNPJ) and drops
anything that doesn't resolve. An unresolved operadora is silently skipped, never guessed.

NB the CNPJ that resolves is the health-plan subsidiary's (e.g. "PORTO SEGURO - SEGURO
SAÚDE S/A"), not the listed holding's. Those subsidiaries have to exist in the registry as
ADR-017 sub-entities (``parent``) for the join to land; that registry work is tracked in
#140 and is deliberately NOT done here — this module only reports what resolves today.

Source (verified live 2026-09-19):
  IGR    https://dadosabertos.ans.gov.br/FTP/PDA/IGR/IGR_versao_2023/pda-023-igr.csv
  cadop  https://dadosabertos.ans.gov.br/FTP/PDA/operadoras_de_plano_de_saude_ativas/Relatorio_cadop.csv
Both latin-1, ``;``-delimited, decimal comma. No token, no robots restriction.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
from typing import Any, Callable, Iterable

IGR_URL = (
    "https://dadosabertos.ans.gov.br/FTP/PDA/IGR/IGR_versao_2023/pda-023-igr.csv"
)
CADOP_URL = (
    "https://dadosabertos.ans.gov.br/FTP/PDA/"
    "operadoras_de_plano_de_saude_ativas/Relatorio_cadop.csv"
)
INDEX_KEY = "ans_igr/index.json"
PUBLIC_URL = "https://www.gov.br/ans/pt-br/acesso-a-informacao/perfil-do-setor/dados-e-indicadores-do-setor"

# The IGR file carries every competência since 2023; only the newest is a current signal.
_ENCODING = "latin-1"


def _download(url: str) -> bytes | None:  # pragma: no cover - network
    import requests

    resp = requests.get(url, timeout=180, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.content


def _rows(blob: bytes) -> list[dict[str, Any]]:
    text = blob.decode(_ENCODING, errors="replace")
    return list(csv.DictReader(io.StringIO(text), delimiter=";"))


def _to_float(v: Any) -> float | None:
    """Parse an ANS-formatted number ("83,07" -> 83.07). Blank/invalid -> None."""
    s = str(v or "").strip().replace(".", "").replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(v: Any) -> int | None:
    s = "".join(ch for ch in str(v or "") if ch.isdigit())
    return int(s) if s else None


def parse_cadop(blob: bytes) -> dict[str, dict[str, Any]]:
    """``{REGISTRO_OPERADORA: {cnpj, cnpj_root, razao_social, nome_fantasia, modalidade}}``.

    This is the structured identity bridge — the only sanctioned way to attribute an IGR
    row to a tracked entity.
    """
    out: dict[str, dict[str, Any]] = {}
    for r in _rows(blob):
        reg = str(r.get("REGISTRO_OPERADORA") or "").strip().strip('"')
        cnpj = "".join(ch for ch in str(r.get("CNPJ") or "") if ch.isdigit())
        if not reg or len(cnpj) < 8:
            continue
        out[reg] = {
            "cnpj": cnpj,
            "cnpj_root": cnpj[:8],
            "razao_social": (r.get("RAZAO_SOCIAL") or "").strip(),
            "nome_fantasia": (r.get("NOME_FANTASIA") or "").strip(),
            "modalidade": (r.get("MODALIDADE") or "").strip(),
        }
    return out


def latest_competencia(rows: Iterable[dict[str, Any]]) -> str | None:
    comps = {str(r.get("COMPETENCIA") or "").strip() for r in rows}
    comps.discard("")
    return max(comps) if comps else None


def parse_igr(blob: bytes, *, competencia: str | None = None) -> list[dict[str, Any]]:
    """IGR rows for ``competencia`` (default: the newest present in the file).

    An operadora appears once per ``COBERTURA`` (médica / exclusivamente odontológica),
    so rows are NOT unique per operadora — ``map_to_entities`` folds them.
    """
    rows = _rows(blob)
    comp = competencia or latest_competencia(rows)
    if not comp:
        return []
    out = []
    for r in rows:
        if str(r.get("COMPETENCIA") or "").strip() != comp:
            continue
        reg = str(r.get("REGISTRO_OPERADORA") or "").strip().strip('"')
        if not reg:
            continue
        out.append({
            "registro": reg,
            "razao_social": (r.get("RAZAO_SOCIAL") or "").strip().strip('"'),
            "cobertura": (r.get("COBERTURA") or "").strip().strip('"'),
            "igr": _to_float(r.get("IGR")),
            "complaints": _to_int(r.get("QTD_RECLAMACOES")),
            "beneficiaries": _to_int(r.get("QTD_BENEFICIARIOS")),
            "porte": (r.get("PORTE_OPERADORA") or "").strip().strip('"'),
            "competencia": comp,
        })
    return out


def map_to_entities(
    rows: Iterable[dict[str, Any]],
    cadop: dict[str, dict[str, Any]],
    *,
    resolver: Callable[[str], str | None],
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """IGR rows → one store record per resolved entity.

    ``resolver`` takes a CNPJ root and returns an entity_id (``entity_registry
    .resolve_by_cnpj``). Rows whose registro isn't in ``cadop``, or whose CNPJ root
    doesn't resolve, are dropped — see the module docstring on why there is no name
    fallback.

    An entity with both a médica and an odontológica line is folded into ONE record:
    complaints and beneficiaries sum, and the headline ``igr`` is taken from the line
    with the most complaints (the dominant book), since IGR is a rate and averaging two
    rates over different denominators would be wrong.
    """
    today = today or dt.date.today()
    by_entity: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        info = cadop.get(row.get("registro") or "")
        if not info:
            continue
        try:
            eid = resolver(info["cnpj_root"])
        except Exception:  # pragma: no cover - resolver best-effort
            eid = None
        if not eid:
            continue
        cur = by_entity.get(eid)
        if cur is None:
            by_entity[eid] = {
                "id": f"ans-igr:{eid}",
                "entity": eid,
                "source": "ANS",
                "company": info.get("razao_social") or row.get("razao_social"),
                "registro_ans": row.get("registro"),
                "modalidade": info.get("modalidade"),
                "index": row.get("igr"),
                "complaints": row.get("complaints") or 0,
                "beneficiaries": row.get("beneficiaries") or 0,
                "cobertura": row.get("cobertura"),
                "period": row.get("competencia"),
                "url": PUBLIC_URL,
                "date": today.isoformat(),
                "_top_complaints": row.get("complaints") or 0,
            }
            continue
        cur["complaints"] += row.get("complaints") or 0
        cur["beneficiaries"] += row.get("beneficiaries") or 0
        if (row.get("complaints") or 0) > cur["_top_complaints"]:
            cur["_top_complaints"] = row.get("complaints") or 0
            cur["index"] = row.get("igr")
            cur["cobertura"] = row.get("cobertura")
    for rec in by_entity.values():
        rec.pop("_top_complaints", None)
    return list(by_entity.values())


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    rated = [r for r in records if isinstance(r.get("index"), (int, float))]
    worst = sorted(rated, key=lambda r: -(r["index"] or 0))[:5]
    return {
        "kind": "ans_complaints_index",
        "source": "ANS",
        "total": len(records),
        "period": records[0]["period"] if records else None,
        "worst": [{"entity": r["entity"], "index": r.get("index"),
                   "complaints": r.get("complaints")} for r in worst],
    }


# --- durable store (same shape as bcb_reclamacoes) ------------------------

def merge(existing: dict[str, Any] | None, records: list[dict[str, Any]], *,
          today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.date.today()
    store: dict[str, dict[str, Any]] = dict((existing or {}).get("records") or {})
    for r in records:
        if r.get("entity"):
            store[r["entity"]] = r
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
    return list((index or {}).get("records", {}).values())


def update_store(records: list[dict[str, Any]], bucket: str, *,
                 s3: Any | None = None, today: dt.date | None = None) -> str | None:
    if not bucket:
        return None
    merged = merge(load_index(bucket, s3=s3), records, today=today)
    return publish(merged, bucket, s3=s3)


def registry_root_resolver(table: Any | None = None) -> Callable[[str], str | None]:
    """A ``cnpj_root -> entity_id`` lookup built from ONE registry scan.

    ``entity_registry.resolve_by_cnpj`` queries per call, which is the right primitive for
    a single lookup and the wrong one here: the IGR file carries ~900 operadoras per
    competência, and doing that many sequential gets inside the ingest Lambda's per-source
    budget times out (measured). This pays one scan and then resolves in memory.
    """
    from src.synth import entity_registry

    roots: dict[str, str] = {}
    for e in entity_registry.list_entities(table=table):
        eid = e.get("entity_id")
        for cr in (e.get("cnpj_roots") or []):
            root = "".join(ch for ch in str(cr) if ch.isdigit())[:8]
            if len(root) == 8 and eid:
                roots.setdefault(root, eid)
    return lambda root: roots.get(root)


def run(bucket: str | None = None, *, today: dt.date | None = None,
        downloader: Callable[[str], bytes | None] | None = None,
        resolver: Callable[[str], str | None] | None = None) -> dict[str, Any]:
    """Fetch → resolve by CNPJ → persist. Standalone/scheduled entrypoint."""
    dl = downloader or _download
    if resolver is None:  # pragma: no cover - wiring
        resolver = registry_root_resolver()
    igr_blob = dl(IGR_URL)
    cadop_blob = dl(CADOP_URL)
    if not igr_blob or not cadop_blob:
        return {"status": "noop", "reason": "source unavailable"}
    rows = parse_igr(igr_blob)
    cadop = parse_cadop(cadop_blob)
    records = map_to_entities(rows, cadop, resolver=resolver, today=today)
    if bucket and records:
        update_store(records, bucket, today=today)
    return {"status": "ok", "rows": len(rows), "operadoras": len(cadop),
            "resolved": len(records), **summarize(records)}
