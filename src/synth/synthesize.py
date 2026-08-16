"""Build flagged narratives from candidates — LLM preferred, heuristic fallback."""
from __future__ import annotations

import datetime as dt
from typing import Any

from src.synth import bedrock_llm, citations
from src.synth.entities import known_parents


SYSTEM = (
    "You are a competitive-intelligence analyst for Brazilian financial services. "
    "Write the briefing in Brazilian Portuguese (pt-BR) — the readers are "
    "non-bilingual executives. Write a short flagged briefing (3–6 sentences). "
    "When a Source provides an http(s) URL, cite the claim by pasting that URL "
    "verbatim as plain text. If a Source has no URL, refer to it by name only — "
    "never write a URL, the literal word 'None', angle brackets like "
    "'<BCB-Autorizacoes>', or a placeholder such as '(URL: ...)'. "
    "Do not invent URLs or numbers. If uncertain, say so. "
    "Prefer Portuguese for regulatory titles when present. "
    "Return only the briefing prose — no title, heading, or markdown."
)

ENTITY_LABELS = {
    "nubank": "Nubank / Nu Holdings",
    "stone": "Stone / StoneCo",
    "pagseguro": "PagSeguro / PagBank",
    "inter": "Inter&Co",
    "xp": "XP Inc / XP Investimentos",
    "itau": "Itaú",
    "btg": "BTG Pactual",
    "bradesco": "Bradesco",
    "santander": "Santander",
    "bb": "Banco do Brasil",
    "caixa": "Caixa Econômica Federal",
    "picpay": "PicPay",
    "mercado_pago": "Mercado Pago",
    "c6": "C6 Bank",
    "original": "Banco Original",
    "neon": "Neon",
    "creditas": "Creditas",
    "recargapay": "RecargaPay",
    "infinitepay": "InfinitePay (CloudWalk)",
    "nomad": "Nomad",
}

# Plain pt-BR names for the signal "lenses" (data sources fused into a narrative).
LENS_LABELS = {
    "regulatory": "Regulatório (BCB)",
    "fatos": "Fato Relevante (CVM)",
    "dou": "Diário Oficial (DOU)",
    "sec": "Documentos SEC (EUA)",
    "ofertas": "Ofertas de distribuição (CVM)",
    "inf_diario": "Patrimônio de fundos (CVM)",
    "funds": "Registro de fundos (CVM)",
    "pix": "Movimentação Pix",
    "juros": "Juros médios (BCB)",
    "entrants": "Novos entrantes (BCB)",
    "market": "Participação de mercado",
}


def synthesize_candidate(
    candidate: dict[str, Any],
    *,
    use_llm: bool = True,
) -> dict[str, Any] | None:
    """Return a narrative record with enforced citations, or None if unusable."""
    sources = list(candidate.get("sources") or [])
    raw_text: str | None = None
    mode = "heuristic"

    if use_llm:
        prompt = _build_prompt(candidate)
        raw_text = bedrock_llm.converse(
            prompt,
            model_id=bedrock_llm.DEFAULT_SYNTH_MODEL,
            system=SYSTEM,
        )
        if raw_text:
            mode = "llm"

    if not raw_text:
        raw_text = _heuristic_narrative(candidate)
        mode = "heuristic"

    guarded = citations.enforce_citations(raw_text, sources)
    if not guarded["ok"]:
        raw_text = _heuristic_narrative(candidate, force_urls=True)
        guarded = citations.enforce_citations(raw_text, sources)
        mode = "heuristic"
        if not guarded["ok"]:
            return None

    return {
        "id": candidate.get("id"),
        "kind": candidate.get("kind"),
        "entity": candidate.get("entity"),
        "entities": candidate.get("entities") or [],
        "lenses": candidate.get("lenses") or [],
        "is_alert": bool(candidate.get("is_alert")),
        "threat_score": candidate.get("threat_score"),
        "threat_factors": candidate.get("threat_factors") or {},
        "threat_score_note": "estimated_v1",
        "narrative": guarded["narrative"],
        "citations": guarded["citations"],
        "dropped_urls": guarded["dropped_urls"],
        "mode": mode,
        "as_of": candidate.get("as_of") or dt.date.today().isoformat(),
        "data_as_of": candidate.get("data_as_of") or {},
        "source_ids": [s.get("id") for s in sources if s.get("id")],
    }


