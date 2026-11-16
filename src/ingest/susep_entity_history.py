"""Dated ownership map for insurers (#389, ADR 031 Board M1): the SUSEP counterpart of #330.

#330 dates who owned each bank per quarter from the BCB register. Insurers are not in it, so
an insurer's past filings and numbers could only go to today's registry entity.

Source: SUSEP's statistics system (SES), ``BaseCompleta.zip``. One table matters here:
``Ses_grupos_economicos.csv`` (``damesano;coenti;noenti;cogrupo;nogrupo``), the economic
group of every supervised company (seguradora, EAPC, capitalização, resseguradora) every
month from 1999-05 (probed 2026-11-16: 66,940 rows, 462 companies, 122 groups to 2026-09).
The archive is 580 MB. Only the 0.6 MB member is read, through HTTP range requests.

Three traps:

* ``99999 OUTROS GRUPOS``, ``01225 INDEPENDENTE`` and a blank group are placeholders, not
  owners. Chubb's and Allianz's Brazilian companies sit in INDEPENDENTE from 2003–04 on.
  A placeholder month attributes the company to its own entity, never to the last named group
  (Prudential do Brasil left Bradesco's group in 2002 and then shows INDEPENDENTE).
* SES carries no CNPJ. The company code (``coenti``) equals ``CodigoFIP`` on SUSEP's current
  company list, which has the CNPJ. Companies that have since left the market keep their code
  only, so they are in ``codes`` but not in ``roots``.
* The registry's alias index resolves none of SES's group names. Groups map to registry
  entities through the curated ``config/susep_groups.json``. An unmapped real group gives no
  group entity, never a guess.

Map shape, the same as #330 so ``bcb_entity_history.entity_at`` works unchanged: quarterly
intervals (``from``/``to`` YYYYMM, inclusive) per CNPJ root under ``roots``, plus the same
intervals per SUSEP code under ``codes`` (for SES financial tables, #390). Each interval has
``name``, ``susep_code``, ``group``, ``group_name``, ``entity_id`` (direct, via the CNPJ) and
``group_entity_id`` (the mapped group, or the direct entity when the group is a placeholder).
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import json
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.ingest import bcb_entity_history as eh

BASE_ZIP = "https://www2.susep.gov.br/download/estatisticas/BaseCompleta.zip"
GROUPS_MEMBER = "Ses_grupos_economicos.csv"
PUBLIC_URL = "https://www2.susep.gov.br/menuestatistica/SES/principal.aspx"
MAP_KEY = "entity_history/susep_map.json.gz"
SCHEMA = "onca/susep-entity-history/v1"
GROUPS_CONFIG = Path(__file__).resolve().parents[2] / "config" / "susep_groups.json"
PLACEHOLDER_GROUPS = frozenset({"99999", "01225", ""})
QUARTER_MONTHS = (3, 6, 9, 12)
COMPLETE_RATIO = 0.95

_KEY = ("name", "group", "entity_id", "group_entity_id", "cnpj_root")


# --- fetch (HTTP range reads of one ZIP member) ----------------------------------

class _RangeFile(io.RawIOBase):
    """A read-only, seekable view of a remote file through HTTP Range requests."""

    def __init__(self, url: str, session: Any | None = None, timeout: int = 120):
        import requests
        self.url, self.pos, self.timeout = url, 0, timeout
        self.http = session or requests.Session()
        self.size = int(self.http.head(url, timeout=timeout, allow_redirects=True)
                        .headers["Content-Length"])

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, off: int, whence: int = 0) -> int:
        self.pos = {0: off, 1: self.pos + off, 2: self.size + off}[whence]
        return self.pos

    def readinto(self, b) -> int:  # noqa: ANN001
        n = min(len(b), self.size - self.pos)
        if n <= 0:
            return 0
        r = self.http.get(self.url, headers={"Range": f"bytes={self.pos}-{self.pos + n - 1}"},
                          timeout=self.timeout)
        if r.status_code != 206:
            raise IOError(f"range read refused: HTTP {r.status_code}")
        data = r.content
        b[:len(data)] = data
        self.pos += len(data)
        return len(data)


def fetch_member(name: str = GROUPS_MEMBER, url: str = BASE_ZIP) -> bytes:
    """One member of SES's BaseCompleta.zip, without downloading the 580 MB archive."""
    with zipfile.ZipFile(io.BufferedReader(_RangeFile(url), buffer_size=1 << 20)) as z:
        return z.read(name)


