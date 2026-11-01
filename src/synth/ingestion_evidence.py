"""ADR 014 next step 3: an evidence-backed `ingestion_gap` detector.

The coverage-gap triage used to call a question an `ingestion_gap` ("no source ingests this")
from keywords alone. The keyword list went stale as sources landed: certifications, ESG, complaints and
revenue are all ingested now. This module checks the question against what Onça actually holds:

- a catalog of DATA FAMILIES, each with the question cues that ask for it and where it lives
  in feed.json;
- for each family the question asks about, how many entities or rows carry it in the live
  feed, whether the entities the question names are covered, and the status of its sources
  on the "Fontes" roadmap (`source_coverage.ROADMAP`).

The verdict, worst first:
  ingestion_gap — nothing in the feed carries it, and no live source would;
  coverage_gap  — a live source carries the family, but not for the entity asked about,
                  or the source is live and currently holds nothing;
  retrieval_gap — the data is there; the agent didn't use it.

Deterministic, read-only, no model call. It never writes code (AUTO_CODEGEN stays off); the
evidence goes on the gap record and to the operators in "Pontos cegos".
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable


def _norm(text: Any) -> str:
    t = unicodedata.normalize("NFKD", str(text or ""))
    return re.sub(r"\s+", " ", "".join(c for c in t if not unicodedata.combining(c)).lower()).strip()


def _attr(key: str, pred: Callable[[Any], bool] | None = None):
    """Counter + per-entity check over feed.entity_attrs[eid][key]."""
    def ok(v: Any) -> bool:
        return pred(v) if pred else v not in (None, "", [], {}, "unknown")

    def count(feed: dict[str, Any]) -> int:
        return sum(1 for a in (feed.get("entity_attrs") or {}).values() if ok(a.get(key)))

    def has(feed: dict[str, Any], eid: str) -> bool:
        return ok(((feed.get("entity_attrs") or {}).get(eid) or {}).get(key))
    return count, has


def _block(key: str):
    """Counter + per-entity check over a block on feed.entities[*]."""
    def count(feed: dict[str, Any]) -> int:
        return sum(1 for e in (feed.get("entities") or [])
                   if isinstance(e, dict) and e.get(key) not in (None, "", [], {}))

    def has(feed: dict[str, Any], eid: str) -> bool:
        return any(isinstance(e, dict) and e.get("entity") == eid
                   and e.get(key) not in (None, "", [], {}) for e in (feed.get("entities") or []))
    return count, has


def _rows(key: str, *ent_keys: str):
    """Counter + per-entity check over a top-level feed list."""
    ent_keys = ent_keys or ("entity",)

    def count(feed: dict[str, Any]) -> int:
        return len(feed.get(key) or [])

    def has(feed: dict[str, Any], eid: str) -> bool:
        for r in feed.get(key) or []:
            if not isinstance(r, dict):
                continue
            if any(r.get(k) == eid for k in ent_keys) or eid in (r.get("entities") or []):
                return True
        return False
    return count, has


def _none():
    return (lambda feed: 0), (lambda feed, eid: False)


_THIRD_PARTY = re.compile(r"\b(iso|pci|soc)\b", re.I)


def _third_party(certs: Any) -> bool:
    for c in certs or []:
        label = c if isinstance(c, str) else (c or {}).get("label") or ""
        if _THIRD_PARTY.search(label):
            return True
    return False


@dataclass(frozen=True)
class Family:
    id: str
    label: str
    cues: str                       # regex over the accent-folded, lower-cased question
    counter: tuple[Callable[..., int], Callable[..., bool]]
    sources: tuple[str, ...] = ()   # source_coverage.ROADMAP ids
    note: str = ""
    excludes: str | None = None     # a question matching this is NOT about the family

    def matches(self, nq: str) -> bool:
        if self.excludes and re.search(self.excludes, nq):
            return False
        return bool(re.search(self.cues, nq))


#: Order matters only for display. A question can match several families.
FAMILIES: tuple[Family, ...] = (
    Family("third_party_cert", "Certificações de terceiros (ISO, PCI, SOC)",
           r"\b(iso|pci|soc ?2?|27001|9001|20000|selo)\b", _attr("certifications", _third_party),
           note="o campo certificações guarda autorizações regulatórias (BCB/CVM/SUSEP/B3), não "
                "ISO/PCI/SOC; nenhuma fonte ingere certificações de terceiros"),
    Family("authorization", "Autorizações regulatórias (BCB, CVM, SUSEP, B3)",
           r"autoriza|licenc|credenciad|em funcionamento|certificac|certificad",
           _attr("certifications"), sources=("new_entrants",)),
    Family("esg", "ESG (carteira ISE B3)", r"\besg\b|sustentab|\bise\b|ambiental|carbono|climat",
           _attr("esg", lambda v: bool((v or {}).get("ise_b3"))),
           note="só a participação na carteira ISE B3, que exige listagem; ratings ESG de agência "
                "são proprietários"),
    Family("complaints", "Reclamações e reputação (ranking BCB, Reclame Aqui)",
           r"reclama|ouvidoria|procon|reputac", _rows("reputation"), sources=("consumidor_gov",),
           note="consumidor.gov.br segue sem credencial válida"),
    Family("revenue", "Receita e lucro (CVM DFP/ITR)",
           r"receita|faturamento|lucro|prejuizo|resultado|balanco|demonstrac|\bdre\b",
           _rows("financials", "entity_id"), sources=("cvm_financials",)),
    Family("basel", "Solvência (Índice de Basileia)", r"basileia|solvencia|capital regulat|indice de capital",
           _block("soundness"), sources=("soundness",)),
    Family("npl", "Inadimplência (15+ dias)", r"inadimpl|calote|atraso", _block("inadimplencia"),
           sources=("inadimplencia",)),
    Family("liquidity", "Liquidez (LCR/NSFR, Pilar 3)", r"liquidez|\blcr\b|\bnsfr\b", _block("pilar3_km1"),
           sources=("pilar3_km1",)),
    Family("market_share", "Participação de mercado (IF.data)",
           r"participacao de mercado|market share|fatia de mercado", _block("market_share_pct"),
           sources=("market_share",)),
    Family("profitability", "Rentabilidade (ROE/ROA, alavancagem)", r"\broe\b|\broa\b|rentabil|alavancag",
           _block("fundamentals"), sources=("fundamentals",)),
    Family("sanctions", "Sanções e processos (CEIS/CNEP, CADE, CVM)",
           r"sanc|\bceis\b|\bcnep\b|\bmulta|punic|processo administrativo|penalidad",
           _rows("enforcement", "entity", "target"), sources=("sanctions", "cade")),
    Family("distress", "Recuperação judicial e falência", r"recuperacao (extra)?judicial|falencia|falid",
           _rows("distress"), sources=("news", "fatos")),
    Family("headcount", "Número de funcionários", r"funcionari|empregad|headcount|colaborador|demiss",
           _none(), note="nenhuma fonte ingere quadro de pessoal; o porte (size_band) é inferência"),
    Family("credit_rating", "Rating de crédito de agência",
           r"\brating\b|nota de credito|moody|fitch|standard ?& ?poor|\bs ?& ?p\b",
           _none(), note="ratings de agência são proprietários; nenhuma fonte os ingere",
           excludes=r"\besg\b|sustentab"),   # "rating ESG" asks for the ESG family
    Family("social", "Redes sociais (X, LinkedIn)", r"linkedin|twitter|redes sociais|instagram",
           _none(), sources=("x_twitter",), note="ADR 008: X depende de API paga"),
)

_SEVERITY = {"ingestion_gap": 3, "coverage_gap": 2, "retrieval_gap": 1}


def _source_rows() -> dict[str, dict[str, Any]]:
    from src.synth import source_coverage
    return {r["id"]: r for r in source_coverage.ROADMAP}


def _family_verdict(fam: Family, feed: dict[str, Any], entities: list[str],
                    roadmap: dict[str, dict[str, Any]]) -> dict[str, Any]:
    count, has = fam.counter
    n = int(count(feed))
    srcs = [{"id": s, "label": roadmap.get(s, {}).get("label", s),
             "status": roadmap.get(s, {}).get("status", "unknown")} for s in fam.sources]
    live = any(s["status"] == "live" for s in srcs)
    covered = {e: bool(has(feed, e)) for e in entities}
    if n == 0:
        verdict = "coverage_gap" if live else "ingestion_gap"
    elif entities and not all(covered.values()):
        verdict = "coverage_gap"
    else:
        verdict = "retrieval_gap"
    return {"family": fam.id, "label": fam.label, "verdict": verdict, "rows": n,
            "entities": covered, "sources": srcs, "note": fam.note}


def _recommend(ev: dict[str, Any]) -> str:
    v, label, n = ev["verdict"], ev["label"], ev["rows"]
    gated = [s["label"] + f" ({s['status']})" for s in ev["sources"] if s["status"] != "live"]
    note = f" Observação: {ev['note']}." if ev.get("note") else ""
    if v == "retrieval_gap":
        return (f"{label}: o dado existe ({n} registros no feed) — o agente não o usou; revisar "
                f"o grounding dessa família.{note}")
    if v == "coverage_gap":
        missing = [e for e, ok in ev["entities"].items() if not ok]
        who = f" sem cobertura para {', '.join(missing)}" if missing else " sem registros no momento"
        return (f"{label}: a fonte está ativa, mas{who} ({n} registros no feed) — estender a "
                f"cobertura da fonte existente.{note}")
    tail = f" Fontes avaliadas: {', '.join(gated)}." if gated else ""
    return (f"{label}: nenhuma fonte ativa traz esse dado — requer fonte ou detector novo "
            f"(issue + revisão humana; AUTO_CODEGEN desligado).{tail}{note}")


def assess(question: str, feed: dict[str, Any] | None, entities: list[str] | None = None
           ) -> dict[str, Any] | None:
    """Evidence-backed class for a question, or None when no data family matches it.

    Returns {class, families: [per-family evidence], recommendation}. `class` is the worst
    verdict across the families the question asks about."""
    if not feed:
        return None
    nq = _norm(question)
    fams = [f for f in FAMILIES if f.matches(nq)]
    if not fams:
        return None
    roadmap = _source_rows()
    evs = [_family_verdict(f, feed, list(entities or []), roadmap) for f in fams]
    worst = max(evs, key=lambda e: _SEVERITY[e["verdict"]])
    return {"class": worst["verdict"], "families": evs,
            "recommendation": " ".join(_recommend(e) for e in
                                       sorted(evs, key=lambda e: -_SEVERITY[e["verdict"]]))}
