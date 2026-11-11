"""BCB resolution regimes — liquidação extrajudicial / intervenção / RAET (#362).

Why this exists (the Will Bank miss, 2026-11): Will Financeira was put in liquidação
extrajudicial by Ato do Presidente nº 1376 on 2026-01-21 (Banco Master, its controller, on
2025-11-18). Both decrees predate the distress store and its 30-day news sweep, and the news
that followed ("liquidante do Banco Master…", "credores do Will Bank…") names the status, not
the event — so Onça kept showing two liquidated banks as growing competitors.

The BCB publishes every resolution regime it runs, officially and machine-readably
(robots.txt allows ``/api/``), behind the "Regimes de resolução" page
(https://www.bcb.gov.br/estabilidadefinanceira/resolucao):

  - ``GET /api/servico/sitebcb/regime_resolucao_ativo?filtro=(Situacao eq 'Ativo')`` →
    ``{"conteudo": [{"identificador", "NomeIf", "Situacao", ...}]}`` (25 rows on 2026-11-06);
  - ``GET /api/servico/sitebcb/Regesp_Internet/Detalhamento?identificador=<id>`` → CNPJ,
    ``TipoRegime`` (Liquidação Extrajudicial / Intervenção / RAET), ``DataInicio``,
    ``DataEncerramento``, the decree act (``Paragrafo1``), ``Responsavel`` and ``Situacao``
    (for a closed regime, the reason — e.g. "Encerrado (Convolação em falência.)").

The ingest Lambda keeps a snapshot in ``reference/bcb_regimes.json``. A regime that leaves the
active list is re-read once so its closure reason is kept (a conversion into falência is itself
a distress event). The distress watch turns the snapshot into ``regulatory`` events.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
from typing import Any, Callable

BASE = "https://www.bcb.gov.br/api/servico/sitebcb"
ACTIVE_URL = f"{BASE}/regime_resolucao_ativo?filtro=(Situacao%20eq%20%27Ativo%27)"
DETAIL_URL = f"{BASE}/Regesp_Internet/Detalhamento?identificador={{id}}"
PAGE_URL = "https://www.bcb.gov.br/estabilidadefinanceira/resolucao"
SNAPSHOT_KEY = "reference/bcb_regimes.json"
CLOSED_KEEP_DAYS = 365
# A fetch that suddenly lists far fewer regimes is a broken response, not mass closures.
MIN_ACTIVE_RATIO = 0.5
UA = {"User-Agent": "Mozilla/5.0 (compatible; OncaRegulatoryIngest/1.0)",
      "Accept": "application/json"}

KIND_BY_TYPE = {
    "liquidacao extrajudicial": "liquidacao_extrajudicial",
    "intervencao": "intervencao",
    "regime de administracao especial temporaria": "raet",
    "raet": "raet",
}


def _fold(s: Any) -> str:
    import unicodedata

    t = unicodedata.normalize("NFKD", str(s or ""))
    return " ".join("".join(c for c in t if not unicodedata.combining(c)).lower().split())


def kind_for(tipo: Any) -> str | None:
    """Distress kind for a BCB ``TipoRegime`` (None for an unknown regime type)."""
    f = _fold(tipo)
    for k, v in KIND_BY_TYPE.items():
        if f.startswith(k) or k in f:
            return v
    return None


def is_falencia_closure(situacao: Any) -> bool:
    """A regime closed by conversion into (or decree of) bankruptcy."""
    f = _fold(situacao)
    return f.startswith("encerrado") and "falencia" in f


def _act(paragrafo: Any) -> tuple[str | None, str | None]:
    """(act text, act url) from the decree paragraph: "<a href='…'>Ato do Presidente Nº 1376,
    de 21/01/2026</a>"."""
    p = str(paragrafo or "")
    m = re.search(r"<a\s+href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", p, re.S | re.I)
    if not m:
        return None, None
    url = html.unescape(m.group(1)).replace(" ", "%20")
    if url.startswith("/"):
        url = "https://www.bcb.gov.br" + url
    text = re.sub(r"<[^>]+>", "", html.unescape(m.group(2))).strip()
    return text or None, url


