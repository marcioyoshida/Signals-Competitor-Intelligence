"""ADR 019 Phase 1 — declarative source & lens registry (descriptive).

Single source of truth for each ingestion source and each candidates lens. Phase 1 is
**non-breaking**: ``candidates.py`` derives its lens-policy sets (``LENS_WEIGHT``,
``HIGH_VALUE_SOLO_LENSES``, ``STRUCTURED_SUBJECT_LENSES``, ``BACKDROP_LENSES``) and its
section→lens list from here, reproducing the previously hand-maintained frozensets exactly.
Later phases drive the ``lambda_port`` pipeline loop and vertical selection from the same
specs (see ``docs/2026-08-31-adr-source-registry-verticals.md``).

Two specs, deliberately split (a small refinement of the ADR's single-spec sketch):
  - :class:`LensSpec` — the *lens policy* (weight + which lens-sets it belongs to). Lens policy
    is a property of the LENS, and several sources can share a lens (``competitor`` and
    ``fiagro_moves`` both feed ``funds``), so it lives once on the lens, not per source.
  - :class:`SourceSpec` — a *source*: its digest-section key → lens, plus ingestion metadata
    (resolution / delta / integration / cadence / verticals / gating) that Phases 2–4 consume.
    Phase 1 uses only ``section_key`` + ``lens``; the rest is forward-looking, with defaults.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# Verticals (ADR 019). A vertical is the market a deployment serves; the same codebase runs
# as Onça (financial-services) or as the Anteater sectorial product by setting ONCA_VERTICAL.
VERTICAL_FS = "financial-services"
# Anteater sectorial verticals — first-class here (Phase 4) so the sectorial product is a
# CONFIG of this codebase, not a fork. Each folds in fully by (a) its industry slugs in
# VERTICAL_INDUSTRIES below + entity_registry.INDUSTRIES, (b) a seeded sector entity universe,
# (c) any sector-specific SourceSpecs (verticals={<sector>}). Until (a)/(b) are populated a
# sector fails closed (empty feed) — it never leaks FS data.
SECTORIAL_VERTICALS = ("pharma", "health", "logistics", "retail", "energy", "telecom", "agro")
KNOWN_VERTICALS = (VERTICAL_FS, *SECTORIAL_VERTICALS)
ALL = "all"


def is_known_vertical(vertical: str) -> bool:
    return vertical in KNOWN_VERTICALS


@dataclass(frozen=True)
class LensSpec:
    """Scoring/clustering policy for one candidates lens."""
    name: str
    weight: float                    # -> LENS_WEIGHT
    solo: bool = False               # -> HIGH_VALUE_SOLO_LENSES (a lone NEW signal can surface)
    structured_subject: bool = False  # -> STRUCTURED_SUBJECT_LENSES (fuse only on shared entity)
    backdrop: bool = False           # -> BACKDROP_LENSES (never a solo seed)


@dataclass(frozen=True)
class SourceSpec:
    """One ingestion source: its digest section → lens, plus ingestion metadata."""
    id: str                          # digest section key (e.g. "cade", "pix_moves")
    lens: str                        # candidates lens this section feeds
    # --- forward-looking (ADR 019 Phases 2–4; NOT consumed in Phase 1) ---
    resolution: str = "text"         # "cnpj" | "name" | "prebound" | "macro" | "text"
    delta: str = "new"               # "new" | "moves" | "store" | "none"
    integration: str = "lens"        # "lens" (digest section + candidates) | "store" (index.json)
    cadence_days: int = 1
    verticals: frozenset[str] = field(default_factory=lambda: frozenset({VERTICAL_FS}))
    default_on: bool = True
    env_flag: str | None = None      # existing ONCA_* override, when a source is env-gated
    # Phase 2 — the digest section the source emits (consumed by the registry-driven runner):
    label: str | None = None         # human budget label; falls back to id
    state_key: str | None = None      # delta seen-set key (DynamoDbState); falls back to id
    seed_if_empty: bool = True       # suppress the first-run flood (False = report all on seed)
    items_limit: int = 10            # _tag_new(new[:items_limit])
    context_limit: int = 15          # _strip_raw(records[:context_limit])


# --- Lens policy — reproduces candidates.py's LENS_WEIGHT + the three *_LENSES sets ---------
LENSES: dict[str, LensSpec] = {
    "regulatory": LensSpec("regulatory", 0.35, solo=True),
    "cvm_normas": LensSpec("cvm_normas", 0.33, solo=True),  # #194 CVM legislação/notícias
    "antitrust":  LensSpec("antitrust", 0.33, solo=True),                       # #61 CADE
    "sanctions":  LensSpec("sanctions", 0.32, solo=True, structured_subject=True),  # #60
    "fatos":      LensSpec("fatos", 0.30, solo=True, structured_subject=True),
    "dou":        LensSpec("dou", 0.30, solo=True),
    "sec":        LensSpec("sec", 0.25, solo=True, structured_subject=True),
    "ofertas":    LensSpec("ofertas", 0.20, solo=True, structured_subject=True),
    "contracts":  LensSpec("contracts", 0.20, solo=True, structured_subject=True),  # #62 PNCP
    "entrants":   LensSpec("entrants", 0.18, solo=True, structured_subject=True),
    "funds":      LensSpec("funds", 0.15, solo=True, structured_subject=True),
    "inf_diario": LensSpec("inf_diario", 0.15, structured_subject=True),
    "pix":        LensSpec("pix", 0.12, structured_subject=True),
    "juros":      LensSpec("juros", 0.12, structured_subject=True),
    "news":       LensSpec("news", 0.12),
    "market":     LensSpec("market", 0.08, backdrop=True),
}

# --- Sources — reproduces _collect_signals' (section_key, lens) list, IN ORDER --------------
# Order matters: _collect_signals dedups by item id (first section wins), so preserve it.
# `verticals={ALL}` marks the sector-agnostic sources (they also serve the Anteater verticals);
# the rest are the financial-services vertical's current implementations.
SOURCES: list[SourceSpec] = [
    SourceSpec("regulatory", "regulatory", items_limit=8, context_limit=12,
               state_key="bcb_normativos", label="BCB normativos", seed_if_empty=False),
    SourceSpec("competitor", "funds", resolution="cnpj", items_limit=8, context_limit=12,
               state_key="cvm_fundos", label="CVM funds", seed_if_empty=False),
    SourceSpec("new_entrants", "entrants", resolution="cnpj", items_limit=8),
    SourceSpec("ofertas", "ofertas", resolution="cnpj",
               state_key="cvm_ofertas", label="CVM ofertas"),
    SourceSpec("fatos", "fatos", items_limit=12),
    SourceSpec("dou", "dou", label="Diário Oficial"),
    SourceSpec("cvm_normas", "cvm_normas", items_limit=8, context_limit=12,                  # #194
               state_key="cvm_normas", label="CVM normas/sancionador", seed_if_empty=False),
    SourceSpec("sanctions", "sanctions", resolution="cnpj", verticals=frozenset({ALL}),     # #60
               integration="store", state_key="ceis_cnep", env_flag="ONCA_CEIS_CNEP",
               label="CEIS/CNEP sanctions"),
    SourceSpec("cade", "antitrust", resolution="name", verticals=frozenset({ALL}),          # #61
               state_key="cade", env_flag="ONCA_CADE", label="CADE antitrust"),
    SourceSpec("contracts", "contracts", resolution="cnpj", integration="store",            # #62
               default_on=False, env_flag="ONCA_PNCP_CONTRATOS", verticals=frozenset({ALL}),
               state_key="pncp_contratos", label="PNCP contracts"),
    SourceSpec("news", "news", resolution="text"),
    SourceSpec("sec_filings", "sec"),
    SourceSpec("pix_moves", "pix", delta="moves"),
    SourceSpec("juros_moves", "juros", delta="moves"),
    SourceSpec("inf_diario_moves", "inf_diario", delta="moves"),
    # FIAGRO agri-funds moves reuse the "funds" lens (deliberately — not a new lens), so one
    # material NEW move scores/alerts like a fresh CVM fund-class filing.
    SourceSpec("fiagro_moves", "funds", delta="moves", resolution="prebound"),
    SourceSpec("market", "market", resolution="macro"),
]

SPECS: dict[str, SourceSpec] = {s.id: s for s in SOURCES}


def by_id(source_id: str) -> SourceSpec:
    return SPECS[source_id]


# --- Vertical taxonomy (ADR 019 Phase 3b) --------------------------------------------------
# A vertical's in-scope industry slugs — the feed is scoped to these so a sectorial deployment
# publishes only its own market. ``None`` = all industries (no scoping): the financial-services
# vertical spans the entire entity_registry.INDUSTRIES taxonomy, so Onça needs no feed scoping.
# Sectorial verticals (pharma/health/logistics/retail/energy/telecom/agro) register their
# industry sets here when the Anteater deployment is folded in (Phase 4).
VERTICAL_INDUSTRIES: dict[str, "frozenset[str] | None"] = {
    VERTICAL_FS: None,
    # Sectorial verticals: their industry slugs are populated when the sector's entities are
    # seeded (Phase 4 fold-in). Empty for now ⇒ fail-closed (empty feed), never an FS leak.
    **{v: frozenset() for v in SECTORIAL_VERTICALS},
}


def vertical_industries(vertical: str) -> "frozenset[str] | None":
    """In-scope industry slugs for ``vertical`` — ``None`` means all (no feed scoping).
    Unknown verticals fail closed to an empty set (publish nothing) rather than leak."""
    return VERTICAL_INDUSTRIES.get(vertical, frozenset())


# --- Derived views (consumed by candidates.py in Phase 1) -----------------------------------
def lens_weight() -> dict[str, float]:
    return {name: spec.weight for name, spec in LENSES.items()}


def solo_lenses() -> frozenset[str]:
    return frozenset(n for n, s in LENSES.items() if s.solo)


def structured_subject_lenses() -> frozenset[str]:
    return frozenset(n for n, s in LENSES.items() if s.structured_subject)


def backdrop_lenses() -> frozenset[str]:
    return frozenset(n for n, s in LENSES.items() if s.backdrop)


def section_lens_pairs() -> list[tuple[str, str]]:
    """(digest section key, lens) in order — the _collect_signals `sections` list."""
    return [(s.id, s.lens) for s in SOURCES]


def active(vertical: str | None = None) -> list[SourceSpec]:
    """Sources applicable to ``vertical`` (Phase 3 gate). ``None`` = all sources."""
    if not vertical:
        return list(SOURCES)
    return [s for s in SOURCES if ALL in s.verticals or vertical in s.verticals]


# --- Industry topic registry (#173 / #175 / #176) ---------------------------------------------
# One declarative place, per COVERED INDUSTRY, for the three things that make a sector-wide
# measure visible when it names no operator (MP 1.394, the 2026-09-25 online-betting ban, was
# missed because every query was an ENTITY name):
#   - ``dou_phrases``    DOU quoted-phrase searches (dou.fetch_dou ``topic_terms``). A hit carries
#                        ``industries`` and no entity. Scoped to the NORMATIVE issuers below, so a
#                        phrase like "instituições financeiras" can't flood the DOU lens with
#                        routine COAF/CVM/SUSEP acts (those still arrive via competitor terms).
#   - ``news_queries``   Google News RSS queries (trade_press.fetch_sector_news), ≤3 per industry,
#                        run OUTSIDE ONCA_NEWS_MAX_TERMS. Tuned live 2026-09-27 (see #176): each
#                        returned sector-level headlines in the last 7 days.
#   - ``vocabulary``     the terms that say "this text is about this industry" — used by the
#                        federal-acts classifier (#175) to map an act to industries, and by the
#                        sector-news relevance filter (a headline must contain one).
# Vocabulary syntax (matched on accent-folded lowercase text, word-bounded at both ends):
#   "phrase"   literal phrase;   "stem*"  word-prefix (``seguradora*`` → seguradoras);
#   "re:<rx>"  a raw regex on the folded text (for the few terms that need a lookahead).
# Keep phrases SPECIFIC: every DOU phrase is one HTTP per run and returns ≤20 acts.

#: Organs whose topic-phrase hits are kept (substring of DOU ``hierarchyStr``; a trailing ``$``
#: means the organ must be EXACTLY that — "Presidência da República" alone is the despachos
#: organ, while "Presidência da República/Casa Civil/ABIN" is not a sector-wide issuer).
#: An entry ``"organ@DO1:Resolução,Circular"`` (#189) keeps only that organ's acts published in
#: a DO1 edition (DO1 + DO1_EXTRA_*) whose doc type starts with one of the listed types: the
#: sector regulators' RULES (Resolução Susep 96/97, Portaria Previc 728, RN ANS 679) without
#: their per-entity DO1 Portarias (authorizations, plan approvals) or DO2/DO3 notices.
NORMATIVE_ISSUERS: tuple[str, ...] = (
    "Atos do Poder Executivo",                       # MPs, Decretos
    # #188: LAWS are published under the legislative organs, not the Executive — LC 237
    # (resseguro, 2026-09-15, DO1_EXTRA_C) was invisible. Conversion/expiry of an MP arrives here.
    "Atos do Poder Legislativo",                     # Leis, Leis Complementares
    "Atos do Congresso Nacional",                    # Atos Declaratórios (MP prorrogação/vigência)
    "Presidência da República$",                     # despachos / mensagens / vetos
    "Ministério da Fazenda/Gabinete do Ministro",    # Portarias MF
    "Conselho Monetário Nacional",                   # CMN resolutions
    "Conselho Nacional de Seguros Privados",         # CNSP
    "Conselho Nacional de Previdência Complementar",  # CNPC
    "Secretaria de Prêmios e Apostas",               # SPA (betting regulator)
    # #189: the sector regulators' own normative DO1 acts
    "Superintendência de Seguros Privados@DO1:Resolução,Circular",
    "Previdência Complementar/Diretoria de Normas@DO1:Portaria,Resolução,Instrução",
    "Superintendência Nacional de Previdência Complementar$@DO1:Resolução,Instrução",
    "Comissão de Valores Mobiliários@DO1:Resolução,Instrução,Deliberação,Ofício",
    "Banco Central do Brasil/Diretoria Colegiada@DO1:Resolução,Instrução,Circular",
    "Agência Nacional de Saúde Suplementar@DO1:Resolução",
    "Controle de Atividades Financeiras@DO1:Resolução,Instrução",
    "Unidade de Inteligência Financeira@DO1:Resolução,Instrução",
)

#: #188: explicit watch on primary acts whose FATE arrives months later, outside the DOU
#: lookback (an MP lives 60+60 days; its conversion law or the Congress's Ato Declaratório of
#: expiry cites it by number). Each phrase is searched as a topic term (scoped to
#: NORMATIVE_ISSUERS) until ``until``, and is passed to federal_acts as a known critical
#: instrument so every act citing it inherits ``industries``. (instrument key, phrase,
#: industries, until ISO date).
WATCHED_ACTS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("mp 1.394", "Medida Provisória nº 1.394", ("betting",), "2027-03-31"),   # online-betting ban
    ("mp 1.393", "Medida Provisória nº 1.393", ("banking", "fintech"), "2027-03-31"),  # credit programme
)


def watched_acts(today: "str | None" = None) -> list[tuple[str, str, list[str]]]:
    """The WATCHED_ACTS still live on ``today`` (ISO; default: now) as (key, phrase, industries)."""
    import datetime as _dt

    t = today or _dt.date.today().isoformat()
    return [(k, p, list(i)) for k, p, i, until in WATCHED_ACTS if t <= until]


@dataclass(frozen=True)
class IndustryTopicSpec:
    """Per-industry topic declarations (see the block comment above)."""
    industry: str                      # entity_registry.INDUSTRIES slug
    dou_phrases: tuple[str, ...] = ()
    news_queries: tuple[str, ...] = ()  # ≤ 3
    vocabulary: tuple[str, ...] = ()
    env_flag: str | None = None        # extra ONCA_* toggle for this industry's DOU phrases


#: #195: government consumer-credit programmes (MP 1.393 Desenrola Brasil 3.0, Desenrola
#: Adimplentes) — renegotiation run by the LENDERS, so banking + fintech, not securitization.
_CREDIT_PROGRAMMES: tuple[str, ...] = (
    "desenrola", "renegociacao de dividas", "credito responsavel", "concessao responsavel de credito",
    "tomadores de credito", "superendividamento")

INDUSTRY_TOPICS: list[IndustryTopicSpec] = [
    IndustryTopicSpec(
        "betting",
        # The four pre-#175 betting DOU terms (lambda_port's hard-coded dou_topics), unchanged.
        # + the singular "aposta de quota fixa" (#175, live 2026-09-27): the in.gov.br search is
        # not stemmed, and the singular is what SPA's normative Portarias use (2.750, 2.596).
        # Bare "apostas" was measured and rejected: 20/20 results, the 9 in scope are the same
        # acts these phrases already catch, the rest is MJSP "JOGOS" rating noise.
        dou_phrases=("Secretaria de Prêmios e Apostas", "apostas de quota fixa",
                     "aposta de quota fixa", "Lei nº 14.790", "jogos de azar"),
        news_queries=("bets proibição", "apostas regulamentação", "Secretaria de Prêmios e Apostas"),
        vocabulary=("aposta* de quota fixa", "loteria* de aposta*", "quota fixa", "bets", "bet",
                    "apostas esportivas", "apostas online", "apostas on-line", "apostas virtuais",
                    "casa* de aposta*", "site* de aposta*", "plataforma* de aposta*",
                    "mercado de apostas", "setor de apostas", "jogo* de azar", "jogo* on-line",
                    "jogo* online", "igaming", "cassino* online", "lei 14.790", "lei no 14.790",
                    "secretaria de premios e apostas", "spa/mf", "sigap", "apostador*"),
        env_flag="ONCA_DOU_BETTING",
    ),
    IndustryTopicSpec(
        "banking",
        dou_phrases=("instituições financeiras", "Sistema Financeiro Nacional"),
        # Tuned live 2026-09-27 (the prior pair yielded 0 kept headlines in 30 days: CMN
        # headlines never name the sector, and the gate is right to drop them).
        # These three return Brazilian bank-rule news: BC deadlines/accounting rules, the
        # STJ consignado refunds, obligations placed on banks.
        news_queries=("instituições financeiras Banco Central", "BC determina bancos",
                      "consignado regra bancos"),
        # "bancos" excludes CENTRAL banks (Fed/ECB/Vietnam stories) and non-financial banks
        # ("bancos de sangue/alimentos/leite"), the two noise sources measured live.
        vocabulary=("instituicoes financeiras", "instituicao financeira",
                    "re:\\bbancos\\b(?! centra| de sangue| de alimento| de leite| de dados| de horas)",
                    "banco multiplo",
                    "bancos multiplos", "setor bancario", "sistema bancario", "tarifas bancarias",
                    "sistema financeiro nacional",
                    # NOT "CMN": the council rules for every FS industry, so naming it says nothing
                    # about WHICH industry an act touches (a CMN FIDC rule is not a banking act).
                    "depositos a vista", "recolhimento compulsorio", "compulsorio*", "febraban",
                    "open finance", "basileia") + _CREDIT_PROGRAMMES,
    ),
    IndustryTopicSpec(
        "fintech",
        dou_phrases=("instituições de pagamento", "arranjos de pagamento"),
        news_queries=("Pix Banco Central nova regra", "fintechs regulação",
                      "instituições de pagamento Banco Central"),
        vocabulary=("instituic* de pagamento", "arranjo* de pagamento", "transac* de pagamento",
                    "conta* de pagamento", "iniciador* de pagamento", "moeda eletronica", "fintech*",
                    "pix", "sociedade* de credito direto", "sociedade* de emprestimo entre pessoas",
                    "banking as a service", "baas") + _CREDIT_PROGRAMMES,
    ),
    IndustryTopicSpec(
        "insurance",
        # #189: + the issuers' own act names (Resolução Susep 96/97 were invisible) and the
        # ANS's Resoluções Normativas (health insurers are in "insurance").
        dou_phrases=("seguros privados", "resseguro", "Resolução Susep", "Resolução CNSP",
                     "Resolução Normativa ANS"),
        news_queries=("Susep regras seguros", "seguradoras Susep"),
        vocabulary=("re:\\bseguros?\\b(?!-desemprego|-defeso| desemprego| defeso)",
                    "seguradora*", "resseguro*", "resseguradora*", "susep", "cnsp", "seguros privados",
                    "titulos de capitalizacao", "capitalizacao", "previdencia complementar aberta",
                    "corretor* de seguros", "mercado segurador",
                    "saude suplementar", "resolucao normativa ans", "operadora* de plano* de saude", "plano* privado* de assistencia a saude"),
    ),
    IndustryTopicSpec(
        # Securitização & Crédito (credit originators, FIDC/CRI/CRA, consignado)
        "securitization",
        dou_phrases=("crédito consignado", "securitização"),
        news_queries=("FIDC regras", "crédito consignado regras"),
        vocabulary=("credito consignado", "consignado", "consignados", "securitiza*",
                    "direitos creditorios", "fidc", "fidcs", "certificado* de recebiveis",
                    "cessao de credito", "credito rotativo", "juros do rotativo",
                    "cadastro positivo", "recebiveis"),
        # #195: the consumer-credit PROGRAMME terms (Desenrola, renegociação de dívidas, crédito
        # responsável, superendividamento) moved to banking + fintech (_CREDIT_PROGRAMMES): MP 1.393
        # is run by the lenders, and tagging it securitization-only (0 active entities) hid it.
    ),
    IndustryTopicSpec(
        "asset-management",
        dou_phrases=("fundos de investimento",),
        news_queries=("fundos de investimento CVM regras",),
        vocabulary=("fundo* de investimento*", "gestora* de recursos", "gestao de recursos",
                    "administracao de carteira*", "administradora* de carteira*", "fundos exclusivos",
                    "come-cotas", "anbima", "resolucao cvm 175", "industria de fundos", "cotista*"),
    ),
    IndustryTopicSpec(
        "crypto",
        dou_phrases=("ativos virtuais", "criptoativos"),
        news_queries=("criptoativos regulação", "criptoativos Banco Central"),
        vocabulary=("ativo* virtua*", "criptoativo*", "criptomoeda*", "cripto", "stablecoin*",
                    "bitcoin", "prestadora* de servicos de ativos virtuais", "psav", "psavs",
                    "exchange* de cripto*", "mercado cripto", "lei 14.478", "lei no 14.478"),
    ),
    IndustryTopicSpec(
        "consorcio",
        dou_phrases=("administradoras de consórcio", "Lei nº 11.795"),
        news_queries=("administradoras de consórcio", "consórcio Banco Central regras"),
        # NOT bare "consórcio": in the DOU it is overwhelmingly the PUBLIC consortium
        # (Consórcio Interfederativo / intermunicipal) or a bidding consortium (live 2026-09-27).
        vocabulary=("administradora* de consorcio*", "grupo* de consorcio*", "sistema de consorcio*",
                    "cota* de consorcio*", "carta* de credito", "lei 11.795", "lei no 11.795", "abac",
                    "re:\\bconsorcios\\b(?! public\\w*| intermunicip\\w*| interfederativ\\w*)"),
    ),
    IndustryTopicSpec(
        "closed-pension",
        dou_phrases=("previdência complementar", "Portaria Previc", "Resolução CNPC"),  # #189
        news_queries=("fundos de pensão Previc", "previdência complementar Previc"),
        vocabulary=("previdencia complementar fechada", "re:\\bprevidencia complementar\\b(?! aberta)",
                    "entidade* fechada* de previdencia", "efpc*", "fundo* de pensao", "previc",
                    "cnpc", "conselho nacional de previdencia complementar",
                    "lei complementar 109", "lei complementar no 109"),
    ),
    # --- #195: the 8 covered industries that had no spec (audit R8). -------------------------
    # DOU phrases were replayed live 2026-09-27 (30-day window, delta=75 pages, organ allowlist +
    # NORMATIVE_ISSUERS incl. the #189 doc-type scopes). Every candidate for these industries
    # kept 0 acts: their DOU hits are per-entity CVM Atos Declaratórios, BCB editais, CADE
    # despachos and CARF pautas — not sector rules — so they carry NO dou_phrases (dead config
    # costs one HTTP per run). Measured: "fundos de investimento imobiliário" 10 raw/0 kept,
    # "Lei nº 8.668" 0 (2 kept in 12 months), "Lei nº 14.130" 0, "Fiagro" 0, "distribuidoras de
    # títulos e valores mobiliários" 28/0, "corretoras de títulos e valores mobiliários" 7/0,
    # "credenciadoras" 75/0 (all non-FS), "consultoria de valores mobiliários" 19/0, "Fundos de
    # Investimento em Participações" 7/0, "entidades registradoras" 1/0 (3 kept in 12 months).
    # Their sector rules arrive via the BCB normativos source, the CMN/CVM/BCB issuer phrases and
    # the full-text lead; the VOCABULARY below is what tags them.
    IndustryTopicSpec(
        "real-estate-funds",
        news_queries=("fundos imobiliários CVM regras",),
        vocabulary=("fundo* de investimento imobiliario*", "fundo* imobiliario*", "fii", "fiis",
                    "lei 8.668", "lei no 8.668"),
    ),
    IndustryTopicSpec(
        "agri-funds",
        news_queries=("Fiagro regras",),
        vocabulary=("fiagro*", "cadeias produtivas agroindustriais", "lei 14.130", "lei no 14.130"),
    ),
    IndustryTopicSpec(
        # DTVMs / corretoras (reg_coverage maps BCB + CVM intermediaries here) + underwriting
        "investment-banking",
        news_queries=("corretoras DTVM Banco Central",),
        vocabulary=("distribuidora* de titulos e valores mobiliarios", "dtvm", "dtvms",
                    "corretora* de titulos e valores mobiliarios", "corretora* de cambio, titulos e valores",
                    "ctvm", "corretora* de valores", "banco* de investimento",
                    "oferta* publica* de distribuicao", "coordenador* lider*", "underwriting"),
    ),
    IndustryTopicSpec(
        # credenciadoras (maquininhas). NOT bare "credenciadora": in the DOU it is overwhelmingly
        # an accreditation body (health, education, inspection) — 75/75 non-FS hits, live.
        "acquiring",
        news_queries=("credenciadoras maquininhas Banco Central",),
        vocabulary=("credenciadora* de cart*", "credenciador* de estabelecimento*",
                    "instituic* de pagamento credenciador*", "subcredenciador*", "adquirencia",
                    "maquininha*", "credenciadoras de pagamento*"),
    ),
    IndustryTopicSpec(
        # consultoria de valores mobiliários (CVM Res. 19) — cvm_participantes "consultores"
        "advisory",
        news_queries=("consultoria de valores mobiliários CVM",),
        vocabulary=("consultor* de valores mobiliarios", "consultoria de valores mobiliarios",
                    "analista* de valores mobiliarios", "resolucao cvm 19", "resolucao cvm no 19"),
    ),
    IndustryTopicSpec(
        "wealth-management",
        news_queries=("assessores de investimento CVM",),
        vocabulary=("gestao de patrimonio*", "wealth management", "private banking", "family office*",
                    "assessor* de investimento*", "assessoria* de investimento*", "carteira* administrada*",
                    "resolucao cvm 178", "resolucao cvm no 178"),
    ),
    IndustryTopicSpec(
        # FIP / private equity / venture capital
        "private-markets",
        news_queries=("FIP private equity regras",),
        vocabulary=("fundo* de investimento em participac*", "fip", "fips", "private equity",
                    "venture capital", "capital de risco", "capital empreendedor"),
    ),
    IndustryTopicSpec(
        # market infrastructure: registradoras, bolsa/depositária, credit bureaus
        "financial-data-analytics",
        news_queries=("registradoras de recebíveis Banco Central",),
        vocabulary=("entidade* registradora*", "registradora* de recebiveis", "registro de recebiveis",
                    "infraestrutura* do mercado financeiro", "infraestrutura* de mercado financeiro",
                    "bolsa* de valores", "depositario central", "central depositaria",
                    "gestor* de banco* de dados", "biro* de credito", "bureau* de credito"),
    ),
]

# --- Cross-industry compliance tags (#195) ----------------------------------------------------
# A compliance topic is NOT an industry: an AML/CFT rule (Res. BCB 588 amending Circular 3.978)
# binds every supervised institution at once. federal_acts tags the act ``compliance_tags``
# from its LEAD (same vocabulary syntax as above) and treats the tag as coverage for severity,
# so the rule is no longer "low: no covered industry". DOU phrases: none — "lavagem de dinheiro"
# (19 raw / 4 kept) and "financiamento do terrorismo" (13 / 5) replayed live 2026-09-27 kept
# only acts the betting/pension/BCB routes already deliver (MP 1.394, SPA 2.750/2.596,
# Previc 728, BCB 588).


@dataclass(frozen=True)
class ComplianceTopicSpec:
    tag: str
    label: str                          # pt-BR display
    vocabulary: tuple[str, ...] = ()


COMPLIANCE_TOPICS: list[ComplianceTopicSpec] = [
    ComplianceTopicSpec(
        "aml", "PLD/FT (prevenção à lavagem de dinheiro)",
        vocabulary=("lavagem de dinheiro", "re:\\blavagem\\W{0,2} ou ocultacao", "ocultacao de bens",
                    "financiamento do terrorismo", "financiamento da proliferacao de armas",
                    "pld/ft*", "pld-ft*", "prevencao a lavagem", "prevencao da lavagem",
                    "coaf", "conselho de controle de atividades financeiras",
                    "unidade de inteligencia financeira", "lei 9.613", "lei no 9.613",
                    "circular 3.978", "circular no 3.978"),
    ),
]
COMPLIANCE: dict[str, ComplianceTopicSpec] = {c.tag: c for c in COMPLIANCE_TOPICS}


def compliance_vocabulary() -> dict[str, list[str]]:
    """{compliance tag: [vocabulary terms]} (same syntax as the industry vocabulary)."""
    return {c.tag: list(c.vocabulary) for c in COMPLIANCE_TOPICS if c.vocabulary}

TOPICS: dict[str, IndustryTopicSpec] = {t.industry: t for t in INDUSTRY_TOPICS}
MAX_NEWS_QUERIES_PER_INDUSTRY = 3


def _topics_for(vertical: str | None) -> list[IndustryTopicSpec]:
    """Topic specs in scope for ``vertical`` (None / FS = every declared industry)."""
    scope = vertical_industries(vertical) if vertical else None
    return [t for t in INDUSTRY_TOPICS if scope is None or t.industry in scope]


def dou_topic_terms(vertical: str | None = None,
                    enabled: "Callable[[IndustryTopicSpec], bool] | None" = None) -> dict[str, list[str]]:
    """{DOU phrase: [industry slugs]} for dou.fetch_dou(topic_terms=...). A phrase declared by
    several industries maps to all of them. ``enabled(spec)`` gates an industry (env toggles)."""
    out: dict[str, list[str]] = {}
    for t in _topics_for(vertical):
        if enabled is not None and not enabled(t):
            continue
        for p in t.dou_phrases:
            out.setdefault(p, [])
            if t.industry not in out[p]:
                out[p].append(t.industry)
    return out


def dou_topic_organs(vertical: str | None = None) -> dict[str, list[str]]:
    """{DOU phrase: organ scopes} — every topic phrase is scoped to NORMATIVE_ISSUERS."""
    return {p: list(NORMATIVE_ISSUERS) for p in dou_topic_terms(vertical)}


def news_topic_queries(vertical: str | None = None) -> dict[str, list[str]]:
    """{industry: [news queries]} (≤ MAX_NEWS_QUERIES_PER_INDUSTRY each)."""
    return {t.industry: list(t.news_queries[:MAX_NEWS_QUERIES_PER_INDUSTRY])
            for t in _topics_for(vertical) if t.news_queries}


def industry_vocabulary(vertical: str | None = None) -> dict[str, list[str]]:
    """{industry: [vocabulary terms]} (syntax in the block comment above)."""
    return {t.industry: list(t.vocabulary) for t in _topics_for(vertical) if t.vocabulary}
