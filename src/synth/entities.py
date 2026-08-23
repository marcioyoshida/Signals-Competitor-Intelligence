"""Entity resolution helpers for multi-source fusion (payments/fintech focus)."""
from __future__ import annotations

import os
import re
from typing import Any

# Canonical entity id → aliases (uppercased substrings / tickers).
# Used to fuse SEC, CVM, BCB signals that use different labels.
ENTITY_ALIASES: dict[str, list[str]] = {
    "nubank": ["NUBANK", "NU PAGAMENTOS", "NU HOLDINGS", "NU INVEST", " NU ", "TICKER:NU"],
    # "STONE " (trailing space) matches Stone IP/SCFI/SCD without catching the
    # unrelated "BANCO STONEX" / "STONEX DTVM" (StoneX Group).
    "stone": ["STONECO", "STONE ", "STNE", "TICKER:STNE"],
    "pagseguro": ["PAGSEGURO", "PAGBANK", "PAGS", "TICKER:PAGS"],
    "inter": ["INTER&CO", "INTER CO", "BANCO INTER", "INTR", "TICKER:INTR"],
    "xp": ["XP INC", "XP INVESTIMENTOS", "XP INVEST", "TICKER:XP", "BCO XP"],
    "itau": ["ITAU", "ITAÚ", "ITAU UNIBANCO", "ITAU BBA", "INTRAG"],
    "btg": ["BTG PACTUAL", "BTG"],
    "bradesco": ["BRADESCO", "BRADESCARD"],
    "santander": ["SANTANDER"],
    "bb": ["BANCO DO BRASIL", "BANCO DO BRA", " BB "],
    "caixa": ["CAIXA ECONOMICA", "CAIXA ECONÔMICA", "CAIXA"],
    "picpay": ["PICPAY"],
    "mercado_pago": ["MERCADO PAGO", "MERCADO CRÉDITO", "MERCADO CREDITO"],
    "c6": ["BCO C6", "BANCO C6", "C6 BANK"],
    "original": ["BANCO ORIGINAL"],
    "neon": ["NEON PAGAMENTOS", "NEON FINANCEIRA", "NEON CORRETORA", "NEON "],
    # Private fintechs (legal names verified live against the BCB registry).
    "creditas": ["CREDITAS"],
    "recargapay": ["RECARGAPAY", "RECARGA PAY"],
    # Brand InfinitePay; legal entity CloudWalk.
    "infinitepay": ["INFINITEPAY", "INFINITE PAY", "CLOUDWALK", "CLOUD WALK"],
    # Nomad has no BCB footprint (US-facing) — matches via CVM/news only.
    "nomad": ["NOMAD"],
}

# Curated entity -> industry module(s) (ADR 002 Phase B). Entities can span
# several; kept deliberately small and human-assigned (the trusted classification
# auto-tagging cannot infer). Slugs must exist in entity_registry.INDUSTRIES.
ENTITY_INDUSTRIES: dict[str, list[str]] = {
    "itau": ["banking"], "bb": ["banking"], "bradesco": ["banking"],
    "santander": ["banking"], "caixa": ["banking"], "c6": ["banking"],
    "original": ["banking"],
    "nubank": ["banking", "fintech"], "inter": ["banking", "fintech"],
    "btg": ["investment-banking", "asset-management"],
    "xp": ["asset-management", "investment-banking"],
    "stone": ["fintech"], "pagseguro": ["fintech"], "mercado_pago": ["fintech"],
    "picpay": ["fintech"], "neon": ["fintech"], "creditas": ["fintech"],
    "recargapay": ["fintech"], "infinitepay": ["fintech"], "nomad": ["fintech"],
}


def _alias_map() -> dict[str, list[str]]:
    """Effective {entity_id: aliases} — the DynamoDB registry when configured
    (so new entities resolve with no code deploy), else the built-in seed."""
    if os.environ.get("ONCA_ENTITIES_TABLE"):
        try:
            from src.synth import entity_registry

            registry = entity_registry.load_alias_map()
            if registry:
                return registry
        except Exception as exc:  # pragma: no cover - graceful fallback
            print(f"Warning: entities registry unavailable, using built-in aliases: {exc}")
    return ENTITY_ALIASES


