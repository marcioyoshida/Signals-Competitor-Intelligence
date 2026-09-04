"""Entity discovery & enrichment pipeline (ADR 011 / issue #14).

Closes the "unknown unknowns" gap: companies/funds that recur in news or in
regulator registries but are not yet in the entities registry stay invisible
to synthesis. This module is the producer that *finds*, *enriches*, and
*proposes* — precision-first, never silent pollution of the registry.

Verticals (first cut):
  1. **Structured official registry sync** — CVM FIAGRO Informe Mensal
     (and later FII, BCB lists, CVM cias abertas). CNPJ-keyed → strong
     identity → eligible for auto-add under the industry module.
  2. **Keyword / industry harvest from news** — scan recent free-text for a
     keyword (e.g. "FIAGRO") and associated fund names / B3 tickers that do
     not resolve; queue as discovery candidates with evidence.

Promotion policy (mirrors ADR 011 §4):
  - Strong structured identity (CNPJ from CVM/BCB filing, or B3 ticker matched
    to CVM registry) → ``auto_create`` at confidence=cnpj (or enrich existing).
  - News-only brand → ``propose_review(kind="discovery")`` for analyst vetting.
  - Never hijack a name already owned by another entity.

Usage:
  from src.synth.entity_discovery import discover_fiagro, harvest_keyword
  report = discover_fiagro()          # structured CVM path
  cands  = harvest_keyword("FIAGRO")  # news path (needs recent news items)

Wired as a best-effort pass in the ingest Lambda (gated by
``ONCA_ENTITY_DISCOVERY``, default off until validated) or a weekly schedule.
"""
from __future__ import annotations

import os
import re
import unicodedata
from typing import Any, Iterable

from src.synth import entity_registry
from src.synth.entities import detect_b3_tickers

# Industry keyword → registry industry slug (must exist in INDUSTRIES).
INDUSTRY_KEYWORDS: dict[str, str] = {
    "FIAGRO": "agri-funds",
    "FIAGROS": "agri-funds",
    "FII": "real-estate-funds",
    "FIIS": "real-estate-funds",
    "FUNDO IMOBILIÁRIO": "real-estate-funds",
    "FUNDOS IMOBILIÁRIOS": "real-estate-funds",
}

# Stop words / generic tokens that should never become entity brands alone.
_STOP = frozenset(
    {
        "o", "a", "os", "as", "de", "da", "do", "das", "dos", "e", "em", "no", "na",
        "nos", "nas", "um", "uma", "para", "com", "por", "ao", "à", "aos", "às",
        "que", "se", "su", "seu", "sua", "seus", "suas", "este", "esta", "isto",
        "the", "of", "and", "or", "in", "on", "at", "to", "for", "by", "from",
        "fiagro", "fiagros", "fii", "fiis", "fundo", "fundos", "classe", "classes",
        "cota", "cotas", "imobiliário", "imobiliários", "imobiliaria", "imobiliarias",
        "agro", "crédito", "credito", "rural", "agrícola", "agricola", "imob",
        "ltda", "sa", "s.a.", "s/a", "me", "epp", "eireli", "inc", "corp",
        # consórcio legal-form words (issue #46) — so a razão social reduces to its brand.
        "administradora", "administradoras", "adm", "consórcio", "consorcio",
        "consórcios", "consorcios",
    }
)

# ADR 017 sub-entity linking, shared by discover_fiagro/discover_consorcio: a fund or
# consórcio line links to the tier-1 INSTITUTION its brand belongs to (`BRADESCO ADM DE
# CONSÓRCIOS` -> `bradesco`). A parentable institution has a HIGHER-TIER industry — an
# entry-tier/leaf player (a fund, a consórcio administrator) is a sub-entity, never a
# parent, so those industries are excluded from parentability.
_LEAF_INDUSTRIES = frozenset(
    {"agri-funds", "real-estate-funds", "consorcio", "betting", "crypto"}
)


def _norm_brand(s: str | None) -> str:
    return entity_registry.normalize_alias(s or "")


def _build_parent_brand_idx(entities: list[dict[str, Any]]) -> dict[str, str]:
    """Normalized brand/alias/entity-id -> institution id. Two passes so an exact key
    (entity_id or a full alias) always beats a looser brand token."""
    parentable = [
        (e.get("entity_id"), {a for a in (e.get("aliases") or []) if a})
        for e in entities
        if e.get("entity_id") and set(e.get("industries") or []) - _LEAF_INDUSTRIES
    ]
    idx: dict[str, str] = {}
    for eid, aliases in parentable:  # pass 1 — exact keys (any length; `bb` is valid)
        idx.setdefault(_norm_brand(eid), eid)
        for a in aliases:
            idx.setdefault(a, eid)
    for eid, _ in parentable:  # pass 2 — brand token, ≥3 chars to avoid generic 2-letter
        tok = _norm_brand(eid.split("_")[0])  # collisions like "BR" -> br_partners
        if tok and tok != _norm_brand(eid) and len(tok) >= 3:
            idx.setdefault(tok, eid)
    return idx


def _resolve_parent(parent_brand_idx: dict[str, str], brand: str | None) -> str | None:
    """The institution a sub-entity's brand belongs to — full brand then leading token."""
    nb = _norm_brand(brand)
    if not nb:
        return None
    return parent_brand_idx.get(nb) or parent_brand_idx.get(nb.split(" ")[0])


# Generic / legal-form / fund-structure words that must not stand in for a brand.
# A fund name reduced to only these has no distinctive brand → propose, don't
# auto-create (keeps "INVESTIMENTO", "RESP LIMITADA" etc. out of the registry).
_GENERIC = frozenset(
    {
        "investimento", "investimentos", "responsabilidade", "limitada", "resp",
        "multimercado", "multiestrategia", "multiestratégia", "renda", "fixa",
        "variavel", "variável", "direitos", "creditorios", "creditórios",
        "recebiveis", "recebíveis", "participacoes", "participações", "cadeias",
        "produtivas", "agroindustriais", "agroindustrial", "desenvolvimento",
        "infraestrutura", "incentivado", "referenciado", "aberto", "fechado",
        "condominio", "condomínio", "profissionais", "qualificados", "geral",
        "especial", "master", "feeder", "cotas", "responsabilidad",
    }
)


# Generic corporate/industry words that are never a brand on their own (applied
# only to single-token candidates; multi-word names may legitimately contain them).
_SINGLE_STOP = frozenset(
    {
        "banco", "bank", "digital", "fundo", "fundos", "grupo", "group", "classe",
        "asset", "capital", "holding", "financeira", "financeiro", "seguros",
        "seguradora", "seguro", "corretora", "gestora", "company", "companhia",
        "cia", "investimentos", "credito", "pagamentos", "consorcio", "cooperativa",
        "fintech", "bolsa", "mercado", "conta", "cartao", "cartoes",
    }
)