def normalize(detail: dict[str, Any]) -> dict[str, Any] | None:
    """One detail payload → a compact regime row (None when it carries no CNPJ)."""
    cnpj = re.sub(r"\D", "", str(detail.get("Cnpj") or ""))
    if len(cnpj) < 8:
        return None
    act, act_url = _act(detail.get("Paragrafo1"))
    return {
        "id": str(detail.get("identificador")),
        "name": str(detail.get("Nome") or detail.get("NomeIf") or "").strip(),
        "cnpj": cnpj[:8],
        "cnpj_full": cnpj,
        "segment": detail.get("Segmento"),
        "tipo": detail.get("TipoRegime"),
        "kind": kind_for(detail.get("TipoRegime")),
        "situacao": detail.get("Situacao"),
        "start": str(detail.get("DataInicio") or "")[:10] or None,
        "end": str(detail.get("DataEncerramento") or "")[:10] or None,
        "act": act,
        "act_url": act_url,
        "responsavel": detail.get("Responsavel"),
        "site": detail.get("Site"),
        "emails": detail.get("Emails"),
    }


def _http_get(url: str) -> Any:
    import requests

    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_active(*, get: Callable[[str], Any] | None = None,
                 prev_active: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every ACTIVE regime, detailed. Raises when the list itself can't be read.

    A regime still on the list whose detail fetch fails keeps its ``prev_active`` row: dropping
    it would read as a CLOSURE in ``merge_state`` (2026-11-11 06:50 run: two details hit the
    ingest budget and two live liquidações were recorded as closed until the next run)."""
    get = get or _http_get
    prev_active = prev_active or {}
    rows = (get(ACTIVE_URL) or {}).get("conteudo") or []
    out: list[dict[str, Any]] = []
    for r in rows:
        rid = r.get("identificador")
        if rid is None:
            continue
        try:
            det = ((get(DETAIL_URL.format(id=rid)) or {}).get("conteudo") or [None])[0]
        except Exception as exc:  # one detail failing never drops the others
            print(f"Warning: BCB regime detail {rid} failed: {exc}")
            det = None
        row = normalize(det) if det else None
        if not row:
            row = prev_active.get(str(rid))
        if row:
            out.append(row)
    return out


def fetch_detail(rid: str, *, get: Callable[[str], Any] | None = None) -> dict[str, Any] | None:
    get = get or _http_get
    det = ((get(DETAIL_URL.format(id=rid)) or {}).get("conteudo") or [None])[0]
    return normalize(det) if det else None


def merge_state(prev: dict[str, Any] | None, active: list[dict[str, Any]], *, as_of: str,
                detail: Callable[[str], dict[str, Any] | None] | None = None) -> dict[str, Any]:
    """New snapshot: ``active`` keyed by regime id; regimes that left the active list move to
    ``closed`` (re-read once via ``detail`` for their closure reason, kept CLOSED_KEEP_DAYS)."""
    prev = prev or {}
    prev_active: dict[str, Any] = dict(prev.get("active") or {})
    closed: dict[str, Any] = dict(prev.get("closed") or {})
    cur = {r["id"]: r for r in active}
    for rid, row in prev_active.items():
        if rid in cur or rid in closed:
            continue
        fresh = None
        if detail:
            try:
                fresh = detail(rid)
            except Exception as exc:
                print(f"Warning: BCB regime closure re-read {rid} failed: {exc}")
        closed[rid] = {**row, **(fresh or {}), "left_active_on": as_of}
    for rid in list(closed):
        if rid in cur:  # back on the active list
            closed.pop(rid)
    floor = (dt.date.fromisoformat(as_of) - dt.timedelta(days=CLOSED_KEEP_DAYS)).isoformat()
    closed = {k: v for k, v in closed.items() if str(v.get("left_active_on") or as_of) >= floor}
    return {"as_of": as_of, "source": PAGE_URL, "active_count": len(cur),
            "active": cur, "closed": closed}


# --- history: every regime the BCB ever ran (1,171 on 2026-11-06, 375 closed by falência) ---------
STATUS_URL = f"{BASE}/Regesp_Internet/ParametroSituacao"
LIST_URL = f"{BASE}/regime_resolucao_ativo?filtro={{filtro}}"
HISTORY_REFRESH_DAYS = 7
_LEGAL_TAIL = re.compile(r"\s+(?:ltda|s\s*/?\s*a|sa|eireli|me|epp)\.?$")


def legal_key(name: Any) -> str:
    """Accent-folded legal name minus its legal-form tail (same rule as the distress watch)."""
    t = re.sub(r"[^a-z0-9 ]+", " ", _fold(name))
    t = re.sub(r"\s+", " ", t).strip()
    prev = None
    while prev != t:
        prev, t = t, _LEGAL_TAIL.sub("", t).strip()
    return t


def fetch_history(*, get: Callable[[str], Any] | None = None) -> dict[str, dict[str, Any]]:
    """id → {name, situacao} for every CLOSED regime (list only; no details)."""
    from urllib.parse import quote

    get = get or _http_get
    out: dict[str, dict[str, Any]] = {}
    for st in [r.get("Nome") for r in ((get(STATUS_URL) or {}).get("conteudo") or [])]:
        if not st or st == "Ativo":
            continue
        rows = (get(LIST_URL.format(filtro=quote(f"(Situacao eq '{st}')"))) or {}).get("conteudo") or []
        for r in rows:
            if r.get("identificador") is not None:
                out[str(r["identificador"])] = {"name": r.get("NomeIf"), "situacao": st}
    return out


def update_history(doc: dict[str, Any], prev: dict[str, Any], *, as_of: str,
                   match_names: set[str] | None, get: Callable[[str], Any] | None = None) -> int:
    """Carry / weekly-refresh the closed-regime list into ``doc["history"]`` and fetch details
    (once, kept in ``doc["history_detail"]``) only for institutions whose legal name equals a
    tracked entity's name — the distress watch then binds a detail only by CNPJ. Returns the
    number of new details fetched."""
    hist = dict(prev.get("history") or {})
    stale = (not hist.get("rows") or not hist.get("as_of")
             or (dt.date.fromisoformat(as_of) - dt.date.fromisoformat(hist["as_of"])).days >= HISTORY_REFRESH_DAYS)
    if stale:
        try:
            rows = fetch_history(get=get)
            if rows and len(rows) >= len(hist.get("rows") or {}) * MIN_ACTIVE_RATIO:
                hist = {"as_of": as_of, "rows": rows}
        except Exception as exc:
            print(f"Warning: BCB regime history refresh failed: {exc}")
    doc["history"] = hist
    details = dict(prev.get("history_detail") or {})
    new = 0
    for rid, row in (hist.get("rows") or {}).items():
        if rid in details or rid in doc.get("active", {}) or not match_names:
            continue
        if legal_key(row.get("name")) not in match_names:
            continue
        try:
            det = fetch_detail(rid, get=get)
        except Exception as exc:
            print(f"Warning: BCB regime history detail {rid} failed: {exc}")
            continue
        if det:
            details[rid] = det
            new += 1
    doc["history_detail"] = details
    return new


def update_state(bucket: str, *, s3: Any = None, get: Callable[[str], Any] | None = None,
                 as_of: str | None = None, match_names: set[str] | None = None) -> dict[str, Any]:
    """Fetch, diff and persist ``reference/bcb_regimes.json``. Never overwrites a good snapshot
    with an empty or collapsed fetch. ``match_names`` (tracked entities' legal-name keys) drives
    the history detail fetch."""
    import boto3

    s3 = s3 or boto3.client("s3")
    as_of = as_of or dt.date.today().isoformat()
    try:
        prev = json.loads(s3.get_object(Bucket=bucket, Key=SNAPSHOT_KEY)["Body"].read())
    except Exception:
        prev = {}
    active = fetch_active(get=get, prev_active=prev.get("active") or {})
    n_prev = len(prev.get("active") or {})
    if not active or (n_prev and len(active) < n_prev * MIN_ACTIVE_RATIO):
        return {"skipped": f"suspicious fetch ({len(active)} active vs {n_prev} before)"}
    doc = merge_state(prev, active, as_of=as_of,
                      detail=lambda rid: fetch_detail(rid, get=get))
    fetched = update_history(doc, prev, as_of=as_of, match_names=match_names, get=get)
    s3.put_object(Bucket=bucket, Key=SNAPSHOT_KEY, ContentType="application/json",
                  Body=json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    return {"active": doc["active_count"], "closed": len(doc["closed"]),
            "history": len((doc.get("history") or {}).get("rows") or {}),
            "history_detail": len(doc["history_detail"]), "history_detail_new": fetched}


if __name__ == "__main__":
    for r in fetch_active():
        print(f"{r['start']}  {r['kind'] or '?':26}  {r['cnpj']}  {r['name'][:60]}  [{r['act']}]")