def _trust_map() -> dict[str, bool]:
    """{entity_id: trusted-for-free-text-bare-token}. Registry entities are trusted
    iff curated or news_safe; the built-in seed (returned empty here) defaults to
    trusted in resolve_entities since it is all curated."""
    if os.environ.get("ONCA_ENTITIES_TABLE"):
        try:
            from src.synth import entity_registry

            return entity_registry.load_trust_map()
        except Exception as exc:  # pragma: no cover - graceful fallback
            print(f"Warning: entities trust map unavailable: {exc}")
    return {}


def _ambiguous_tokens() -> frozenset[str]:
    """Bare tokens that are common words (resolve only in structured sources).

    Registry-backed (per-entity ``ambiguous`` flag) when the table is configured,
    else the built-in AMBIGUOUS_TOKENS seed. Data, not code — API-editable."""
    if os.environ.get("ONCA_ENTITIES_TABLE"):
        try:
            from src.synth import entity_registry

            toks = entity_registry.load_ambiguous_tokens()
            if toks:
                return frozenset(toks)
        except Exception as exc:  # pragma: no cover - graceful fallback
            print(f"Warning: ambiguous-token map unavailable, using built-in: {exc}")
    return AMBIGUOUS_TOKENS


def signal_blob(item: dict[str, Any]) -> str:
    parts = [
        str(item.get(k) or "")
        for k in (
            "subject",
            "doc_type",
            "title",
            "institution",
            "name",
            "company",
            "issuer",
            "leader",
            "admin",
            "manager",
            "fund_name",
            "modality",
            "security",
            "ticker",
            "segment",
            "entity_type",
        )
    ]
    ticker = item.get("ticker")
    if ticker:
        parts.append(f"TICKER:{str(ticker).upper()}")
    return " ".join(parts).upper()


def known_parents(item: dict[str, Any]) -> list[str]:
    """Known-competitor entities behind an entrant, from its QSA controllers.

    Links a quietly-registered fintech to its parent when a controller/sócio is
    a known player — e.g. a new SCD controlled by "NU HOLDINGS" → ``nubank``.
    Excludes a match that is really the entrant's own name (avoids self-links).
    """
    controllers = item.get("controllers") or item.get("controller") or []
    if isinstance(controllers, str):
        controllers = [controllers]
    ctrl = " " + " ".join(str(c) for c in controllers).upper() + " "
    if not ctrl.strip():
        return []
    own_name = str(item.get("name") or "").upper()
    parents: list[str] = []
    for entity_id, aliases in _alias_map().items():
        for alias in aliases:
            token = alias.upper()
            if token.startswith("TICKER:"):
                continue
            if token in ctrl and token not in own_name:
                parents.append(entity_id)
                break
    return parents


# Acquiring stems (uppercase, accent-folded not needed — matched as substrings on
# the already-uppercased blob). Distinctive to merchant acquiring, so they gate the
# ambiguous "Rede" (=network) without catching card *issuing* ("cartão").
_ACQ_TERMS = ("ADQUIR", "MAQUININHA", "CREDENCIAD", "MDR", "TPV")

# Bank-owned acquiring subsidiaries. A parent-bank signal that is specifically about
# its acquiring arm also surfaces under the subsidiary, so the Adquirência module
# captures the parent's acquiring-segment activity (the only channel for Rede, which
# is delisted, internal to Itaú, and can't resolve on its own). Attach only when the
# parent is present AND a distinctive subsidiary name appears; for an ambiguous name
# ("Rede" = network) also require an acquiring term, so "rede de agências" (branch
# network) never false-matches. This is resolution *logic* (like known_parents), kept
# in code; the parent/child identities live in the registry.
ACQUIRING_SUBSIDIARIES: dict[str, dict[str, tuple[str, ...]]] = {
    "cielo": {"parents": ("bradesco", "bb"), "names": ("CIELO",), "ambiguous_names": ()},
    "getnet": {"parents": ("santander",), "names": ("GETNET",), "ambiguous_names": ()},
    "rede": {"parents": ("itau",), "names": ("REDECARD",), "ambiguous_names": ("REDE",)},
}


def _word_present(token: str, blob: str) -> bool:
    return re.search(r"(?<![0-9A-ZÀ-Ÿ])" + re.escape(token) + r"(?![0-9A-ZÀ-Ÿ])", blob) is not None


def acquiring_cross_refs(item: dict[str, Any], found: list[str]) -> list[str]:
    """Bank-owned acquiring subsidiaries to attach when a parent's signal is about
    its acquiring arm. Returns subsidiaries not already in ``found``."""
    blob = " " + signal_blob(item) + " "
    has_term = any(t in blob for t in _ACQ_TERMS)
    out: list[str] = []
    for sub, cfg in ACQUIRING_SUBSIDIARIES.items():
        if sub in found or not any(p in found for p in cfg["parents"]):
            continue
        hit = any(_word_present(n, blob) for n in cfg["names"])
        if not hit and has_term:
            hit = any(_word_present(n, blob) for n in cfg.get("ambiguous_names", ()))
        if hit:
            out.append(sub)
    return out


