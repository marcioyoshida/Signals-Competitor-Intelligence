"""BCB complaints-ranking history per institution (#335, ADR 031 Board M1).

The live reader (``bcb_reclamacoes``) uses Olinda ``RankingReclamacoes``, which serves only the
latest quarter (probed 2026-11-16: 2026-Q3 only). History comes from the file service behind
the BCB ranking page (www3.bcb.gov.br has no robots.txt; dadosabertos.bcb.gov.br disallows
``/api/`` and is not used):

    https://www3.bcb.gov.br/rdrweb/rest/ext/ranking/arquivo
        ?ano=YYYY&periodicidade=SEMESTRAL|TRIMESTRAL&periodo=N&tipo=Bancos e financeiras

HTTP 204 means "not published". Depth: half-yearly 2014-S2 to 2016-S2, quarterly from 2017-Q1
(2022-Q2 was never published). Semicolon CSV, latin-1, one row per conglomerate or
institution, BR decimals.

Three methodologies, recorded per row as ``method`` because the index is not comparable
across them:

* ``regulated_2014`` (2014-S2 to 2024-Q1): regulated founded / regulated other / unregulated /
  total complaints; the index is regulated founded complaints per million clients.
* ``transition_2024`` (2024-Q2): answered complaints and extrapolated regulated founded.
* ``sampled_2024`` (2024-Q3 on): answered, founded, extrapolated founded and analysed: BCB
  analyses a sample and extrapolates; the index is extrapolated founded per million clients.

Size bands change too (two million clients, four million, "Top 10", "Top 15" including payment
institutions); ``category`` is kept verbatim.

Attribution by date. An institution row carries an 8-digit CNPJ root → #330 ``entity_at`` at
the period end. A conglomerate row carries only a name ("ITAU (conglomerado)"); it is matched
against that quarter's IF.data conglomerate names (the #330 register snapshots), which give
the leader's root → ``entity_at``. The name is itself dated (HSBC's 2015 conglomerate is
HSBC's). Unmatched names fall back to a registry name lookup, labelled
``attribution: "name"``.

Store: ``board_history/reclamacoes/{label}.json`` per period (a published period is
immutable) and ``series.json.gz``. It makes no model calls.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import json
import re
import unicodedata
from typing import Any, Callable, Iterable, Mapping

from src.ingest import bcb_entity_history as eh

URL = "https://www3.bcb.gov.br/rdrweb/rest/ext/ranking/arquivo"
TIPO = "Bancos e financeiras"
PUBLIC_URL = "https://www.bcb.gov.br/meubc/rankingreclamacoes"
PREFIX = "board_history/reclamacoes/"
SERIES_KEY = f"{PREFIX}series.json.gz"
SCHEMA = "onca/reclamacoes-history/v1"
FIRST_SEMESTER = (2014, 2)
FIRST_QUARTERLY = 2017

# Header → canonical field. Matched on the normalized header text, longest first, so
# "procedentes extrapoladas" is not read as "procedentes".
_COUNT_COLS = (
    ("quantidade de reclamacoes reguladas procedentes extrapoladas", "procedentes_extrapoladas"),
    ("quantidade de reclamacoes procedentes extrapoladas", "procedentes_extrapoladas"),
    ("quantidade de reclamacoes reguladas procedentes", "reguladas_procedentes"),
    ("quantidade de reclamacoes reguladas - outras", "reguladas_outras"),
    ("quantidade de reclamacoes nao reguladas", "nao_reguladas"),
    ("quantidade total de reclamacoes respondidas", "respondidas"),
    ("quantidade total de reclamacoes analisadas", "analisadas"),
    ("quantidade de reclamacoes procedentes", "procedentes"),
    ("quantidade total de reclamacoes", "total"),
    ("quantidade total de clientes ccs e scr", "clientes"),
    ("quantidade de clientes ccs", "clientes_ccs"),
    ("quantidade de clientes scr", "clientes_scr"),
    ("quantidade de clientes fgc", "clientes_fgc"),
)


def _norm(s: Any) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s).strip()


def name_key(name: Any) -> str:
    """Normalized conglomerate name: no "(conglomerado)", accents, punctuation or S.A."""
    s = _norm(name).replace("(conglomerado)", "")
    s = re.sub(r"\b(s\.?\s?a\.?|s/a)\s*$", "", s.strip())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _num(v: Any) -> float | None:
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return float(s.replace(".", "").replace(",", "."))
    except ValueError:
        return None


# --- periods and fetch -----------------------------------------------------------------

def periods(today: dt.date | None = None) -> list[dict[str, Any]]:
    """Every period that may exist, oldest first: half-years 2014-S2…2016-S2, then quarters."""
    today = today or dt.date.today()
    out = [{"ano": y, "periodicidade": "SEMESTRAL", "periodo": p}
           for y in (2014, 2015, 2016) for p in (1, 2) if (y, p) >= FIRST_SEMESTER]
    for y in range(FIRST_QUARTERLY, today.year + 1):
        for p in (1, 2, 3, 4):
            if dt.date(y, 3 * p, 1) <= today:
                out.append({"ano": y, "periodicidade": "TRIMESTRAL", "periodo": p})
    return out


def label(p: Mapping[str, Any]) -> str:
    return f"{p['ano']}-{'S' if p['periodicidade'] == 'SEMESTRAL' else 'T'}{p['periodo']}"


def period_end(p: Mapping[str, Any]) -> str:
    m = 6 * p["periodo"] if p["periodicidade"] == "SEMESTRAL" else 3 * p["periodo"]
    y = p["ano"]
    return (dt.date(y + m // 12, m % 12 + 1, 1) - dt.timedelta(days=1)).isoformat()


def fetch(p: Mapping[str, Any], get: Callable[..., Any] | None = None) -> bytes | None:
    """The period's CSV, or None when BCB answers 204 (not published)."""
    import requests
    get = get or requests.get
    r = get(URL, params={"ano": p["ano"], "periodicidade": p["periodicidade"],
                         "periodo": p["periodo"], "tipo": TIPO}, timeout=60)
    if r.status_code == 204 or not r.content:
        return None
    r.raise_for_status()
    return r.content


