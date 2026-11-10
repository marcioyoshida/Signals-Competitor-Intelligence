"""Dated CNPJ ↔ entity map (#330, ADR 031 Board M1): who owned which institution, when.

Historic backfills (IF.data, COSIF, KM1, CVM IPE, rate tables) are keyed by CNPJ. The
registry's ``CNPJ#`` items only say who owns a root *today*. That attributes HSBC Bank
Brasil's 2015 numbers to Bradesco, or drops them, because Bradesco only bought it in 2016.
Mergers and renames need dates.

Source: BCB IF.data ``IfDataCadastro``. It is a quarterly register of every supervised
institution, from 2000-03 to the latest published quarter (probed 2026-11-10: 3,972 rows
in 2000-03, 5,799 in 2025-06). Each row carries the 8-digit CNPJ root (``CodInst``), the
name as of that quarter, the status, the financial and (from 2014) prudential
conglomerate codes, and the 8-digit root of the conglomerate's leader
(``CnpjInstituicaoLider``). For example, HSBC Bank Brasil (01701201) is in HSBC's
conglomerate in 2015-12 and in Bradesco's (leader 60746948) in 2016-09.

Two attributions are kept for every quarter, because consumers need different ones:

* ``entity_id`` (level ``direct``): the registry entity that owns the root itself.
* ``group_entity_id`` (level ``group``): the registry entity that owns the conglomerate
  *leader's* root. Prudential and consolidated facts belong here. Use this level to
  ask "whose numbers were these at that date".

Both go through the registry's ``canonical_id`` merge chains (the same survivor the
feed uses). That resolves the duplicate-id trap (``itau`` vs ``itau-unibanco``) the
same way everywhere, instead of by ``sorted(ids)[0]``.

What this cannot see: asset deals with no change of CNPJ ownership, such as Citi's retail
book moving to Itaú in 2016–17. Those need a curated override, not this register.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import time
from typing import Any, Callable, Iterable, Mapping

BASE = "https://olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1/odata"
PUBLIC_URL = "https://www3.bcb.gov.br/ifdata/"
FIRST_QUARTER = 200003
MAP_KEY = "entity_history/cnpj_map.json"
SNAPSHOT_PREFIX = "entity_history/cadastro/"
SCHEMA = "onca/entity-history/v1"

# Statuses that mean the institution exists at that date. "I" (inativa) rows linger
# in the register for years after a merger or cancellation, so counting them would
# keep an absorbed bank "alive" under its old owner. Liquidation and bankruptcy
# (LE/LO/F) are kept: the legal entity still exists and still files.
IN_FORCE = frozenset({"A", "LE", "LO", "F"})

# Only these fields are kept from a snapshot, which keeps the cached quarters small.
_FIELDS = ("CodInst", "NomeInstituicao", "Situacao", "CodConglomeradoFinanceiro",
           "CodConglomeradoPrudencial", "CnpjInstituicaoLider")


# --- quarters -------------------------------------------------------------------

def quarter_of(as_of: Any) -> int:
    """The latest quarter end (YYYYMM) on or before ``as_of`` (date, ISO string or YYYYMM)."""
    if isinstance(as_of, int):
        y, m = divmod(as_of, 100)
    else:
        d = as_of if isinstance(as_of, dt.date) else dt.date.fromisoformat(str(as_of)[:10])
        y, m = d.year, d.month
        # A quarter-end month counts only from its last day; earlier days fall back a quarter.
        if m % 3 == 0 and d.day < _month_end(y, m):
            m -= 1
    q = (m // 3) * 3
    if q == 0:
        return (y - 1) * 100 + 12
    return y * 100 + q


def _month_end(y: int, m: int) -> int:
    nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
    return (nxt - dt.timedelta(days=1)).day


def quarters(start: int = FIRST_QUARTER, end: int | None = None) -> list[int]:
    """Every quarter end from ``start`` to ``end`` inclusive, oldest first."""
    end = end or quarter_of(dt.date.today())
    out, y, m = [], start // 100, start % 100
    while y * 100 + m <= end:
        out.append(y * 100 + m)
        m += 3
        if m > 12:
            y, m = y + 1, m - 12
    return out


# --- fetch ----------------------------------------------------------------------

def cadastro_url(anomes: int) -> str:
    return (f"{BASE}/IfDataCadastro(AnoMes=@AnoMes)?@AnoMes={anomes}"
            f"&$format=json&$top=20000")


def slim(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{k: r.get(k) for k in _FIELDS} for r in rows]


def fetch_cadastro(anomes: int, *, session: Any | None = None, retries: int = 3,
                   timeout: int = 90) -> list[dict[str, Any]]:
    """One quarter of the register (slimmed). An unpublished quarter returns []."""
    if session is None:
        import requests

        session = requests
    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = session.get(cadastro_url(anomes), timeout=timeout)
            resp.raise_for_status()
            return slim(resp.json().get("value") or [])
        except Exception as exc:  # network / 5xx: back off and retry
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"IfDataCadastro {anomes} failed after {retries} tries: {last}")


def latest_published(*, fetch: Callable[[int], list[dict[str, Any]]] = fetch_cadastro,
                     today: dt.date | None = None, back: int = 6) -> int | None:
    """Newest quarter with rows (IF.data publishes ~3 months after quarter end)."""
    q = quarter_of(today or dt.date.today())
    for _ in range(back):
        if fetch(q):
            return q
        q = q - 3 if q % 100 > 3 else (q // 100 - 1) * 100 + 12
    return None


# --- build ------------------------------------------------------------------------

def _root(v: Any) -> str | None:
    s = "".join(c for c in str(v or "") if c.isdigit())
    return s[:8] if len(s) >= 8 else None


def observations(rows: Iterable[Mapping[str, Any]], root_map: Mapping[str, str],
                 canon: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """{cnpj_root: what that quarter says about it}, in-force institutions only.

    Conglomerate rows (``CodInst`` starting with ``C``) are not institutions and are skipped;
    their codes still reach every member row.
    """
    def ent(root: str | None) -> str | None:
        eid = root_map.get(root) if root else None
        return canon.get(eid, eid) if eid else None

    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        code = str(r.get("CodInst") or "")
        root = _root(code) if code[:1].isdigit() else None
        if not root or (r.get("Situacao") or "") not in IN_FORCE:
            continue
        leader = _root(r.get("CnpjInstituicaoLider"))
        group = r.get("CodConglomeradoPrudencial") or r.get("CodConglomeradoFinanceiro")
        direct = ent(root)
        out[root] = {
            "name": (r.get("NomeInstituicao") or "").strip(),
            "status": r.get("Situacao"),
            "group": group,
            "leader": leader,
            "entity_id": direct,
            # An institution outside any conglomerate is its own group.
            "group_entity_id": ent(leader) if leader else direct,
        }
    return out


_KEY = ("name", "status", "group", "leader", "entity_id", "group_entity_id")


def build_map(snapshots: Mapping[int, list[dict[str, Any]]], root_map: Mapping[str, str],
              canon: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Collapse quarterly observations into intervals per CNPJ root.

    A new interval starts whenever anything in ``_KEY`` changes, so renames, a change
    of owner, liquidation and a gap (absent or inactive for a quarter) are all visible.
    ``from``/``to`` are the first and last quarters observed (YYYYMM, inclusive).
    """
    canon = canon or {}
    qs = sorted(snapshots)
    roots: dict[str, list[dict[str, Any]]] = {}
    prev_q: int | None = None
    for q in qs:
        obs = observations(snapshots[q], root_map, canon)
        for root, o in obs.items():
            ivs = roots.setdefault(root, [])
            last = ivs[-1] if ivs else None
            if last and last["to"] == prev_q and all(last[k] == o[k] for k in _KEY):
                last["to"] = q
            else:
                ivs.append({"from": q, "to": q, **o})
        prev_q = q
    return {
        "schema": SCHEMA,
        "source": "BCB IF.data IfDataCadastro",
        "source_url": PUBLIC_URL,
        "quarters": [qs[0], qs[-1]] if qs else [],
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "roots": roots,
    }


