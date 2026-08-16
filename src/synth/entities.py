"""Entity resolution helpers for multi-source fusion (payments/fintech focus)."""
from __future__ import annotations

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
    for entity_id, aliases in ENTITY_ALIASES.items():
        for alias in aliases:
            token = alias.upper()
            if token.startswith("TICKER:"):
                continue
            if token in ctrl and token not in own_name:
                parents.append(entity_id)
                break
    return parents


def resolve_entities(item: dict[str, Any]) -> list[str]:
    """Return canonical entity ids matched in an item (may be multiple).

    Includes parents linked via an entrant's QSA controllers, so a new fintech
    controlled by a known player clusters into that player's narrative.
    """
    blob = f" {signal_blob(item)} "
    found: list[str] = []
    for entity_id, aliases in ENTITY_ALIASES.items():
        for alias in aliases:
            token = alias.upper()
            if token.startswith("TICKER:"):
                if token in blob.replace(" ", ""):
                    found.append(entity_id)
                    break
                # also plain ticker word boundary-ish
                t = token.split(":", 1)[1]
                if re.search(rf"(^|[^A-Z0-9]){re.escape(t)}([^A-Z0-9]|$)", blob):
                    found.append(entity_id)
                    break
            elif token in blob:
                found.append(entity_id)
                break
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