# --- parse -------------------------------------------------------------------------------

def method_of(header: Iterable[str]) -> str:
    h = [_norm(c) for c in header]
    if any("procedentes extrapoladas" in c for c in h):
        return "sampled_2024" if any(c == "quantidade de reclamacoes procedentes" for c in h) else "transition_2024"
    return "regulated_2014"


def _field(header: str) -> str | None:
    h = _norm(header)
    for prefix, key in _COUNT_COLS:
        if h == prefix:
            return key
    return None


def parse(content: bytes, p: Mapping[str, Any]) -> list[dict[str, Any]]:
    text = content.decode("latin-1")
    reader = csv.reader(io.StringIO(text), delimiter=";")
    header = [c.strip() for c in next(reader)]
    method = method_of(header)
    idx = {_norm(c): i for i, c in enumerate(header)}
    fields = [(i, _field(c)) for i, c in enumerate(header)]
    out = []
    for row in reader:
        if not row or not any(x.strip() for x in row):
            continue
        cell = lambda name: row[idx[name]].strip() if name in idx and idx[name] < len(row) else ""  # noqa: E731
        tipo = cell("tipo")
        cnpj = re.sub(r"\D", "", cell("cnpj if"))
        rec = {"period": label(p), "as_of": period_end(p), "method": method,
               "category": cell("categoria") or None,
               "kind": "conglomerado" if _norm(tipo).startswith("conglomerado") else "instituicao",
               "cnpj8": cnpj[:8] if len(cnpj) >= 8 else None,
               "name": cell("instituicao financeira"),
               "indice": _num(cell("indice"))}
        for i, key in fields:
            if key and i < len(row):
                rec[key] = _num(row[i])
        out.append(rec)
    return out


# --- attribution -------------------------------------------------------------------------