def parse_groups(content: bytes) -> list[dict[str, str]]:
    """Rows of ``Ses_grupos_economicos.csv``, trimmed; rows without a month are dropped."""
    out = []
    for r in csv.DictReader(io.StringIO(content.decode("latin-1")), delimiter=";"):
        row = {k: (r.get(k) or "").strip() for k in ("damesano", "coenti", "noenti", "cogrupo", "nogrupo")}
        if len(row["damesano"]) == 6 and row["damesano"].isdigit() and row["coenti"]:
            out.append(row)
    return out


def load_group_config(path: Path | str = GROUPS_CONFIG) -> dict[str, str]:
    """{SES group code: registry entity_id} from the curated config."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return {code: g["entity_id"] for code, g in (doc.get("groups") or {}).items() if g.get("entity_id")}


def code_roots(susep_rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """{SUSEP code: CNPJ root} from SUSEP's current company list (``susep_entidades``)."""
    out = {}
    for r in susep_rows:
        code, cnpj = str(r.get("codigo_fip") or "").strip(), str(r.get("cnpj") or "")
        if code and len(cnpj) >= 8:
            out[code] = cnpj[:8]
    return out


# --- build ------------------------------------------------------------------------

def observations(rows: Iterable[Mapping[str, str]], month: str, roots_by_code: Mapping[str, str],
                 root_map: Mapping[str, str], group_map: Mapping[str, str],
                 canon: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """{SUSEP code: what month ``month`` says about it}."""
    def ent(eid: str | None) -> str | None:
        return canon.get(eid, eid) if eid else None

    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["damesano"] != month:
            continue
        code = r["coenti"]
        root = roots_by_code.get(code)
        direct = ent(root_map.get(root)) if root else None
        group = r["cogrupo"] if r["cogrupo"] not in PLACEHOLDER_GROUPS else ""
        out[code] = {
            "name": r["noenti"],
            "susep_code": code,
            "cnpj_root": root,
            "group": group or None,
            "group_name": r["nogrupo"] if group else None,
            "entity_id": direct,
            # A placeholder group means "no named owner": the company is its own group.
            "group_entity_id": ent(group_map.get(group)) if group else direct,
        }
    return out


def complete_through(rows: Iterable[Mapping[str, str]], ratio: float = COMPLETE_RATIO) -> str | None:
    """The latest month SES has fully published. Late filers make the newest month partial
    (2026-09 had 226 companies against 248 in 2026-08, IRB Re among the missing), and a
    partial month would read as dozens of market exits. A trailing month under ``ratio`` of
    the month before is dropped, repeatedly."""
    count: dict[str, int] = {}
    for r in rows:
        count[r["damesano"]] = count.get(r["damesano"], 0) + 1
    ms = sorted(count)
    while len(ms) > 1 and count[ms[-1]] < ratio * count[ms[-2]]:
        ms.pop()
    return ms[-1] if ms else None


def quarter_months(rows: Iterable[Mapping[str, str]]) -> list[str]:
    rows = list(rows)
    last = complete_through(rows)
    return sorted({r["damesano"] for r in rows
                   if int(r["damesano"][4:]) in QUARTER_MONTHS and last and r["damesano"] <= last})


def build_map(rows: list[Mapping[str, str]], roots_by_code: Mapping[str, str],
              root_map: Mapping[str, str], group_map: Mapping[str, str],
              canon: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Collapse quarter-end observations into intervals per SUSEP code and per CNPJ root.

    A new interval starts when anything in ``_KEY`` changes or the company is absent for a
    quarter, so a change of group and a market exit are both visible."""
    canon = canon or {}
    rows = list(rows)
    by_month: dict[str, list[Mapping[str, str]]] = {}
    for r in rows:
        by_month.setdefault(r["damesano"], []).append(r)
    qs = quarter_months(rows)
    codes: dict[str, list[dict[str, Any]]] = {}
    prev: int | None = None
    for m in qs:
        q = int(m)
        for code, o in observations(by_month[m], m, roots_by_code, root_map, group_map, canon).items():
            ivs = codes.setdefault(code, [])
            last = ivs[-1] if ivs else None
            if last and last["to"] == prev and all(last[k] == o[k] for k in _KEY):
                last["to"] = q
            else:
                ivs.append({"from": q, "to": q, **o})
        prev = q
    roots: dict[str, list[dict[str, Any]]] = {}
    for ivs in codes.values():
        for iv in ivs:
            if iv["cnpj_root"]:
                roots.setdefault(iv["cnpj_root"], []).append(iv)
    for ivs in roots.values():
        ivs.sort(key=lambda iv: iv["from"])
    return {
        "schema": SCHEMA,
        "source": "SUSEP SES Ses_grupos_economicos",
        "source_url": PUBLIC_URL,
        "regulator": "susep",
        "quarters": [int(qs[0]), int(qs[-1])] if qs else [],
        "complete_through": complete_through(rows),
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "groups": {code: eid for code, eid in sorted(group_map.items())},
        "codes": codes,
        "roots": roots,
    }


# --- lookups ----------------------------------------------------------------------

def code_interval_at(m: Mapping[str, Any], code: str, as_of: Any) -> dict[str, Any] | None:
    """The interval of SUSEP company ``code`` at ``as_of`` (for SES tables keyed by code)."""
    span = m.get("quarters") or []
    if not span:
        return None
    q = eh.quarter_of(as_of)
    if q < span[0]:
        return None
    q = min(q, span[1])
    for iv in (m.get("codes") or {}).get(str(code).strip()) or []:
        if iv["from"] <= q <= iv["to"]:
            return iv
    return None


def code_entity_at(m: Mapping[str, Any], code: str, as_of: Any, level: str = "group") -> str | None:
    iv = code_interval_at(m, code, as_of)
    return iv.get("group_entity_id" if level == "group" else "entity_id") if iv else None


def changes(m: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every quarter a company moved between two NAMED groups (placeholders skipped)."""
    out = []
    for code, ivs in (m.get("codes") or {}).items():
        named = [iv for iv in ivs if iv.get("group")]
        for a, b in zip(named, named[1:]):
            if a["group"] != b["group"]:
                out.append({"susep_code": code, "name": b["name"], "at": b["from"],
                            "from_group": a["group_name"], "to_group": b["group_name"],
                            "from_entity": a["group_entity_id"], "to_entity": b["group_entity_id"]})
    return sorted(out, key=lambda c: (c["at"], c["susep_code"]))


# --- store ------------------------------------------------------------------------

def put_map(s3: Any, bucket: str, m: Mapping[str, Any]) -> None:
    s3.put_object(Bucket=bucket, Key=MAP_KEY, ContentType="application/json", ContentEncoding="gzip",
                  Body=gzip.compress(json.dumps(m, ensure_ascii=False, separators=(",", ":")).encode()))


def load_map(s3: Any, bucket: str) -> dict[str, Any] | None:
    try:
        body = s3.get_object(Bucket=bucket, Key=MAP_KEY)["Body"].read()
    except Exception:  # noqa: BLE001 - absent map = no insurer attribution yet
        return None
    try:
        body = gzip.decompress(body)
    except OSError:
        pass
    return json.loads(body)