def _build_prompt(candidate: dict[str, Any]) -> str:
    lines = [
        f"Entity: {candidate.get('entity') or candidate.get('entities') or 'n/a'}",
        f"Lenses: {', '.join(candidate.get('lenses') or [])}",
        "Sources (use only these URLs):",
    ]
    for s in candidate.get("sources") or []:
        url = s.get("url")
        url_field = url if url else "(no link — refer by name, do not write a URL)"
        lines.append(
            f"- lens={s.get('_lens')} id={s.get('id')} source={s.get('source')} "
            f"url={url_field} summary={_one_line(s)}"
        )
    lines.append("")
    lines.append("Write the flagged briefing now, fusing the lenses into one narrative.")
    return "\n".join(lines)


def _one_line(src: dict[str, Any]) -> str:
    for key in (
        "subject",
        "fund_name",
        "issuer",
        "name",
        "company",
        "institution",
        "security",
        "modality",
        "ticker",
        "form",
    ):
        if src.get(key):
            extra = ""
            if src.get("pct_change") is not None:
                extra = f" pct_change={src.get('pct_change')}"
            if src.get("pl") is not None:
                extra += f" pl={src.get('pl')}"
            return f"{src[key]}{extra}"[:220]
    return str(src.get("id") or "")[:80]


def _heuristic_narrative(
    candidate: dict[str, Any],
    *,
    force_urls: bool = False,
) -> str:
    """Deterministic multi-lens briefing from structured fields."""
    seed = candidate.get("seed") or {}
    related = candidate.get("related") or []
    entity = candidate.get("entity")
    entity_label = ENTITY_LABELS.get(str(entity or ""), str(entity or "").title() or None)
    parts: list[str] = []

    if candidate.get("kind") == "entity_fusion" and entity_label:
        lenses = ", ".join(LENS_LABELS.get(l, l) for l in (candidate.get("lenses") or []))
        parts.append(
            f"Sinais combinados sobre {entity_label}, cruzando várias fontes "
            f"({lenses})."
        )

    parts.append(_describe_signal(seed))
    for rel in related[:4]:
        parts.append(_describe_signal(rel, prefix="Relacionado"))

    if force_urls:
        for s in candidate.get("sources") or []:
            if s.get("url"):
                parts.append(f"Source: {s['url']}")

    return " ".join(p for p in parts if p)


_DOU_ORGANS = [
    ("Defesa Econômica", "CADE"),
    ("Seguros Privados", "SUSEP"),
    ("Previdência Complementar", "PREVIC"),
    ("Banco Central", "BACEN"),
    ("Valores Mobiliários", "CVM"),
    ("Conselho Nacional de Seguros", "CNSP"),
    ("Conselho Monetário", "CMN"),
]


def _dou_organ(organ: str | None) -> str:
    """Short label for a DOU publishing body (from its hierarchyStr)."""
    text = organ or ""
    for needle, short in _DOU_ORGANS:
        if needle in text:
            return short
    return (text.split("/")[-1] or "DOU")[:40]