# --- lookups ----------------------------------------------------------------------

def _field(level: str) -> str:
    if level not in ("group", "direct"):
        raise ValueError("level must be 'group' or 'direct'")
    return "group_entity_id" if level == "group" else "entity_id"


def _clamp(m: Mapping[str, Any], as_of: Any) -> int | None:
    span = m.get("quarters") or []
    if not span:
        return None
    q = quarter_of(as_of)
    if q < span[0]:
        return None
    # After the last published quarter, the last known state is still in force.
    return min(q, span[1])


def interval_at(m: Mapping[str, Any], cnpj_root: Any, as_of: Any) -> dict[str, Any] | None:
    q, root = _clamp(m, as_of), _root(cnpj_root)
    if q is None or not root:
        return None
    for iv in (m.get("roots") or {}).get(root) or []:
        if iv["from"] <= q <= iv["to"]:
            return iv
    return None


def entity_at(m: Mapping[str, Any], cnpj_root: Any, as_of: Any,
              level: str = "group") -> str | None:
    """The registry entity a CNPJ root belonged to at ``as_of``, or None.

    None means not in force at that date (not yet authorised, inactive, absorbed) or
    not a registry entity. Callers must not fall back to today's owner.
    """
    iv = interval_at(m, cnpj_root, as_of)
    return iv.get(_field(level)) if iv else None


