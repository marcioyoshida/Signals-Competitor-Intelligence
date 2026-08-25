"""Ingest judicial-recovery / bankruptcy filings from the CNJ DataJud public API.

Issue #25 — *Pedidos de recuperação judicial*. The CNJ **DataJud** ("Base
Nacional de Dados do Poder Judiciário") exposes a public Elasticsearch API per
tribunal at ``api-publica.datajud.cnj.jus.br/api_publica_<trib>/_search`` with a
published API key. We query the **classe processual** codes for corporate
distress:

  - ``129`` Recuperação Judicial
  - ``131`` Recuperação Extrajudicial
  - ``130`` Falência

**Privacy caveat (drives the design):** the public API is party-name-scrubbed —
a hit carries ``numeroProcesso``, ``classe``, ``orgaoJulgador``, ``dataAjuizamento``
and ``movimentos`` but **no ``partes``**. So a filing cannot be matched to a
tracked competitor by name here; this source is a **sector-distress signal**
(volume/trend of new RJ/falência filings), not an entity-tied one. Named recovery
events (e.g. a listed issuer's RJ) still arrive entity-matched via news/fatos.
Entity matching would need a paid named-process tier or a news cross-reference.

Best-effort: any HTTP/parse issue degrades to ``[]`` (never breaks the digest).
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Any, Callable, Iterable

import requests

# Published DataJud public API key (CNJ docs). Env-overridable if CNJ rotates it.
API_KEY = os.environ.get(
    "ONCA_DATAJUD_API_KEY",
    "cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw==",
)
BASE = "https://api-publica.datajud.cnj.jus.br/api_publica_{trib}/_search"

# classe processual (CNJ tabela unificada) -> label.
DISTRESS_CLASSES: dict[int, str] = {
    129: "Recuperação Judicial",
    131: "Recuperação Extrajudicial",
    130: "Falência",
}
# Tribunals to sweep. TJSP alone dominates corporate RJ; keep the set small and
# env-tunable (each is one HTTP call). Aliases are the api_publica_<trib> slugs.
DEFAULT_TRIBUNALS = ("tjsp", "tjrj", "tjmg")
# DataJud indexing lags materially (~2 months): a 30-day window comes back empty.
# 90 days catches the lagging filings and suits a slow sector-distress *trend*.
DEFAULT_LOOKBACK_DAYS = 90
DEFAULT_MAX_PER_TRIB = 50


def _parse_ajuizamento(v: Any) -> dt.date | None:
    """dataAjuizamento is ``YYYYMMDDHHMMSS`` (or ISO on some tribunals)."""
    s = str(v or "").strip()
    if not s:
        return None
    if "-" in s:  # ISO-ish
        try:
            return dt.date.fromisoformat(s[:10])
        except ValueError:
            pass
    digits = "".join(ch for ch in s if ch.isdigit())[:8]  # YYYYMMDD prefix
    try:
        return dt.datetime.strptime(digits, "%Y%m%d").date()
    except ValueError:
        return None


def _fetch(trib: str, body: dict[str, Any]) -> dict[str, Any]:
    resp = requests.post(
        BASE.format(trib=trib),
        json=body,
        headers={"Authorization": f"APIKey {API_KEY}", "Content-Type": "application/json"},
        timeout=25,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_recuperacao_judicial(
    tribunals: Iterable[str] = DEFAULT_TRIBUNALS,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    *,
    classes: Iterable[int] = tuple(DISTRESS_CLASSES),
    max_per_trib: int = DEFAULT_MAX_PER_TRIB,
    today: dt.date | None = None,
    fetcher: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Recent corporate-distress filings across ``tribunals``.

    Returns one doc per process (party-name-scrubbed) with a stable ``id`` for
    ``detect_new``. ``fetcher`` is injected in tests.
    """
    today = today or dt.date.today()
    cutoff = today - dt.timedelta(days=lookback_days)
    fetch = fetcher or _fetch
    class_list = [int(c) for c in classes]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for trib in dict.fromkeys(str(t).strip().lower() for t in tribunals if str(t).strip()):
        body = {
            "size": max_per_trib,
            "query": {"terms": {"classe.codigo": class_list}},
            "sort": [{"dataAjuizamento": {"order": "desc"}}],
        }
        try:
            data = fetch(trib, body)
        except Exception as exc:  # pragma: no cover - best-effort per tribunal
            print(f"Warning: DataJud {trib} fetch failed: {exc}")
            continue
        for hit in (((data or {}).get("hits") or {}).get("hits") or []):
            src = hit.get("_source") or {}
            filed = _parse_ajuizamento(src.get("dataAjuizamento"))
            if not filed or filed < cutoff:
                continue
            num = str(src.get("numeroProcesso") or hit.get("_id") or "").strip()
            if not num:
                continue
            doc_id = f"datajud:{trib}:{num}"
            if doc_id in seen:
                continue
            seen.add(doc_id)
            code = int((src.get("classe") or {}).get("codigo") or 0)
            classe = DISTRESS_CLASSES.get(code, (src.get("classe") or {}).get("nome") or "Distress")
            orgao = (src.get("orgaoJulgador") or {}).get("nome") or ""
            trib_up = str(src.get("tribunal") or trib).upper()
            out.append({
                "id": doc_id,
                "kind": "recuperacao_judicial",
                "source": "DataJud-CNJ",
                "classe": classe,
                "classe_codigo": code,
                "tribunal": trib_up,
                "orgao_julgador": orgao,
                "numero_processo": num,
                "date": filed.isoformat(),
                # Party-name-scrubbed: subject is the class + court, not a company.
                "subject": f"{classe} — {trib_up}" + (f" · {orgao}" if orgao else ""),
                # No public per-process URL; DataJud is data-only. Consulta-processual
                # portals are per-tribunal and CAPTCHA-gated, so leave unlinked.
                "url": "https://www.cnj.jus.br/sistemas/datajud/",
            })
    out.sort(key=lambda d: d["date"], reverse=True)
    return out


def summarize(filings: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the distress signal: total + per-class + per-tribunal counts.

    Party-name-scrubbed data has no entity to attach to, so the *volume* is the
    signal — a macro-style sector-distress indicator (rising RJ/falência filings).
    """
    by_class: dict[str, int] = {}
    by_trib: dict[str, int] = {}
    for f in filings:
        by_class[f.get("classe", "?")] = by_class.get(f.get("classe", "?"), 0) + 1
        by_trib[f.get("tribunal", "?")] = by_trib.get(f.get("tribunal", "?"), 0) + 1
    return {
        "kind": "sector_distress",
        "source": "DataJud-CNJ",
        "total": len(filings),
        "by_class": by_class,
        "by_tribunal": by_trib,
    }