# Bare single-token aliases that are also common words / surnames / too-short —
# they collide with unrelated free text ("Stone" vs "Rolling Stone", "caixa" =
# cashbox, "nu" = nude in pt, "nomad"/"neon" everyday words). Each of these
# entities ALSO has a distinctive multi-token or ticker alias, so gating the bare
# token barely costs recall. (Nomad's only alias is the bare word — it is the one
# entity that loses free-text recall; add a distinctive alias if that matters.)
AMBIGUOUS_TOKENS: frozenset[str] = frozenset(
    {"STONE", "NEON", "NOMAD", "NU", "BB", "CAIXA", "XP"}
)

# Sources whose entity mentions are FREE TEXT (headlines), where a bare ambiguous
# token is untrustworthy. Keyed on `source` because DOU carries kind="regulatory"
# like BCB, yet its body is prose. Everything else is treated as a structured
# identity source (the field itself asserts "this is a company").
_FREE_TEXT_SOURCES: frozenset[str] = frozenset({"NEWS", "DOU"})

# Word chars for boundary tests: letters (incl. pt accents), digits, underscore.
_WORD = r"[0-9A-Za-zÁÉÍÓÚÂÊÔÃÕÇÀÜáéíóúâêôãõçàü_]"


def _word_match(token: str, blob: str) -> bool:
    """Whole-word (boundary-anchored) match of an uppercase token/phrase — so
    STONE does not match STONEX/LIMESTONE, and ação does not match celebração."""
    return re.search(rf"(?<!{_WORD}){re.escape(token)}(?!{_WORD})", blob) is not None


def _match_kinds(
    aliases: list[str],
    blob: str,
    blob_nospace: str,
    trusted: bool = True,
    ambiguous_tokens: frozenset[str] | set[str] = AMBIGUOUS_TOKENS,
) -> set[str]:
    """Classify how an entity's aliases hit the blob:
    'strong'  — the item's own ticker field (TICKER:XXX) is present (authoritative);
    'distinct'— a distinctive alias (multi-token, or a curated non-common token);
    'ambiguous'— a bare token that needs structured context.

    ``trusted`` is False for entities no human has vouched for (auto-created, not
    yet news_safe): their bare single-token brand is treated as ambiguous, so a
    new common-word-brand fintech can't false-match in free text before review.
    ``ambiguous_tokens`` is the set of common-word bare tokens; registry-backed
    (per-entity ``ambiguous`` flag) when configured, else the built-in seed.
    """
    kinds: set[str] = set()
    for alias in aliases:
        token = alias.upper().strip()
        if not token:
            continue
        if token.startswith("TICKER:"):
            sym = token.split(":", 1)[1]
            if token.replace(" ", "") in blob_nospace:
                kinds.add("strong")  # from item.ticker — unambiguous
            elif _word_match(sym, blob):
                kinds.add("ambiguous" if sym in ambiguous_tokens else "distinct")
            continue
        if _word_match(token, blob):
            if " " in token:
                kinds.add("distinct")  # multi-token names are always distinctive
            elif trusted and token not in ambiguous_tokens:
                kinds.add("distinct")
            else:
                kinds.add("ambiguous")
    return kinds


# Cited-source constructions (pt-BR): when an entity is named only as the SOURCE
# of a figure — "segundo a Serasa", "dados da Economatica", "levantamento da X" —
# it is not the subject of the story. Data-vendor/bureau names (Serasa, Boa Vista,
# Economatica, Quod) are constantly cited this way in market/credit news, so a raw
# match mis-frames them as the subject. The cue is matched immediately BEFORE the
# entity mention; if every mention is source-cued, the entity is dropped.
_SOURCE_CUE = re.compile(
    r"(?:SEGUNDO|CONFORME|DE ACORDO COM|DADOS|LEVANTAMENTO|PESQUISA|ESTUDO|"
    r"MONITOR|BOLETIM|PAINEL|[IÍ]NDICE|RELAT[OÓ]RIO|C[AÁ]LCULO|N[UÚ]MEROS)"
    # Optional reporting verb between the cue and the source name — the common
    # "conforme APONTA a Serasa" / "segundo MOSTROU a X" form. Stems cover the
    # conjugations (aponta/apontou/apontam, indica/indicou, …).
    r"(?:\s+(?:APONT|APUR|INDIC|MOSTR|REVEL|ESTIM|CALCUL|INFORM|DIVULG|REGISTR|"
    r"PROJET|CONSTAT|SINALIZ|AFER|DETECT|IDENTIFIC)\w*)?"
    r"(?:\s+D[AEO]S?| COM| POR| SOBRE)?"
    r"(?:\s+(?:A|O|AS|OS))?"
    r"\s+$"
)