def _describe_signal(sig: dict[str, Any], prefix: str = "") -> str:
    lens = sig.get("_lens") or ""
    url = sig.get("url") or ""
    alert = " [NOVO]" if sig.get("is_new") else ""
    head = f"{prefix}: " if prefix else ""

    if lens == "fatos":
        return (
            f"{head}{sig.get('doc_type') or 'Fato Relevante'}{alert}: "
            f"{sig.get('company') or sig.get('name') or 'companhia'} — "
            f"{sig.get('subject') or 'n/d'}. {url}"
        ).strip()
    if lens == "dou":
        organ = _dou_organ(sig.get("organ"))
        return (
            f"{head}Diário Oficial{alert} ({organ}): "
            f"{sig.get('title') or sig.get('subject') or 'ato'}. {url}"
        ).strip()
    if lens == "regulatory" or sig.get("doc_type") or sig.get("subject"):
        return (
            f"{head}Regulatório{alert}: {sig.get('doc_type') or 'documento'} "
            f"{sig.get('number') or ''} — {sig.get('subject') or 'n/d'}. {url}"
        ).strip()
    if lens == "sec" or sig.get("ticker") and sig.get("form"):
        subj = (sig.get("subject") or "").strip()
        if not subj and isinstance(sig.get("text"), str):
            subj = sig["text"][:240].replace("\n", " ").strip()
        subj_s = f" — {subj}" if subj else ""
        return (
            f"{head}Documento SEC{alert}: {sig.get('ticker')} {sig.get('form')} "
            f"({sig.get('company') or ''}) protocolado {sig.get('filed') or ''}"
            f"{subj_s}. {url}"
        ).strip()
    if lens == "ofertas" or sig.get("issuer") or sig.get("security"):
        amt = sig.get("amount")
        amt_s = f" valor={amt}" if amt is not None else ""
        return (
            f"{head}Oferta CVM{alert}: {sig.get('security') or 'instrumento'} por "
            f"{sig.get('issuer') or 'emissor'}; líder {sig.get('leader') or 'n/d'}"
            f"{amt_s}. {url}"
        ).strip()
    if lens == "inf_diario" or sig.get("pl") is not None and sig.get("fund_name"):
        pct = sig.get("pct_change")
        pct_s = f" variação={pct}%" if pct is not None else ""
        return (
            f"{head}Patrimônio de fundo{alert}: {sig.get('fund_name') or sig.get('cnpj')} "
            f"PL={sig.get('pl')}{pct_s} (admin. {sig.get('admin') or 'n/d'}). {url}"
        ).strip()
    if lens == "pix" or (sig.get("ispb") and sig.get("tx_value") is not None):
        return (
            f"{head}Movimentação Pix{alert}: {sig.get('institution') or sig.get('ispb')} "
            f"valor={sig.get('tx_value')} variação={sig.get('pct_change')}. {url}"
        ).strip()
    if lens == "juros" or sig.get("rate_year") is not None:
        return (
            f"{head}Juros{alert}: {sig.get('institution') or sig.get('cnpj8')} "
            f"{sig.get('modality') or ''} taxa_ano={sig.get('rate_year')} "
            f"variação={sig.get('pct_change')}. {url}"
        ).strip()
    if lens == "entrants" or sig.get("entity_type"):
        name = sig.get("name") or sig.get("cnpj") or "entidade"
        brand = sig.get("trade_name")
        who = f"{name} ({brand})" if brand and brand.upper() not in str(name).upper() else name
        lic = sig.get("license_class") or sig.get("entity_type") or "entidade"
        ctrl = f" Controlador: {sig.get('controller')}." if sig.get("controller") else ""
        parents = known_parents(sig)
        plabel = ", ".join(ENTITY_LABELS.get(p, str(p).title()) for p in parents)
        parent_s = f" Vinculado a concorrente conhecido: {plabel}." if parents else ""
        text = f"{head}Novo entrante{alert}: {who} — {lic}.{ctrl}{parent_s}".strip()
        return text.replace("..", ".")
    if lens == "market" or sig.get("share_pct") is not None:
        return (
            f"{head}Participação de mercado: {sig.get('institution')} "
            f"share={sig.get('share_pct')} valor={sig.get('value')}."
        ).strip()
    if sig.get("fund_name"):
        return (
            f"{head}Registro de fundo{alert}: {sig.get('fund_name')} "
            f"(admin. {sig.get('admin') or 'n/d'}). {url}"
        ).strip()
    return f"{head}Sinal{alert} id={sig.get('id')}. {url}".strip()