def conglomerate_leaders(snapshot: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """{normalized name: CNPJ root} from one IF.data register snapshot: each conglomerate's
    name to its leader's root, then each institution's own name to its root (a conglomerate
    name wins a clash). Caixa is ranked as a "conglomerado" but in IF.data is a single
    institution, so the institution names are needed too."""
    out: dict[str, str] = {}
    rows = list(snapshot)
    for r in rows:
        code = str(r.get("CodInst") or "")
        leader = re.sub(r"\D", "", str(r.get("CnpjInstituicaoLider") or ""))[:8]
        if code.startswith("C") and len(leader) == 8:
            out.setdefault(name_key(r.get("NomeInstituicao")), leader)
    for r in rows:
        code = str(r.get("CodInst") or "")
        if code[:1].isdigit() and len(code) >= 8:
            out.setdefault(name_key(r.get("NomeInstituicao")), code[:8])
    return out


def attribute(rows: list[dict[str, Any]], emap: Mapping[str, Any], leaders: Mapping[str, str],
              resolve_name: Callable[[str], list[str]] | None = None) -> list[dict[str, Any]]:
    """Set ``entity_id``, ``direct_entity_id`` and ``attribution`` on each row."""
    for r in rows:
        r["entity_id"] = r["direct_entity_id"] = None
        r["attribution"] = None
        if r["kind"] == "instituicao" and r.get("cnpj8"):
            r["entity_id"] = eh.entity_at(emap, r["cnpj8"], r["as_of"])
            r["direct_entity_id"] = eh.entity_at(emap, r["cnpj8"], r["as_of"], level="direct")
            r["attribution"] = "dated" if r["entity_id"] else None
            continue
        leader = leaders.get(name_key(r["name"]))
        if leader:
            r["leader_cnpj8"] = leader
            r["entity_id"] = r["direct_entity_id"] = eh.entity_at(emap, leader, r["as_of"])
            r["attribution"] = "dated" if r["entity_id"] else None
        if not r["entity_id"] and resolve_name:
            hits = resolve_name(name_key(r["name"]))
            if len(hits) == 1:
                r["entity_id"] = r["direct_entity_id"] = hits[0]
                r["attribution"] = "name"
    return rows


# --- series ------------------------------------------------------------------------------

def build_series(rows: Iterable[Mapping[str, Any]], built_at: str,
                 published: Iterable[str] = (), missing: Iterable[str] = ()) -> dict[str, Any]:
    """{entity: [rows sorted by date]}; conglomerate rows first when an entity has both."""
    ents: dict[str, list[dict[str, Any]]] = {}
    unattributed: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("entity_id"):
            ents.setdefault(r["entity_id"], []).append(dict(r))
        else:
            u = unattributed.setdefault(r["name"], {"rows": 0, "last": r["as_of"], "cnpj8": r.get("cnpj8")})
            u["rows"] += 1
            u["last"] = max(u["last"], r["as_of"])
    for pts in ents.values():
        pts.sort(key=lambda r: (r["as_of"], r["kind"] != "conglomerado", r.get("cnpj8") or ""))
    return {"schema": SCHEMA, "built_at": built_at, "source": URL, "public_url": PUBLIC_URL,
            "periods": list(published), "not_published": list(missing),
            "rows": sum(len(v) for v in ents.values()), "entities": ents,
            "unattributed": dict(sorted(unattributed.items(), key=lambda kv: -kv[1]["rows"]))}


def series_for(s: Mapping[str, Any], entity_id: str, *, method: str | None = None,
               conglomerate_only: bool = True) -> list[dict[str, Any]]:
    """One entity's rows, oldest first. By default the conglomerate row of each period (the
    published headline); an entity ranked only as a single institution keeps that row."""
    pts = (s.get("entities") or {}).get(entity_id) or []
    if method:
        pts = [p for p in pts if p["method"] == method]
    if not conglomerate_only:
        return list(pts)
    out, seen = [], set()
    for p in pts:                     # sorted with the conglomerate row first per period
        if p["period"] not in seen:
            out.append(p)
            seen.add(p["period"])
    return out


def put_json(s3: Any, bucket: str, key: str, doc: Any, *, gz: bool = False) -> None:
    body = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()
    s3.put_object(Bucket=bucket, Key=key, ContentType="application/json",
                  **({"ContentEncoding": "gzip"} if gz else {}), Body=gzip.compress(body) if gz else body)


def get_json(s3: Any, bucket: str, key: str) -> Any:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception:  # noqa: BLE001
        return None
    try:
        body = gzip.decompress(body)
    except OSError:
        pass
    return json.loads(body)