def members(m: Mapping[str, Any], entity_id: str, as_of: Any,
            level: str = "group") -> list[str]:
    """Every CNPJ root attributed to ``entity_id`` at ``as_of`` (sorted)."""
    q, f = _clamp(m, as_of), _field(level)
    if q is None:
        return []
    return sorted(root for root, ivs in (m.get("roots") or {}).items()
                  for iv in ivs if iv["from"] <= q <= iv["to"] and iv.get(f) == entity_id)


# --- store ------------------------------------------------------------------------

def snapshot_key(anomes: int) -> str:
    return f"{SNAPSHOT_PREFIX}{anomes}.json.gz"


def put_snapshot(s3: Any, bucket: str, anomes: int, rows: list[dict[str, Any]]) -> None:
    body = gzip.compress(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode())
    s3.put_object(Bucket=bucket, Key=snapshot_key(anomes), Body=body,
                  ContentType="application/json", ContentEncoding="gzip")


def get_snapshot(s3: Any, bucket: str, anomes: int) -> list[dict[str, Any]] | None:
    try:
        raw = s3.get_object(Bucket=bucket, Key=snapshot_key(anomes))["Body"].read()
    except Exception as exc:
        if "NoSuchKey" in type(exc).__name__ or "NoSuchKey" in str(exc):
            return None
        raise
    return json.loads(gzip.decompress(raw))


def put_map(s3: Any, bucket: str, m: Mapping[str, Any]) -> None:
    s3.put_object(Bucket=bucket, Key=MAP_KEY, ContentType="application/json",
                  Body=json.dumps(m, ensure_ascii=False, separators=(",", ":")).encode())


def load_map(s3: Any, bucket: str) -> dict[str, Any] | None:
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=MAP_KEY)["Body"].read())
    except Exception as exc:
        if "NoSuchKey" in type(exc).__name__ or "NoSuchKey" in str(exc):
            return None
        raise


def canonical_map(entities: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """{duplicate_id: survivor} from the registry's canonical_id chains (feed's own rule)."""
    from src.dashboard.feed_builder import _canonical_map

    return _canonical_map({str(e["entity_id"]): {"canonical_id": e.get("canonical_id")}
                           for e in entities if e.get("entity_id")})
