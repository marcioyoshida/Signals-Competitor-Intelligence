"""#301 / ADR 022 Phase 6 — multi-bank Pilar 3 *text* from the qualitative tables in BCB DASFN.

The spike (2026-11-02) found the uniform route the IR-site PDFs never were. The same DASFN catalog
that gives us KM1 (`bcb_km1`) also registers each institution's **qualitative** Pilar 3 tables
(Res. BCB 54/20). These are prose fields in the bank's own JSON:

    OVA    visão geral do gerenciamento de riscos       LIQA   risco de liquidez
    CRA    risco de crédito                             MRA    risco de mercado
    IRRBBA risco de taxa de juros (carteira bancária)  ORA    risco operacional

Live coverage of the tracked banks: real prose from BB, BTG, Safra, C6, BV, Pan, BMG, Agibank,
Mercado Pago and Stone. Itaú, Nubank and Santander publish only a pointer ("see our Pilar 3 PDF"),
so they are skipped; Itaú comes from CVM IPE (`pilar3`). Bradesco and Inter return 404, PicPay 403.

**robots.txt, per host (RFC 9309):** a 2xx file is obeyed, a 4xx means no restrictions, and a 5xx
or an unreachable host means *disallow*. That excludes ABC Brasil and Sicoob today.

Each bank becomes one record in `pilar3/index.json` (`kind="dasfn"`). Its risk sentences feed
`financial_tone`, and its prose (`_prose`, in memory only, never stored in the index) feeds the KB
through `pilar3.promote_to_kb`.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.parse
import urllib.robotparser
from typing import Any, Callable

import requests

DASFN = "https://olinda.bcb.gov.br/olinda/servico/DASFN/versao/v1/odata"
TABLES = {
    "ova": "Visão geral do gerenciamento de riscos (OVA)",
    "liqa": "Gerenciamento do risco de liquidez (LIQA)",
    "cra": "Gerenciamento do risco de crédito (CRA)",
    "mra": "Gerenciamento do risco de mercado (MRA)",
    "irrbba": "Risco de taxa de juros da carteira bancária (IRRBBA)",
    "ora": "Gerenciamento do risco operacional (ORA)",
}
DOC_TYPE = "Pilar 3 — tabelas qualitativas (BCB DASFN)"
MIN_PROSE_CHARS = 600            # below this the "table" is a pointer to a PDF, not content
MAX_AGE_YEARS = 2                # Pan's latest tables are 2022: too old to score tone on
_UA = {"User-Agent": "Mozilla/5.0"}
_ROBOTS_UA = "Onca-CI"


def _year(row: dict[str, Any]) -> int:
    """Reference year of a catalog row. "…_20251231_ova.json" -> 2025 (a filing stamp such as
    "26-04-02_202451" must not read as 2024); else a bare year in the argument or the URL."""
    for text in (row.get("Argumento") or "", row.get("URLDados") or ""):
        m = re.search(r"(20\d\d)1231", text)
        if m:
            return int(m.group(1))
    for text in (row.get("Argumento") or "", row.get("URLDados") or ""):
        m = re.search(r"(?<!\d)(20\d\d)(?!\d)", text)
        if m:
            return int(m.group(1))
    return 0


def list_qualitative(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{cnpj: {name, year, tables: {table: url}}}. Per table, the latest year wins, and /v2/ wins
    a tie. The record's year is its OVA year (else the newest table's)."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        parts = (r.get("Recurso") or "").split("/")
        table = parts[1] if len(parts) > 1 else ""
        cnpj, url = r.get("CnpjInstituicao"), r.get("URLDados")
        if table not in TABLES or not cnpj or not url:
            continue
        rank = (_year(r), "/v2/" in (r.get("Recurso") or ""))
        rec = out.setdefault(cnpj, {"name": r.get("NomeInstituicao"), "tables": {}, "_rank": {}})
        if rank > rec["_rank"].get(table, (-1, False)):
            rec["tables"][table] = url
            rec["_rank"][table] = rank
    for rec in out.values():
        ranks = rec.pop("_rank")
        rec["year"] = (ranks.get("ova") or max(ranks.values()))[0]
    return out


class Robots:
    """Per-host robots.txt cache with the RFC 9309 status rules."""

    def __init__(self, get: Callable[..., Any] | None = None):
        self._get = get or (lambda u: requests.get(u, timeout=20, headers={"User-Agent": _ROBOTS_UA}))
        self._cache: dict[str, Any] = {}

    def allowed(self, url: str) -> bool:
        p = urllib.parse.urlparse(url)
        host = f"{p.scheme}://{p.netloc}"
        if host not in self._cache:
            try:
                r = self._get(host + "/robots.txt")
                status = r.status_code
            except Exception:
                status = None
            if status is not None and 200 <= status < 300:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
                self._cache[host] = rp
            elif status is not None and 400 <= status < 500:
                self._cache[host] = True               # no robots file: unrestricted
            else:
                self._cache[host] = False              # 5xx or unreachable: assume disallow
        rule = self._cache[host]
        return rule if isinstance(rule, bool) else rule.can_fetch(_ROBOTS_UA, url)


def _fetch_json(url: str) -> Any:
    r = requests.get(url, timeout=(8, 25), headers=_UA)
    r.raise_for_status()
    body = r.content.decode("utf-8-sig", errors="replace").strip()
    body = re.sub(r"^\s*/\*|\*/\s*$", "", body).strip()
    return json.loads(body, strict=False)            # some banks embed raw control characters


def prose_blocks(doc: Any, *, min_words: int = 12) -> list[str]:
    """The prose fields of a qualitative table, in document order (codes and numbers dropped)."""
    out: list[str] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            s = re.sub(r"\s+", " ", o).strip()
            if len(s.split()) >= min_words and s not in out:
                out.append(s)

    walk(doc)
    return out


def bank_prose(tables: dict[str, str], *, robots: Robots, fetch: Callable[[str], Any] = _fetch_json,
               pause_s: float = 0.5, sleep: Callable[[float], None] = time.sleep) -> tuple[str, list[str]]:
    """Fetch a bank's qualitative tables and join them into one sectioned text. Returns
    (prose, table names used). Tables blocked by robots, failing, or pointer-only are skipped."""
    sections, used = [], []
    dead: set[str] = set()
    for table in TABLES:
        url = tables.get(table)
        host = urllib.parse.urlparse(url or "").netloc
        if not url or host in dead or not robots.allowed(url):
            continue
        try:
            blocks = prose_blocks(fetch(url))
        except Exception as exc:  # self-hosted endpoints vary
            # Circuit breaker: one failure skips the host's other tables. Live 2026-11-02, a
            # hanging host (connect timeout x 6 tables) spent the whole budget on one bank.
            print(f"Warning: DASFN {table} fetch failed ({host}): {str(exc)[:80]}")
            dead.add(host)
            blocks = []
        finally:
            sleep(pause_s)
        text = "\n\n".join(blocks)
        if len(text) >= MIN_PROSE_CHARS // 2:
            sections.append(f"{TABLES[table]}\n\n{text}")
            used.append(table)
    prose = "\n\n".join(sections)
    return (prose, used) if len(prose) >= MIN_PROSE_CHARS else ("", [])


def build(resources: dict[str, dict[str, Any]], *, resolver: Callable[[dict[str, Any]], list[str]],
          robots: Robots | None = None, fetch: Callable[[str], Any] = _fetch_json,
          budget_s: float = 240.0, clock: Callable[[], float] = time.monotonic,
          sleep: Callable[[float], None] = time.sleep,
          today: dt.date | None = None) -> dict[str, dict[str, Any]]:
    """Resolve each institution to a tracked entity and pull its qualitative prose. Only resolved
    banks are fetched; the budget stops new banks once spent (the rest wait for next month).

    ``date`` is the reference year's end, capped at today: BMG and Safra publish a "2026" table
    during 2026, and a future date misranks by recency. The KB key uses the year, so the cap
    doesn't change the key from run to run."""
    from src.ingest import pilar3

    today = today or dt.date.today()
    robots = robots or Robots()
    started = clock()
    records: dict[str, dict[str, Any]] = {}
    for cnpj, meta in sorted(resources.items(), key=lambda kv: kv[1].get("name") or ""):
        if clock() - started > budget_s:
            print("Info: DASFN Pilar 3 budget spent; remaining banks deferred")
            break
        name = meta.get("name") or ""
        try:
            ents = resolver({"source": "News", "title": name, "institution": name}) or []
        except Exception:  # pragma: no cover
            ents = []
        year = meta.get("year") or 0
        if not ents or year < today.year - MAX_AGE_YEARS:
            continue
        prose, used = bank_prose(meta["tables"], robots=robots, fetch=fetch, sleep=sleep)
        if not prose:
            continue
        sents = pilar3.risk_sentences(prose)
        main = meta["tables"].get("ova") or meta["tables"][used[0]]
        for eid in ents:
            if eid in records:
                continue
            records[eid] = {"entity": eid, "kind": "dasfn", "company": name, "cnpj": cnpj,
                            "subject": f"{DOC_TYPE} — {year}", "year": year,
                            "date": min(f"{year}-12-31", today.isoformat()), "url": main, "tables": used,
                            "sentences": sents, "_prose": prose}
    return records


def fetch_catalog() -> list[dict[str, Any]]:
    from src.ingest import bcb_km1
    return (bcb_km1._get(f"{DASFN}/Recursos?$filter=Api%20eq%20'pilar3'&$format=json&$top=100000")
            .get("value") or [])