def _source_attribution_only(token: str, blob: str) -> bool:
    """True iff every whole-word occurrence of ``token`` in ``blob`` is immediately
    preceded by a cited-source cue (so the entity is the source, not the subject)."""
    anchored = re.compile(rf"(?<!{_WORD}){re.escape(token)}(?!{_WORD})")
    any_occ = False
    for m in anchored.finditer(blob):
        any_occ = True
        if not _SOURCE_CUE.search(blob[: m.start()]):
            return False  # a subject-position mention exists → keep the entity
    return any_occ


def _entity_source_only(aliases: list[str], blob: str) -> bool:
    """True iff the entity matches the blob only in cited-source position."""
    matched = False
    for alias in aliases:
        token = alias.upper().strip()
        if not token or token.startswith("TICKER:"):
            continue
        if _word_match(token, blob):
            matched = True
            if not _source_attribution_only(token, blob):
                return False
    return matched


def resolve_entities(item: dict[str, Any]) -> list[str]:
    """Return canonical entity ids matched in an item (may be multiple).

    Identity is established by anchored, context-gated matches (not raw
    substrings): a strong identifier (ticker) or a distinctive alias resolves
    anywhere; a bare *ambiguous* common-word token resolves only in a structured
    identity source — never from a free-text headline (so "Rolling Stone" music
    news does not cluster into Stone the acquirer). A free-text match that is only
    a cited data source ("segundo a Serasa") is likewise dropped. Also links
    QSA-controller parents so a new fintech clusters into a known player's narrative.
    """
    blob = f" {signal_blob(item)} "
    blob_nospace = blob.replace(" ", "")
    free_text = str(item.get("source") or "").upper() in _FREE_TEXT_SOURCES
    trust = _trust_map()
    ambig = _ambiguous_tokens()
    found: list[str] = []
    for entity_id, aliases in _alias_map().items():
        # Default trusted=True: the built-in seed is all curated, and a registry
        # entity absent from the trust map shouldn't be silently muted.
        kinds = _match_kinds(
            aliases, blob, blob_nospace,
            trusted=trust.get(entity_id, True), ambiguous_tokens=ambig,
        )
        if not kinds:
            continue
        resolve = ("strong" in kinds or "distinct" in kinds) or (not free_text)
        if not resolve:
            continue  # ambiguous token in free text → dropped (precision over recall)
        # Source-attribution guard: a free-text distinct match that appears only as
        # a cited data source is not the story's subject. A strong ticker id (a
        # structured assertion) is exempt.
        if free_text and "strong" not in kinds and _entity_source_only(aliases, blob):
            continue
        found.append(entity_id)
    for parent in known_parents(item):
        if parent not in found:
            found.append(parent)
    # A parent bank's acquiring-segment signal also surfaces under its acquiring
    # subsidiary (the only channel for Rede; corroboration for Cielo/Getnet).
    for sub in acquiring_cross_refs(item, found):
        found.append(sub)
    return found


def primary_entity(item: dict[str, Any]) -> str | None:
    ents = resolve_entities(item)
    return ents[0] if ents else None


def tokens_for_match(item: dict[str, Any], min_len: int = 4) -> set[str]:
    """Generic tokens for soft matching when no alias hits."""
    blob = signal_blob(item)
    stop = {
        "FUNDO",
        "INVESTIMENTO",
        "CLASSE",
        "COTA",
        "BANCO",
        "PREFIXADO",
        "CREDITO",
        "CRÉDITO",
        "RENDA",
        "FIXA",
        "TOTAL",
        "LIMITADA",
        "RESP",
        "PRIVADO",
        "PUBLICO",
        "PÚBLICO",
    }
    toks = {
        t
        for t in re.split(r"[^A-Z0-9ÁÉÍÓÚÂÊÔÃÕÇ]+", blob)
        if len(t) >= min_len and t not in stop
    }
    return toks