# Broad B3 ticker: 4-letter root + 1–2 digits. Funds trade as XXXX11, equities as
# XXXX3/XXXX4 (ON/PN), units as XXXX11 — so this spans funds AND ordinary equities
# (ITUB4, BBDC3, MXRF11, KNCA11), unlike a fund-only ``XXXX11``.
_TICKER_RE = re.compile(r"\b([A-Z]{4}\d{1,2})\b")
# Multi-word proper-name brand (≥2 capitalized tokens): "Porto Seguro", "C6 Bank".
_BRAND_MULTI_RE = re.compile(
    r"\b([A-Z0-9ÁÉÍÓÚÂÊÔÃÕÇ][\wÁÉÍÓÚÂÊÔÃÕÇáéíóúâêôãõç]{1,20}"
    r"(?:\s+[A-Z0-9ÁÉÍÓÚÂÊÔÃÕÇ][\wÁÉÍÓÚÂÊÔÃÕÇáéíóúâêôãõç]{1,20}){1,3})\b"
)
# Single-token proper name (Initial-cap then lowercase): "Neon", "Nubank", "Itaú".
# The required lowercase tail excludes ALL-CAPS tickers/acronyms (handled above).
_BRAND_SINGLE_RE = re.compile(
    r"\b([A-ZÁÉÍÓÚÂÊÔÃÕÇ][a-záéíóúâêôãõç][\wáéíóúâêôãõç]{1,19})\b"
)

# --- #14 general NER harvest: company candidates anchored on company CUES ------------
# A proper-name span next to a legal suffix / sector word / typed article ("a fintech X")
# is a company mention — industry-agnostic, so a genuinely new player surfaces from ANY
# narrative, not just near a seeded keyword.
_NER_BRAND = (r"[A-Z0-9ÁÉÍÓÚÂÊÔÃÕÇ][\wÁÉÍÓÚÂÊÔÃÕÇáéíóúâêôãõç&]{1,20}"
              r"(?:\s+[A-Z0-9ÁÉÍÓÚÂÊÔÃÕÇ][\wÁÉÍÓÚÂÊÔÃÕÇáéíóúâêôãõç&]{1,20}){0,3}")
_NER_SECTOR_WORD = (r"S\.?\s?A\.?|Ltda\.?|Seguradora|Seguros|Resseguradora|Securitizadora|"
                    r"Previd[êe]ncia|Capital|Asset|Gest[ãa]o|Gestora|Administradora|"
                    r"Pagamentos|Cons[óo]rcio|Financeira|Corretora|Distribuidora|Holding|"
                    r"Participa[çc][õo]es|Fintech|Cooperativa")
_NER_SUFFIX_RE = re.compile(rf"\b({_NER_BRAND})\s+(?:{_NER_SECTOR_WORD})\b")
_NER_PREFIX_RE = re.compile(
    rf"\b(?:Banco|Funda[çc][ãa]o|Cooperativa|Seguradora|Securitizadora|Gestora)\s+({_NER_BRAND})")
# The article + type word are case-insensitive (scoped `(?i:…)`) but the BRAND capture is
# case-SENSITIVE, so it stops at the first non-capitalized word ("a fintech Zignet levantou"
# → "Zignet", not "Zignet levantou uma rodada").
_NER_TYPED_RE = re.compile(
    r"\b(?i:[ao]s?)\s+(?i:fintech|gestora|seguradora|resseguradora|securitizadora|corretora|"
    r"administradora|financeira|adquirente|cooperativa|distribuidora|banco\s+digital|"
    r"previd[êe]ncia)\s+"
    rf"({_NER_BRAND})")
# Generic heads / regulator words / legal-&-economic type fragments that are never a
# standalone company candidate. A candidate must have ≥1 token outside this set.
_NER_STOP = frozenset({
    "banco", "fundo", "fundacao", "grupo", "companhia", "central", "federal", "nacional",
    "brasil", "conselho", "superintendencia", "comissao", "ministerio", "governo", "uniao",
    "diario", "oficial", "resolucao", "circular", "instrucao", "the", "de", "da", "do",
    "e", "seguros", "seguradora", "capital", "gestao", "gestora", "pagamentos",
    # legal-type + economic-indicator fragments that recur in reg/entrant narratives
    "multiplo", "comercial", "investimento", "cambio", "credito", "financiamento",
    "arrendamento", "cooperativo", "cooperativa", "taxa", "basica", "referencial", "selic",
    "financeira", "consorcio", "administradora", "corretora", "distribuidora", "holding",
})


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", str(s or ""))
        if unicodedata.category(c) != "Mn"
    )


def _norm_txt(s: str) -> str:
    return _strip_accents(s).upper()


def _kw_regex(keyword_forms: Iterable[str]) -> "re.Pattern[str] | None":
    """Accent- and plural-tolerant matcher for one or more keyword phrases.

    Each phrase becomes a word sequence where every word may take an optional
    trailing S — so "FUNDO IMOBILIARIO" matches "fundos imobiliarios" too — and is
    matched against accent-stripped, upper-cased text. This is what lets one
    keyword span the singular/plural/accented variants real news actually uses.
    """
    pats: list[str] = []
    for form in keyword_forms:
        words = [w for w in re.split(r"\s+", _norm_txt(form)) if w]
        if words:
            pats.append(r"\s+".join(re.escape(w) + r"S?" for w in words))
    return re.compile("(?:" + "|".join(pats) + ")") if pats else None


def _root8(cnpj: str | None) -> str:
    d = "".join(ch for ch in str(cnpj or "") if ch.isdigit())
    return d[:8] if len(d) >= 8 else ""


def _slug(s: str) -> str:
    s = re.sub(r"[^\w\u00c0-\u024f]+", "-", (s or "").lower()).strip("-")
    return s[:48] if s else ""


def _brand_from_name(name: str) -> str | None:
    """Extract a short brand-like token sequence from a long fund legal name.

    Prefer the distinctive proper-name span before generic suffixes like
    FIAGRO / FII / FUNDO / CLASSE. Falls back to the first 3–4 non-stop tokens.
    """
    if not name:
        return None
    clean = re.sub(r"[\s\u00a0]+", " ", str(name)).strip()
    # Drop common trailing noise.
    clean = re.sub(
        r"\s+(FIAGRO[- ]?I?MO?BILI[AÁ]RIO|FIAGRO|FII|FUNDO DE INVESTIMENTO.*)$",
        "",
        clean,
        flags=re.I,
    )
    tokens = [t for t in re.split(r"[^\w\u00c0-\u024f]+", clean) if t]
    keep: list[str] = []
    for t in tokens:
        low = t.lower()
        if len(t) == 1:  # single-letter fragment ("P P") — never a brand token
            continue
        if low in _STOP or low in _GENERIC:
            if keep:  # a distinctive span already started — stop extending it
                break
            continue  # leading generic/stop word — skip and keep looking
        keep.append(t)
        if len(keep) >= 4:
            break
    return " ".join(keep) if keep else None


def _profile_from_fiagro(row: dict[str, Any]) -> dict[str, Any]:
    """Compose a registry-ready profile from a CVM FIAGRO informe row."""
    name = str(row.get("fund_name") or "").strip()
    ticker = (row.get("ticker") or "").strip().upper() or None
    cnpj = row.get("cnpj") or ""
    root = _root8(cnpj)
    forms: list[str] = []
    if name and len(name) >= 4:
        forms.append(name)
    if ticker:
        forms.append(ticker)
        forms.append(f"TICKER:{ticker}")
    # Short distinctive brand: first 2–3 meaningful tokens of the name.
    brand = _brand_from_name(name)
    if brand and brand.upper() not in {f.upper() for f in forms}:
        forms.append(brand)
    # Prefer a clean brand, then the ticker; never the raw messy legal name as the
    # display. ``auto_ok`` gates auto-create: a fund with neither a ticker nor a
    # distinctive brand has no clean identity → route to review, don't auto-create.
    auto_ok = bool(ticker or brand)
    display = brand or ticker or f"FIAGRO {root or 'sem-cnpj'}"
    entity_id = _slug(ticker or brand) or f"fiagro-{root or 'unknown'}"
    admin = (row.get("admin") or "").strip() or None
    manager = (row.get("manager") or "").strip() or None
    # NB: admin/manager are the fund's SERVICER (administrator/gestor), not its
    # identity — and are shared across dozens of funds. They must NOT enter the
    # name index (aliases/alias_forms): resolve_entities substring-matches those,
    # so a shared servicer name would fan one fund's signal out to every fund it
    # services. Keep them only as descriptive profile fields (below).
    # Dedupe preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for f in forms:
        k = f.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return {
        "entity_id": entity_id,
        "display_name": display,
        "auto_ok": auto_ok,
        "aliases": uniq,
        "cnpj_roots": [root] if root else [],
        "industries": ["agri-funds"],
        "ticker": ticker,
        "isin": (row.get("isin") or None),
        "admin": admin,
        "gestor": manager,
        "manager": manager,
        "pl": row.get("pl"),
        "source": "cvm_fiagro",
        "confidence": "cnpj",
        "raw_name": name,
        "as_of": row.get("as_of"),
        "url": row.get("url"),
    }


