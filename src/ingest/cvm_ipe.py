"""Ingest CVM material facts — the strategic-disclosure signal (IPE dataset).

CVM Dados Abertos publishes IPE (Informações Periódicas e Eventuais): every
eventual disclosure a B3-listed company files. The strategic ones are
**Fato Relevante** and **Comunicado ao Mercado** — a competitor's own official
statement of M&A, product launches, results guidance, leadership. Authoritative,
citable (rad.cvm.gov.br), zero legal risk — the BR-listed counterpart to the
SEC 6-K lens.

Source package: https://dados.cvm.gov.br/dataset/cia_aberta-doc-ipe
ZIP per year:   .../CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip

Columns (live-verified 2026-08-16): CNPJ_Companhia, Nome_Companhia, Codigo_CVM,
Data_Referencia, Categoria, Tipo, Especie, Assunto, Data_Entrega,
Tipo_Apresentacao, Protocolo_Entrega, Versao, Link_Download.

Signal: detect_new on stable protocol ids; first run seeds baseline.
Lambda port note: handler wraps fetch_material_facts(); DynamoDB state for ids.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
import unicodedata
import zipfile
from typing import Any, Iterable

import requests


def _fold(s: Any) -> str:
    """Accent-stripped uppercase, for robust keyword matching on PT subjects."""
    nfkd = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).upper()


# Governance-event cues (ADR narrative-dimensions: the *governance* axis). A
# Fato Relevante / Comunicado carrying one of these in its Assunto/Espécie/Tipo
# is a corporate-governance event on a tracked entity — control changes, board /
# executive moves, shareholder agreements, bylaw/auditor changes. Matched on the
# accent-folded text so "eleição"/"renúncia"/"governança" hit. Anchored at a
# word start to avoid mid-word collisions.
_GOVERNANCE_CUES = re.compile(
    r"(?<![A-Z])(?:"
    r"CONTROLE|ACORDO DE ACIONISTAS|CONSELHO DE ADMINISTRACAO|CONSELHO FISCAL|"
    r"DIRETORIA|DIRETOR|ADMINISTRADOR|ELEICAO|RENUNCIA|DESTITUICAO|RECONDUCAO|"
    r"NOMEACAO|SUBSTITUICAO|AFASTAMENTO|POSSE|GOVERNANCA|ESTATUTO|AUDITOR|"
    r"PRESIDENTE|CEO|CFO"
    r")"
    # "remuneração dos administradores" (exec comp) is governance but collides
    # with "remuneração aos/dos acionistas" (dividends) — omitted to avoid the
    # false positive; control/board/exec/auditor/statute cues cover the rest.
)


def is_governance(subject: Any, especie: Any = None, tipo: Any = None) -> bool:
    """True if a material fact is a corporate-governance event (by its topic)."""
    return bool(_GOVERNANCE_CUES.search(_fold(f"{subject} {especie or ''} {tipo or ''}")))

ZIP_URL_TMPL = (
    "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip"
)

# The strategic categories; other IPE categories (assembleia, proventos, …) are noise.
DEFAULT_CATEGORIES = ("Fato Relevante", "Comunicado ao Mercado")
DEFAULT_LOOKBACK_DAYS = 45


def fetch_material_facts(
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    watchlist: list[str] | None = None,
    categories: Iterable[str] | None = None,
    *,
    today: dt.date | None = None,
    fetcher: Any | None = None,
) -> list[dict[str, Any]]:
    """Return material facts filed within lookback_days, filtered by watchlist.

    watchlist: case-insensitive substrings matched against Nome_Companhia.
    Empty/None = keep all (noisy). fetcher(year) -> zip bytes (for tests).
    """
    today = today or dt.date.today()
    cutoff = today - dt.timedelta(days=lookback_days)
    watch = [w.upper() for w in (watchlist or []) if w]
    cats = {c.lower() for c in (categories or DEFAULT_CATEGORIES)}
    fetch = fetcher or _fetch_year_zip

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Current year, plus previous when the window crosses the year boundary.
    for year in sorted({cutoff.year, today.year}):
        content = fetch(year)
        if not content:
            continue
        for rec in _iter_zip(content):
            if (rec.get("category") or "").lower() not in cats:
                continue
            date = _parse_date(rec.get("date"))
            if not date or date < cutoff:
                continue
            if watch and not any(w in (rec.get("company") or "").upper() for w in watch):
                continue
            if rec["id"] in seen:
                continue
            seen.add(rec["id"])
            out.append(rec)

    out.sort(key=lambda r: r.get("date") or "", reverse=True)
    return out


def _fetch_year_zip(year: int) -> bytes | None:
    try:
        resp = requests.get(ZIP_URL_TMPL.format(year=year), timeout=180)
        if resp.status_code != 200:
            return None
        return resp.content
    except Exception as exc:  # pragma: no cover - upstream best-effort
        print(f"Warning: CVM IPE fetch failed for {year}: {exc}")
        return None


def _iter_zip(content: bytes) -> Iterable[dict[str, Any]]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for name in zf.namelist():
            if not name.lower().endswith(".csv"):
                continue
            with zf.open(name) as fh:
                text = io.TextIOWrapper(fh, encoding="latin-1", newline="")
                for row in csv.DictReader(text, delimiter=";"):
                    yield _normalize(row)


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    """Map an IPE row (live schema) to a signal record."""
    proto = (row.get("Protocolo_Entrega") or "").strip()
    cnpj = _clean_cnpj(row.get("CNPJ_Companhia"))
    company = (row.get("Nome_Companhia") or "").strip()
    date = (row.get("Data_Entrega") or "")[:10]
    category = (row.get("Categoria") or "").strip()
    subject = (row.get("Assunto") or "").strip() or (row.get("Tipo") or "").strip() or category
    ident = proto or f"{cnpj}:{date}:{subject}"[:80]
    especie = (row.get("Especie") or "").strip() or None
    tipo = (row.get("Tipo") or "").strip() or None
    governance = is_governance(subject, especie, tipo)
    return {
        "id": f"cvm-fato:{ident}",
        "source": "CVM-FatoRelevante",
        "kind": "competitor",
        "doc_type": category,
        "category": category,
        # Governance axis: recognised control/board/executive/statute/auditor
        # events, so a strategic leadership move isn't lost among routine facts.
        "governance": governance,
        "topic": "governance" if governance else None,
        "subject": subject,
        "company": company,
        "name": company,
        "cnpj": cnpj,
        "especie": especie,
        "date": date,
        "event_date": date,
        "reference_date": (row.get("Data_Referencia") or "")[:10] or None,
        "protocol": proto or None,
        "url": (row.get("Link_Download") or "").strip()
        or "https://dados.cvm.gov.br/dataset/cia_aberta-doc-ipe",
    }


def _clean_cnpj(value: Any) -> str | None:
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits or None


def _parse_date(value: Any) -> dt.date | None:
    s = (str(value) if value is not None else "").strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            return dt.date.fromisoformat(s[:10])
        except ValueError:
            return None
    return None


def inspect(lookback_days: int = 30) -> None:
    """One-shot: fetch recent material facts and print a sample."""
    facts = fetch_material_facts(lookback_days=lookback_days)
    print(f"fetch_material_facts({lookback_days}d): {len(facts)} facts")
    for r in facts[:15]:
        print(f"  [{r['date']}] {r['category'][:20]:20} {(r['company'] or '?')[:30]:30} {r['subject'][:50]}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "inspect":
        inspect(int(sys.argv[2]) if len(sys.argv) > 2 else 30)
    else:
        days = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LOOKBACK_DAYS
        watch = sys.argv[2:] if len(sys.argv) > 2 else None
        rows = fetch_material_facts(lookback_days=days, watchlist=watch)
        print(f"{len(rows)} material facts in last {days}d"
              + (f" matching {watch}" if watch else ""))
        for r in rows[:25]:
            print(f"  [{r['date']}] {r['category'][:18]:18} {(r['company'] or '?')[:30]:30} {r['subject'][:46]}")
