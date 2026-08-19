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


def _match_kinds(aliases: list[str], blob: str, blob_nospace: str) -> set[str]:
    """Classify how an entity's aliases hit the blob:
    'strong'  — the item's own ticker field (TICKER:XXX) is present (authoritative);
    'distinct'— a distinctive alias (multi-token, or a non-common ticker symbol);
    'ambiguous'— only a bare common-word token matched (needs structured context)."""
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
                kinds.add("ambiguous" if sym in AMBIGUOUS_TOKENS else "distinct")
            continue
        if _word_match(token, blob):
            if " " not in token and token in AMBIGUOUS_TOKENS:
                kinds.add("ambiguous")
            else:
                kinds.add("distinct")
    return kinds


def resolve_entities(item: dict[str, Any]) -> list[str]:
    """Return canonical entity ids matched in an item (may be multiple).

    Identity is established by anchored, context-gated matches (not raw
    substrings): a strong identifier (ticker) or a distinctive alias resolves
    anywhere; a bare *ambiguous* common-word token resolves only in a structured
    identity source — never from a free-text headline (so "Rolling Stone" music
    news does not cluster into Stone the acquirer). Also links QSA-controller
    parents so a new fintech clusters into a known player's narrative.
    """
    blob = f" {signal_blob(item)} "
    blob_nospace = blob.replace(" ", "")
    free_text = str(item.get("source") or "").upper() in _FREE_TEXT_SOURCES
    found: list[str] = []
    for entity_id, aliases in _alias_map().items():
        kinds = _match_kinds(aliases, blob, blob_nospace)
        if not kinds:
            continue
        if "strong" in kinds or "distinct" in kinds:
            found.append(entity_id)
        elif not free_text:
            found.append(entity_id)  # ambiguous, but a structured field asserts it
        # else: ambiguous token in free text → dropped (precision over recall)
    for parent in known_parents(item):
        if parent not in found:
            found.append(parent)
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