def _profile_from_consorcio(row: dict[str, Any]) -> dict[str, Any]:
    """Compose a registry-ready profile from a BCB consórcio administrator row."""
    name = str(row.get("name") or "").strip()
    root = _root8(row.get("cnpj"))
    brand = _brand_from_name(name)
    forms: list[str] = []
    if name and len(name) >= 4:
        forms.append(name)
    if brand and brand.upper() not in {f.upper() for f in forms}:
        forms.append(brand)
    return {
        "entity_id": _slug(brand) or f"consorcio-{root or 'unknown'}",
        "display_name": brand or f"Consórcio {root or 'sem-cnpj'}",
        "auto_ok": bool(brand),  # a distinctive brand is required to auto-create
        "aliases": forms,
        "cnpj_roots": [root] if root else [],
        "industries": ["consorcio"],
        "source": "bcb_consorcio",
        "confidence": "cnpj",
        "raw_name": name,
        "as_of": row.get("as_of"),
        "url": row.get("url"),
    }


def discover_consorcio(
    *,
    min_branches: int = 1,
    max_new: int = 40,
    auto_create: bool = True,
    table: Any | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Sync the BCB consórcio administradoras into the registry (issue #46, ADR 017).

    Resolve each administrator by CNPJ root: hit → enrich (industry `consorcio` +
    tier-1 `parent` if its brand is a conglomerate's); miss → auto-create (clean brand)
    or propose_review. Mirrors discover_fiagro; parent-linking uses the shared resolver
    so "BRADESCO ADMINISTRADORA DE CONSÓRCIOS" nests under `bradesco`.
    """
    if rows is None:
        from src.ingest import bcb_consorcio

        rows = bcb_consorcio.fetch_consorcio()
    rows = [r for r in rows if int(r.get("branches") or 0) >= min_branches]
    report: dict[str, Any] = {
        "fetched": len(rows), "created": [], "enriched": [], "already": 0,
        "proposed": [], "skipped": [], "errors": [],
    }
    if not rows:
        return report

    entities = list(entity_registry.list_entities(table=table, include_inactive=True))
    cnpj_idx: dict[str, str] = {}
    disp_idx: dict[str, list[str]] = {}
    for e in entities:
        eid = e.get("entity_id")
        if not eid:
            continue
        for r in e.get("cnpj_roots") or []:
            cnpj_idx[str(r)[:8]] = eid
        d = _norm_brand(e.get("display_name") or "")
        if d:
            disp_idx.setdefault(d, []).append(eid)
    parent_idx = _build_parent_brand_idx(entities)

    new_budget = max_new if auto_create else 0
    for row in rows:
        try:
            profile = _profile_from_consorcio(row)
        except Exception as exc:  # pragma: no cover
            report["errors"].append({"row": row.get("cnpj"), "error": str(exc)})
            continue
        root = (profile.get("cnpj_roots") or [None])[0]
        parent = _resolve_parent(parent_idx, profile.get("display_name"))
        # A conglomerate's consórcio arm shares the parent's brand, so the naive slug
        # (`bradesco`) would COLLIDE with — and overwrite — the parent. Give the arm a
        # distinct sub-entity id + label and nest it under the parent (ADR 017).
        if parent and profile["entity_id"] == parent:
            profile["entity_id"] = f"{parent}-consorcio"
            profile["display_name"] = f"{profile['display_name']} Consórcio"
        eid = cnpj_idx.get(root) if root else None

        if eid:  # already tracked → enrich (industry + parent), don't duplicate
            try:
                changed = entity_registry.accumulate_aliases(
                    eid, profile.get("aliases") or [], table=table)
                ent = entity_registry.get_entity(eid, table=table) or {}
                if "consorcio" not in (ent.get("industries") or []):
                    entity_registry.set_industries(
                        eid, list(ent.get("industries") or []) + ["consorcio"],
                        source="enrich", table=table)
                    changed = True
                if parent and parent != eid and not ent.get("parent"):
                    changed = entity_registry.set_parent(eid, parent, source="enrich", table=table) or changed
                if changed:
                    report["enriched"].append(eid)
                else:
                    report["already"] += 1
            except Exception as exc:  # pragma: no cover
                report["errors"].append({"eid": eid, "error": str(exc)})
            continue

        if auto_create and new_budget > 0 and root and profile.get("auto_ok"):
            nb = _norm_brand(profile["display_name"])
            owners = {x for x in (disp_idx.get(nb) or []) if x != profile["entity_id"]}
            # A brand that collides ONLY with the resolved tier-1 parent is the expected
            # sub-entity case ("PORTO SEGURO" consórcio under porto_seguro) — nest it with
            # a distinct id + label instead of proposing. Propose only on a FOREIGN collision.
            if parent and owners and owners <= {parent}:
                profile["entity_id"] = f"{parent}-consorcio"
                profile["display_name"] = f"{profile['display_name']} Consórcio"
                nb = _norm_brand(profile["display_name"])
                owners = set()
            if owners:  # brand collides with a different tracked entity → review, don't merge
                pid = entity_registry.propose_review(
                    kind="discovery", key=profile["entity_id"], proposed=profile["display_name"],
                    reason="name_collision", hint=f"bcb_consorcio cnpj={root} owner={sorted(owners)[0]}",
                    confidence="cnpj", payload={"profile": profile, "source": "bcb_consorcio", "cnpj": root},
                    table=table)
                report["proposed"].append(pid or profile["entity_id"])
                continue
            try:
                entity_registry.put_entity(
                    profile["entity_id"], profile["display_name"], profile.get("aliases") or [],
                    cnpj_roots=profile.get("cnpj_roots") or [], industries=["consorcio"],
                    confidence="cnpj", parent=parent,
                    # Identity is the BCB filing, and many administradora brands are generic
                    # words (Globo/Eldorado/Central) → structured-only, no fragile news match.
                    news_search=False, source="discovery", table=table)
                report["created"].append(profile["entity_id"])
                new_budget -= 1
                cnpj_idx[root] = profile["entity_id"]
                if nb:
                    disp_idx.setdefault(nb, []).append(profile["entity_id"])
            except Exception as exc:  # pragma: no cover
                report["errors"].append({"profile": profile.get("entity_id"), "error": str(exc)})
        else:
            report["skipped"].append(profile.get("entity_id"))
    return report


# --- #14 Official Registry Sync: BCB-authorized institutions -----------------
# license_class -> (industry, arm_suffix). ONLY these classes are promoted; the long
# tail is deliberately excluded so a sync can never flood the registry:
#   - Cooperativa (thousands of singular credit unions) — gated behind include_coops;
#   - Consórcio — owned by discover_consorcio (#46);
#   - Leasing / Agência de Fomento / Companhia Hipotecária — marginal, skipped.
# arm_suffix nests a conglomerate's NON-bank arm as a sub-entity (ADR 017) when its brand
# collides with the tier-1 parent (e.g. "ITAÚ UNIBANCO ... PAGAMENTOS" -> itau-pagamentos).
_BCB_CLASS_MAP: dict[str, tuple[str, str | None]] = {
    "Banco": ("banking", None),
    "Instituição de Pagamento": ("fintech", "pagamentos"),
    "Crédito Direto (SCD)": ("fintech", "scd"),
    "Empréstimo P2P (SEP)": ("fintech", "sep"),
    "Financeira (SCFI)": ("fintech", "financeira"),
    "Microcrédito (SCMEPP)": ("fintech", "microcredito"),
    "Corretora/DTVM": ("investment-banking", "corretora"),
}
_BCB_COOP_CLASS = "Cooperativa"

# #67 prominence gate — the micro-lender tail (hundreds of tiny SCD/SEP/SCFI/microcrédito
# fintechs, e.g. ffcred/rapidium/conpay) is COMPETITIVELY thin per-entity and best human-
# curated, so by default it is PROPOSED (review queue), not auto-created. The competitively
# central segments (banks, payment institutions, corretoras/DTVM) still auto-create. Tunable
# per run / via env so the digital-lender frontier can be opened when wanted.
_BCB_PROPOSE_ONLY = frozenset({
    "Crédito Direto (SCD)", "Empréstimo P2P (SEP)",
    "Financeira (SCFI)", "Microcrédito (SCMEPP)",
})

# #67 relevance filter — real BCB licensees that are NOT FS competitors a war-room tracks:
# captive manufacturer/equipment finance arms + wholesale clearing/custody vehicles. These
# are FILTERED (not created, not proposed) so they can't pollute the `banking` roster.
_NONCOMPETITOR_TOKENS = frozenset({
    "deere", "honda", "volvo", "hyundai", "traton", "xcmg", "toyota", "scania",
    "caterpillar", "komatsu", "iveco", "volkswagen", "renault", "stellantis", "yamaha",
    "kawasaki", "paccar", "daimler", "nissan", "mitsubishi", "suzuki", "agco", "hino",
    "kia", "mercedes", "bmw", "ford", "cnh", "fiat", "moneo", "randon",
})
_NONCOMPETITOR_PHRASES = (
    "john deere", "new holland", "cnh industrial", "mercedes benz", "general motors",
    "national association", "clearing", "de custodia", "custody", "montadora",
)


def _is_noncompetitor(name: str) -> bool:
    """A captive-finance / wholesale-clearing institution — real license, not a competitor."""
    low = _strip_accents(str(name or "")).lower()
    toks = set(re.split(r"[^a-z0-9]+", low))
    if toks & _NONCOMPETITOR_TOKENS:
        return True
    return any(p in low for p in _NONCOMPETITOR_PHRASES)


def _interleave_by_class(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Round-robin rows across license classes so a single create budget is shared #68
    (banks are the first OLINDA resource and used to eat the whole budget)."""
    from collections import OrderedDict, deque

    buckets: "OrderedDict[str, deque]" = OrderedDict()
    for r in rows:
        buckets.setdefault(str(r.get("license_class") or ""), deque()).append(r)
    out: list[dict[str, Any]] = []
    while any(buckets.values()):
        for q in buckets.values():
            if q:
                out.append(q.popleft())
    return out


# Leading org words + the legal/type tail that carry no brand — a bank's DISTINCTIVE
# name is what's left after "BANCO …" is stripped off the front and the license/legal
# boilerplate off the back. Tokens here CUT the brand span; a bare generic result is
# rejected (routed to review, not auto-created) so we never mint "banco"/"bank".
_INST_LEADING = {"banco", "caixa", "cooperativo"}
# `banco`/`bank` also CUT the span (embedded/trailing "… BANCO", #69: bny-mellon-banco).
_INST_CUT = {
    "banco", "bank", "sa", "s", "a", "ltda", "epp", "me", "multiplo", "comercial",
    "investimento", "cctvm", "ctvm", "dtvm", "cvc", "cvmc", "corretora", "distribuidora",
    "instituicao", "sociedade", "financeira", "financiamento", "credito", "cambio",
    "national", "international", "brasil", "do", "de", "da", "e", "pagamento", "pagamentos",
    "ip", "arrendamento", "fomento", "hipotecaria", "scd", "sep", "scfi", "scmepp", "holding",
}
# A bare single generic token is not a brand — route it to review, never auto-create.
_INST_GARBAGE = {
    "banco", "bank", "brasil", "caixa", "cooperativo", "social", "industrial", "comercial",
    "central", "nacional", "popular", "regional", "credito", "financeira", "",
}
def _clean_institution_brand(name: str) -> str | None:
    """A distinctive brand from a BCB institution legal name — accent-free, generic
    prefix/suffix stripped. Returns None for a bare-generic name (→ propose, not create)."""
    toks = [t for t in re.split(r"[^a-z0-9]+", _strip_accents(str(name or "")).lower()) if t]
    while toks and (toks[0] in _INST_LEADING or len(toks[0]) == 1):
        toks = toks[1:]                       # drop leading org words + initials ("J SAFRA")
    brand: list[str] = []
    for t in toks:
        if t in _INST_CUT:
            break
        brand.append(t)
        if len(brand) >= 3:
            break
    b = " ".join(brand)
    if not b or b in _INST_GARBAGE or len(b) < 2:
        return None
    return b


def _profile_from_bcb_institution(row: dict[str, Any]) -> dict[str, Any]:
    """Compose a registry-ready profile from a BCB authorized-institution row."""
    name = str(row.get("name") or "").strip()
    root = _root8(row.get("cnpj"))
    lclass = str(row.get("license_class") or "")
    industry, suffix = _BCB_CLASS_MAP.get(lclass, (None, None))
    if not industry and lclass == _BCB_COOP_CLASS:
        industry = "banking"  # only reached when include_coops opens the gate
    brand = _clean_institution_brand(name)
    display = brand.title() if brand else f"Instituição {root or 'sem-cnpj'}"
    forms: list[str] = []
    if name and len(name) >= 4:
        forms.append(name)              # keep the full legal name as a match alias
    if brand and display.upper() not in {f.upper() for f in forms}:
        forms.append(display)
    return {
        "entity_id": _slug(brand) or f"bcb-{root or 'unknown'}",
        "display_name": display,
        "auto_ok": bool(brand),  # a distinctive brand is required to auto-create
        "aliases": forms,
        "cnpj_roots": [root] if root else [],
        "industries": [industry] if industry else [],
        "license_class": lclass,
        "arm_suffix": suffix,
        "source": "bcb_autorizacoes",
        # official BCB registry listing -> radar tier 'registry' (reg_coverage.radar_score)
        "confidence": "structured",
        "raw_name": name,
    }


def discover_bcb_institutions(
    *,
    max_new: int = 40,
    auto_create: bool = True,
    include_coops: bool = False,
    propose_only_classes: "frozenset[str] | None" = None,
    table: Any | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """#14 Official Registry Sync — promote RELEVANT BCB-authorized institutions
    (banks, payment institutions, SCD/SEP/SCFI/microcrédito, corretoras/DTVM) into the
    entity registry, by CNPJ root. Deliberately relevance-gated so it cannot flood the
    registry: cooperativas (thousands of singulars) are excluded unless ``include_coops``,
    consórcios (discover_consorcio) and marginal classes are skipped. Mirrors
    discover_consorcio: hit → enrich (industry + tier-1 parent); miss → auto-create a
    clean brand or propose_review. A conglomerate's non-bank arm nests as a sub-entity.
    """
    if rows is None:
        from src.ingest import bcb_autorizacoes

        rows = bcb_autorizacoes.fetch_authorized()

    def _relevant(r: dict[str, Any]) -> bool:
        lc = str(r.get("license_class") or "")
        return lc in _BCB_CLASS_MAP or (include_coops and lc == _BCB_COOP_CLASS)

    rows = [r for r in rows if _relevant(r)]
    # #67: drop captive-finance / wholesale-clearing licensees BEFORE the budget loop, so
    # they neither create nor consume a slot. #68: round-robin the rest across classes so
    # the create budget is shared (banks no longer eat it all).
    filtered = [r for r in rows if _is_noncompetitor(r.get("name"))]
    rows = _interleave_by_class([r for r in rows if not _is_noncompetitor(r.get("name"))])
    report: dict[str, Any] = {
        "fetched": len(rows), "created": [], "enriched": [], "already": 0,
        "proposed": [], "skipped": [], "errors": [],
        "filtered": [str(r.get("name") or "")[:60] for r in filtered],
    }
    if not rows:
        return report

    entities = list(entity_registry.list_entities(table=table, include_inactive=True))
    cnpj_idx: dict[str, str] = {}
    disp_idx: dict[str, list[str]] = {}
    for e in entities:
        eid = e.get("entity_id")
        if not eid:
            continue
        for r in e.get("cnpj_roots") or []:
            cnpj_idx[str(r)[:8]] = eid
        d = _norm_brand(e.get("display_name") or "")
        if d:
            disp_idx.setdefault(d, []).append(eid)
    parent_idx = _build_parent_brand_idx(entities)

    propose_only = _BCB_PROPOSE_ONLY if propose_only_classes is None else propose_only_classes
    new_budget = max_new if auto_create else 0
    for row in rows:
        try:
            profile = _profile_from_bcb_institution(row)
        except Exception as exc:  # pragma: no cover
            report["errors"].append({"row": row.get("cnpj"), "error": str(exc)})
            continue
        if not profile["industries"]:  # unmapped class slipped through — skip
            report["skipped"].append(profile["entity_id"])
            continue
        root = (profile.get("cnpj_roots") or [None])[0]
        industry = profile["industries"][0]
        suffix = profile.get("arm_suffix")
        parent = _resolve_parent(parent_idx, profile.get("display_name"))
        # A conglomerate's NON-bank arm shares the parent's brand; the naive slug would
        # collide with (and be blocked from overwriting) the parent — nest it as a distinct
        # sub-entity. A bank whose brand IS the parent is the same tier-1 (handled below).
        if parent and profile["entity_id"] == parent and suffix:
            profile["entity_id"] = f"{parent}-{suffix}"
            profile["display_name"] = f"{profile['display_name']} {suffix.title()}"
        eid = cnpj_idx.get(root) if root else None

        if eid:  # already tracked → enrich (industry + parent), never duplicate
            try:
                changed = entity_registry.accumulate_aliases(
                    eid, profile.get("aliases") or [], table=table)
                ent = entity_registry.get_entity(eid, table=table) or {}
                if industry not in (ent.get("industries") or []):
                    entity_registry.set_industries(
                        eid, list(ent.get("industries") or []) + [industry],
                        source="enrich", table=table)
                    changed = True
                if parent and parent != eid and not ent.get("parent"):
                    changed = entity_registry.set_parent(eid, parent, source="enrich", table=table) or changed
                report["enriched"].append(eid) if changed else report.__setitem__("already", report["already"] + 1)
            except Exception as exc:  # pragma: no cover
                report["errors"].append({"eid": eid, "error": str(exc)})
            continue

        # #67 prominence gate: a NEW micro-lender-tail institution is PROPOSED (human-
        # curated), not auto-created — doesn't consume the create budget.
        if (root and profile.get("auto_ok")
                and profile.get("license_class") in propose_only):
            pid = entity_registry.propose_review(
                kind="discovery", key=profile["entity_id"], proposed=profile["display_name"],
                reason="prominence_gate",
                hint=f"bcb {profile.get('license_class')} cnpj={root}",
                confidence="structured",
                payload={"profile": profile, "source": "bcb_autorizacoes", "cnpj": root},
                table=table)
            report["proposed"].append(pid or profile["entity_id"])
            continue

        if auto_create and new_budget > 0 and root and profile.get("auto_ok"):
            nb = _norm_brand(profile["display_name"])
            owners = {x for x in (disp_idx.get(nb) or []) if x != profile["entity_id"]}
            # A NON-bank arm whose brand collides only with the resolved tier-1 parent is
            # the expected sub-entity case ("ITAÚ … PAGAMENTOS" under itau) — nest it with a
            # distinct id + label. A bank sharing a tracked brand is NOT auto-merged: it
            # falls through to propose_review (a human decides same-vs-different institution).
            if parent and owners and owners <= {parent} and suffix:
                profile["entity_id"] = f"{parent}-{suffix}"
                profile["display_name"] = f"{profile['display_name']} {suffix.title()}"
                nb = _norm_brand(profile["display_name"])
                owners = set()
            if owners:  # collides with a DIFFERENT tracked entity → review, don't merge
                pid = entity_registry.propose_review(
                    kind="discovery", key=profile["entity_id"], proposed=profile["display_name"],
                    reason="name_collision", hint=f"bcb_autorizacoes cnpj={root} owner={sorted(owners)[0]}",
                    confidence="structured",
                    payload={"profile": profile, "source": "bcb_autorizacoes", "cnpj": root},
                    table=table)
                report["proposed"].append(pid or profile["entity_id"])
                continue
            try:
                entity_registry.put_entity(
                    profile["entity_id"], profile["display_name"], profile.get("aliases") or [],
                    cnpj_roots=profile.get("cnpj_roots") or [], industries=[industry],
                    confidence="structured", parent=parent,
                    # many institution brands are generic → structured-only, no fragile news match
                    news_search=False, source="discovery", table=table)
                report["created"].append(profile["entity_id"])
                new_budget -= 1
                cnpj_idx[root] = profile["entity_id"]
                if nb:
                    disp_idx.setdefault(nb, []).append(profile["entity_id"])
            except Exception as exc:  # pragma: no cover
                report["errors"].append({"profile": profile.get("entity_id"), "error": str(exc)})
        else:
            report["skipped"].append(profile.get("entity_id"))
    return report


def discover_fiagro(
    *,
    min_pl: float = 50_000_000.0,
    max_new: int = 40,
    auto_create: bool = True,
    industry: str = "agri-funds",
    table: Any | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Sync CVM FIAGRO universe into the entities registry.

    For each class with PL ≥ min_pl:
      - resolve by CNPJ root or ticker alias
      - if hit → enrich (aliases, industries, ticker, admin/gestor)
      - if miss and auto_create → put_entity (strong CNPJ identity)
      - if miss and not auto_create → propose_review

    Returns a report dict with created / enriched / already / proposed / skipped.
    """
    from src.ingest import cvm_fiagro

    if rows is None:
        rows = cvm_fiagro.fetch_fiagro(min_pl=min_pl)
    report: dict[str, Any] = {
        "fetched": len(rows),
        "created": [],
        "enriched": [],
        "already": 0,
        "proposed": [],
        "skipped": [],
        "errors": [],
    }
    if not rows:
        return report

    new_budget = max_new if auto_create else 0

    # One scan up front → local resolution indexes (cnpj / alias / display). This
    # replaces the per-row DB resolves (resolve_by_name scanned the whole table on
    # every miss → ~O(rows) full scans, which the ingest source-budget truncated).
    # Kept correct intra-run by updating the indexes as we create entities.
    cnpj_idx: dict[str, str] = {}
    alias_idx: dict[str, str] = {}
    disp_idx: dict[str, list[str]] = {}
    # ADR 017: index the tracked INSTITUTIONS a discovered fund may be a sub-entity OF —
    # entities with a non-agri industry (bank/IB/asset manager). A fund whose brand matches
    # one links to it as `parent`, so the parent stays tier-1 while the fund carries the
    # agri line. Brand→PARENT is the correct use of the fund-brand==manager-brand signal
    # (the same signal that must NOT drive industry enrichment — see the enrich note above).
    # A parentable INSTITUTION has at least one non-fund industry — a bank/IB/asset
    # manager, never a pure FII/FIAGRO fund (those are the sub-entities, not the parents).
    _FUND_INDUSTRIES = {"agri-funds", "real-estate-funds"}
    _parentable: list[str] = []
    _aliases_of: dict[str, set[str]] = {}
    existing_ids: set[str] = set()

    def _norm(s: str) -> str:
        return entity_registry.normalize_alias(s or "")

    for e in entity_registry.list_entities(table=table, include_inactive=True):
        _eid = e.get("entity_id")
        if not _eid:
            continue
        existing_ids.add(_eid)
        for r in e.get("cnpj_roots") or []:
            cnpj_idx[str(r)[:8]] = _eid
        for a in e.get("aliases") or []:
            alias_idx[a] = _eid
        d = _norm(e.get("display_name") or "")
        if d:
            disp_idx.setdefault(d, []).append(_eid)
        if set(e.get("industries") or []) - _FUND_INDUSTRIES:
            _parentable.append(_eid)
            _aliases_of[_eid] = {a for a in (e.get("aliases") or []) if a}

    # Build the brand→institution index in two passes so an EXACT key (entity_id or a
    # full alias) always beats a looser brand token (`bb` the bank, not `bb_seguridade`).
    # Keys are NORMALIZED (normalize_alias uppercases + strips accents) so they match a
    # fund's normalized brand — the entity_id is a lowercase slug, so normalize it too.
    parent_brand_idx: dict[str, str] = {}
    for _eid in _parentable:  # pass 1 — exact keys (normalized entity_id + aliases)
        parent_brand_idx.setdefault(_norm(_eid), _eid)
        for a in _aliases_of[_eid]:  # registry aliases are already normalized
            parent_brand_idx.setdefault(a, _eid)
    for _eid in _parentable:  # pass 2 — the entity_id's brand token (lower priority)
        tok = _norm(_eid.split("_")[0])
        if tok and tok != _norm(_eid) and len(tok) >= 2:
            parent_brand_idx.setdefault(tok, _eid)

    def _parent_of(brand: str | None) -> str | None:
        """The tier-1 institution a fund's brand belongs to (`BTG PACTUAL ASSET CERES`
        -> `btg`). Matches the full brand, then its leading token, to a tracked
        institution — never to another fund."""
        nb = _norm(brand or "")
        if not nb:
            return None
        return parent_brand_idx.get(nb) or parent_brand_idx.get(nb.split(" ")[0])

    for row in rows:
        try:
            profile = _profile_from_fiagro(row)
        except Exception as exc:  # pragma: no cover
            report["errors"].append({"row": row.get("cnpj"), "error": str(exc)})
            continue

        root = (profile.get("cnpj_roots") or [None])[0]
        ticker = profile.get("ticker")
        eid = None

        if root:
            eid = cnpj_idx.get(root)
        if not eid and ticker:
            eid = alias_idx.get(_norm(ticker))
        # Intentionally NO brand-only resolution: a FIAGRO fund's brand IS its
        # manager's brand ("KINEA CRÉDITO AGRO FIAGRO" -> "KINEA"), so a brand match
        # resolves the fund to its institutional PARENT, not a peer fund. Enriching
        # that parent with `agri-funds` fanned every one of its non-agri cards (a bank's
        # Tesouro Selic fund, its Macro Day) into the FIAGRO view. Requiring a strong
        # identity (CNPJ root or the fund's own ticker) to merge routes brand-colliding
        # funds to the create/propose branch below, where the name-collision guard
        # raises a review instead of silently polluting the manager.

        if eid:
            ent = entity_registry.get_entity(eid, table=table) or {}
            # #52: a FIAGRO fund that resolves (by cnpj/ticker/alias) to an INSTITUTION —
            # any tracked entity with a NON-fund industry — is a mis-resolution, almost
            # always a fund ticker/CNPJ left on the institution by earlier brand-match
            # pollution. NEVER enrich it: that re-adds agri-funds AND re-accumulates the
            # fund's aliases onto the institution, a self-sustaining loop. Fall through to
            # create/propose (where the collision guard raises a review).
            # This resolution-time guard is now BACKED (not replaced) by write-time
            # precedence (ADR 018 Phase 2): set_industries is _may_write-blocked, and
            # accumulate_aliases drops ticker-shaped forms, on a protected institution.
            # Keeping the guard is deliberate defense-in-depth — it also stops the residual
            # name-alias accumulation and ticker-less-institution edge that per-field
            # precedence does not, and keeps the mis-resolution from happening at all.
            if set(ent.get("industries") or []) - _FUND_INDUSTRIES:
                eid = None
        if eid:
            try:
                changed = False
                if entity_registry.accumulate_aliases(
                    eid, profile.get("aliases") or [], table=table
                ):
                    changed = True
                if industry not in (ent.get("industries") or []):
                    # set_industries is the registry's single writer for the field
                    # (rebuilds the record + ALIAS# index consistently).
                    entity_registry.set_industries(
                        eid, list(ent.get("industries") or []) + [industry],
                        source="enrich", table=table
                    )
                    changed = True
                if ticker and not ent.get("ticker"):
                    if entity_registry.assign_ticker(eid, ticker, table=table):
                        changed = True
                # ADR 017: link an existing fund to its tier-1 parent if not already.
                pid = _parent_of(profile.get("display_name"))
                if pid and pid != eid and not ent.get("parent"):
                    if entity_registry.set_parent(eid, pid, source="enrich", table=table):
                        changed = True
                if changed:
                    report["enriched"].append(eid)
                else:
                    report["already"] += 1
            except Exception as exc:  # pragma: no cover
                report["errors"].append({"eid": eid, "error": str(exc)})
            continue

        # Name-quality gate: only funds with a clean identity (ticker or a
        # distinctive brand) are auto-createable. Junk-named funds ("INVESTIMENTO",
        # legal-form-only) fall through to the propose-only branch for curator review.
        if auto_create and new_budget > 0 and root and profile.get("auto_ok"):
            try:
                brand = profile["display_name"]
                nb = _norm(brand)
                # Collision check against the local indexes (display + alias),
                # excluding this fund's own id — same guarantee as name_owned_by_other
                # without another full-table scan.
                owners = {x for x in (disp_idx.get(nb) or []) if x != profile["entity_id"]}
                alias_owner = alias_idx.get(nb)
                if alias_owner and alias_owner != profile["entity_id"]:
                    owners.add(alias_owner)
                # #52: never OVERWRITE an EXISTING entity whose id equals this fund's
                # brand-slug ("XP" -> "xp"). The self-exclusion above hides exactly this
                # case (the colliding owner IS profile.entity_id); if that id already
                # exists and is not this fund (by CNPJ), it's a foreign entity -> propose.
                if (profile["entity_id"] in existing_ids
                        and cnpj_idx.get(root) != profile["entity_id"]):
                    owners.add(profile["entity_id"])
                if brand and owners:
                    pid = entity_registry.propose_review(
                        kind="discovery",
                        key=profile["entity_id"],
                        proposed=brand,
                        reason="name_collision",
                        hint=f"cvm_fiagro cnpj={root} ticker={ticker or '-'} owner={sorted(owners)[0]}",
                        confidence="cnpj",
                        payload={"profile": profile, "source": "cvm_fiagro", "cnpj": root},
                        table=table,
                    )
                    report["proposed"].append(pid or brand)
                    continue
                entity_registry.put_entity(
                    profile["entity_id"],
                    profile["display_name"],
                    profile.get("aliases") or [],
                    cnpj_roots=profile.get("cnpj_roots") or [],
                    industries=[industry],
                    ticker=ticker,
                    confidence="cnpj",
                    parent=_parent_of(profile.get("display_name")),  # ADR 017 sub-entity link
                    source="discovery",  # ADR 018
                    table=table,
                )
                report["created"].append(profile["entity_id"])
                new_budget -= 1
                # Keep local indexes current so later rows see this new entity
                # (mirror put_entity's normalization: skip TICKER: forms).
                cnpj_idx[root] = profile["entity_id"]
                if nb:
                    disp_idx.setdefault(nb, []).append(profile["entity_id"])
                for a in profile.get("aliases") or []:
                    if str(a).upper().startswith("TICKER:"):
                        continue
                    na = _norm(a)
                    if na:
                        alias_idx.setdefault(na, profile["entity_id"])
            except Exception as exc:  # pragma: no cover
                report["errors"].append(
                    {"profile": profile.get("entity_id"), "error": str(exc)}
                )
        else:
            if not root:
                reason = "fiagro_no_cnpj"
            elif not profile.get("auto_ok"):
                reason = "needs_brand_review"  # strong CNPJ id but no clean brand
            else:
                reason = "fiagro_missing"  # auto_create off / budget exhausted
            try:
                pid = entity_registry.propose_review(
                    kind="discovery",
                    key=profile["entity_id"],
                    proposed=profile["display_name"],
                    reason=reason,
                    hint=f"cvm_fiagro cnpj={root or '-'} ticker={ticker or '-'} pl={profile.get('pl')} raw={str(profile.get('raw_name'))[:60]!r}",
                    confidence="cnpj" if root else "fuzzy",
                    payload={
                        "profile": profile,
                        "source": "cvm_fiagro",
                        "cnpj": root,
                        "ticker": ticker,
                        "pl": profile.get("pl"),
                    },
                    table=table,
                )
                report["proposed"].append(pid or profile["entity_id"])
            except Exception as exc:  # pragma: no cover
                report["errors"].append(
                    {"profile": profile.get("entity_id"), "error": str(exc)}
                )

    return report


def harvest_keyword(
    keyword: str | Iterable[str],
    news_items: Iterable[dict[str, Any]],
    *,
    industry: str | None = None,
    min_docs: int = 2,
    table: Any | None = None,
) -> list[dict[str, Any]]:
    """Scan news for a keyword (or keyword set) and collect unresolved entities.

    Industry-agnostic. ``keyword`` may be a single string or several phrase
    variants; matching is accent- and plural-tolerant (``_kw_regex``). Near each
    keyword hit it captures three surface shapes:
      - **B3 tickers** — 4 letters + 1–2 digits, spanning funds (XXXX11) AND
        equities (XXXX3/4), so banking/insurance names surface, not just funds;
      - **multi-word proper names** — "Porto Seguro", "C6 Bank";
      - **single-token proper names** — "Neon", "Nubank", "Itaú" (Initial-cap +
        lowercase), excluding the keyword's own words, generic corporate words
        (``_SINGLE_STOP``), and sentence-initial tokens to curb noise.

    Frequency-gated (``min_docs`` distinct items). Already-resolved names are
    dropped (``resolve_entities`` + registry alias/name lookups). Returns
    candidate dicts with evidence, most-cited first. Propose-only downstream —
    news alone never auto-creates (ADR 011 §4).
    """
    forms = [keyword] if isinstance(keyword, str) else list(keyword)
    forms = [f for f in forms if str(f or "").strip()]
    kw_re = _kw_regex(forms)
    if kw_re is None:
        return []
    primary = str(forms[0]).strip().upper()
    kw_tokens = {_norm_txt(w) for f in forms for w in re.split(r"\s+", str(f)) if w}

    hits: dict[str, list[str]] = {}
    kinds: dict[str, str] = {}
    evidence: dict[str, list[dict[str, Any]]] = {}

    def _add(label: str, kind: str, doc_id: str, snippet: str) -> None:
        label = label.strip()
        if not label:
            return
        hits.setdefault(label, []).append(doc_id)
        kinds.setdefault(label, kind)
        evidence.setdefault(label, []).append({"doc_id": doc_id, "snippet": snippet[:120]})

    def _near(norm_text: str, start: int, end: int) -> bool:
        return bool(kw_re.search(norm_text[max(0, start - 120) : end + 120]))

    for item in news_items or []:
        if not isinstance(item, dict):
            continue
        text = " ".join(
            str(item.get(k) or "") for k in ("title", "text", "summary", "headline")
        )
        if not text:
            continue
        norm_text = _norm_txt(text)
        if not kw_re.search(norm_text):
            continue
        doc_id = str(item.get("id") or item.get("url") or hash(text) % 10**10)

        # Tickers — the whole item already contains the keyword.
        for m in _TICKER_RE.finditer(text.upper()):
            _add(m.group(1), "ticker", doc_id, text[max(0, m.start() - 40) : m.end() + 40])

        # Multi-word brands — require the keyword nearby.
        for m in _BRAND_MULTI_RE.finditer(text):
            brand = m.group(1).strip()
            if not [t for t in brand.split() if t.lower() not in _STOP]:
                continue
            if _near(norm_text, m.start(), m.end()):
                _add(brand, "brand", doc_id, text[max(0, m.start() - 40) : m.end() + 40])

        # Single-token proper names — skip keyword words, generic corp words, and
        # only the token AT the sentence start (a capitalized common word there is
        # ambiguous; the same brand recurring mid-text is still captured).
        first_start = len(text) - len(text.lstrip())
        for m in _BRAND_SINGLE_RE.finditer(text):
            tok = m.group(1).strip()
            if (
                tok.lower() in _STOP
                or _norm_txt(tok) in kw_tokens
                or _strip_accents(tok).lower() in _SINGLE_STOP
                or m.start() == first_start
            ):
                continue
            if _near(norm_text, m.start(), m.end()):
                _add(tok, "brand", doc_id, text[max(0, m.start() - 40) : m.end() + 40])

    ind = (
        industry
        or INDUSTRY_KEYWORDS.get(primary)
        or INDUSTRY_KEYWORDS.get(primary.rstrip("S"))
    )

    candidates: list[dict[str, Any]] = []
    for label, ids in hits.items():
        n_docs = len(set(ids))
        if n_docs < min_docs:
            continue
        try:
            if entity_registry.resolve_by_alias(label, table=table):
                continue
            if entity_registry.resolve_by_name(label, table=table):
                continue
        except Exception:
            pass
        is_ticker = kinds.get(label) == "ticker"
        candidates.append(
            {
                "surface": label,
                "kind": "ticker" if is_ticker else "brand",
                "tickers": [label] if is_ticker else [],
                "doc_count": n_docs,
                "count": n_docs,
                "industry": ind,
                "keyword": primary,
                "evidence_ids": list(set(ids))[:8],
                "evidence": (evidence.get(label) or [])[:5],
                "sample_titles": [
                    (e.get("snippet") or "")[:80] for e in (evidence.get(label) or [])[:3]
                ],
            }
        )

    candidates.sort(
        key=lambda c: (-c["doc_count"], 0 if c["kind"] == "ticker" else 1, c["surface"])
    )
    return candidates


def harvest_ner(
    narratives: Iterable[dict[str, Any]], *,
    min_mentions: int = 2, max_candidates: int = 40, table: Any | None = None,
) -> list[dict[str, Any]]:
    """#14 general NER harvest — company-name candidates from the narrative corpus.

    Heuristic (no ML): a proper-name span next to a legal-suffix / sector-word / typed-
    article cue is a company mention. Drops names that already resolve to a registry entity,
    strips generic/regulator heads, and frequency-gates across distinct narratives. Returns
    candidate dicts for `propose_news_candidates` — PROPOSE-ONLY (news never auto-creates,
    ADR 011 §4)."""
    hits: dict[str, set[str]] = {}
    evidence: dict[str, list[str]] = {}

    def _add(label: str, doc_id: str, snippet: str) -> None:
        label = re.sub(r"\s+", " ", str(label or "")).strip(" .,-")
        toks = [t for t in label.split() if _strip_accents(t).lower() not in _NER_STOP]
        # need a distinctive token, ≥3 chars, and not just short/generic heads
        if not label or not toks or len(label) < 3 or all(len(t) <= 2 for t in toks):
            return
        hits.setdefault(label, set()).add(doc_id)
        evidence.setdefault(label, []).append(re.sub(r"\s+", " ", snippet[:120]).strip())

    for n in narratives or []:
        if not isinstance(n, dict):
            continue
        text = " ".join(str(n.get(k) or "") for k in ("narrative", "title", "text", "summary"))
        if not text:
            continue
        doc_id = str(n.get("id") or hash(text) % 10**10)
        for rx in (_NER_SUFFIX_RE, _NER_PREFIX_RE, _NER_TYPED_RE):
            for m in rx.finditer(text):
                _add(m.group(1), doc_id, text[max(0, m.start() - 30): m.end() + 30])

    # A candidate is "known" iff its name resolves to a registry entity (alias or name,
    # table-aware — reads the DB alias map when a table is supplied). Non-empty ⇒ drop.
    def _is_known(name: str) -> bool:
        try:
            return bool(
                entity_registry.resolve_by_alias(name, table=table)
                or entity_registry.resolve_by_name(name, table=table)
            )
        except Exception:  # pragma: no cover - resolution best-effort
            return False

    cands: list[dict[str, Any]] = []
    for label, ids in hits.items():
        if len(ids) < min_mentions or _is_known(label):
            continue  # too rare, or already a known entity
        cands.append({
            "surface": label, "kind": "ner", "keyword": "ner", "industry": None,
            "doc_count": len(ids), "evidence": evidence.get(label, [])[:3],
            "evidence_ids": sorted(ids)[:5],
        })
    cands.sort(key=lambda c: -c["doc_count"])
    return cands[:max_candidates]


def propose_news_candidates(
    candidates: list[dict[str, Any]],
    *,
    table: Any | None = None,
    max_propose: int = 20,
) -> list[str]:
    """Emit review-queue proposals for news-only discovery candidates.

    Never auto-creates from news alone (ADR 011 §4). Returns proposal ids.
    """
    proposed: list[str] = []
    for c in (candidates or [])[:max_propose]:
        surface = str(c.get("surface") or c.get("label") or "").strip()
        if not surface:
            continue
        try:
            docs = c.get("doc_count") or c.get("count") or 0
            pid = entity_registry.propose_review(
                kind="discovery",
                key=surface,
                proposed=surface,
                reason=f"news_keyword_harvest:{c.get('keyword') or ''}",
                hint=f"industry={c.get('industry') or '-'} docs={docs} tickers={c.get('tickers') or []}",
                confidence="fuzzy",
                payload={
                    "surface": surface,
                    "kind": c.get("kind"),
                    "industry": c.get("industry"),
                    "keyword": c.get("keyword"),
                    "doc_count": docs,
                    "evidence": c.get("evidence") or [],
                    "evidence_ids": c.get("evidence_ids") or [],
                    "sample_titles": c.get("sample_titles") or [],
                    "tickers": c.get("tickers") or [],
                },
                table=table,
            )
            proposed.append(pid or surface)
        except Exception:
            continue
    return proposed


def run_discovery(
    *,
    fiagro: bool = True,
    keyword: str | None = "FIAGRO",
    news_items: Iterable[dict[str, Any]] | None = None,
    auto_create_structured: bool = True,
    min_pl: float = 50_000_000.0,
    table: Any | None = None,
) -> dict[str, Any]:
    """Run the discovery pipeline end-to-end for the FIAGRO vertical (extensible).

    Structured CVM path runs first (strong ids). News keyword harvest is
    propose-only and only runs when ``news_items`` is supplied.
    """
    summary: dict[str, Any] = {"fiagro": None, "keyword": None}

    if fiagro:
        summary["fiagro"] = discover_fiagro(
            min_pl=min_pl,
            auto_create=auto_create_structured,
            table=table,
        )

    if keyword and news_items is not None:
        cands = harvest_keyword(keyword, news_items, table=table)
        proposed = propose_news_candidates(cands, table=table)
        summary["keyword"] = {
            "keyword": keyword,
            "candidates": len(cands),
            "proposed": proposed,
            "top": cands[:10],
        }

    return summary


if __name__ == "__main__":
    import json
    import sys

    write = "--write" in sys.argv
    if not write:
        os.environ.pop("ONCA_ENTITIES_TABLE", None)
        print("Dry-run (pass --write and set ONCA_ENTITIES_TABLE to mutate registry)")

    from src.ingest import cvm_fiagro

    rows = cvm_fiagro.fetch_fiagro(min_pl=50e6)
    print(f"Fetched {len(rows)} FIAGRO classes with PL ≥ R$50mi")
    for r in rows[:8]:
        p = _profile_from_fiagro(r)
        print(
            f"  {p['entity_id']:20}  ticker={p.get('ticker')}  "
            f"cnpj={p['cnpj_roots']}  display={str(p['display_name'])[:40]}"
        )

    if write and os.environ.get("ONCA_ENTITIES_TABLE"):
        report = discover_fiagro(min_pl=50e6, auto_create=True)
        print(
            json.dumps(
                {k: (v if not isinstance(v, list) else len(v)) for k, v in report.items()},
                indent=2,
            )
        )
        print("created:", report["created"][:15])
