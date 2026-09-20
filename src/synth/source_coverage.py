"""Source-coverage roadmap — every acquisition route Onça has, in one table.

The war room's "Fontes" rail tab. Modelled on Griffon's Coverage panel (one row per
source, a status pill, how it's ingested, and a measured number), with one addition
Griffon doesn't need: a **roadmap** column. Onça's ADRs have spent real effort deciding
that particular routes are dead — GDELT at 7.4% entity match, Google Search barred by
`robots.txt`, the general MZiQ adapter at 6 workbooks against a threshold of 20 — and
that knowledge currently lives only in `docs/` and in commit messages. A route that was
measured and rejected is a *result*, and it belongs on the board next to the live ones,
with its number attached, so it is not re-proposed.

Two design rules, both learned the hard way in this repo:

1. **Every metric is measured from the built feed, never asserted here.** A hand-written
   "134 institutions" would rot the first time an ingester changed. `LIVE` rows declare
   *where to count*, not *what the count is*.
2. **`live` means reaching the product, not merely deployed.** #145 found
   `cvm_financials.py` switched off in the deployed stack while every doc described it as
   shipped, and #150 found the monthly pipeline dead for weeks behind a linear Step
   Functions chain. So a row whose measured count is zero is rendered as a defect
   (`silent`), not quietly as "live".

Pure/no I/O: `build(feed)` takes the finished feed and returns the block. Called from
`feed_builder.lambda_handler` next to the ADR-018 integrity audit.
"""
from __future__ import annotations

from typing import Any

# Status vocabulary. Order matters — it is the render order and the tally order.
STATUSES = ("live", "silent", "gated", "planned", "rejected")

#: Human labels for the status pills (pt-BR — the war room's language).
STATUS_LABEL = {
    "live": "no ar",
    "silent": "sem dados",
    "gated": "desligada",
    "planned": "planejada",
    "rejected": "avaliada e recusada",
}

# Groups, in render order.
GROUPS = ("sinais", "fundamentos", "registro", "avaliadas")
GROUP_LABEL = {
    "sinais": "Sinais (3×/dia)",
    "fundamentos": "Fundamentos financeiros (mensal)",
    "registro": "Registro de entidades",
    "avaliadas": "Rotas avaliadas",
}


class Row(dict):
    """One source. A dict so it serialises straight into feed.json."""

    def __init__(self, id: str, label: str, group: str, status: str, *,
                 method: str = "—", cadence: str = "—", note: str = "",
                 issue: str | None = None, lens: str | None = None,
                 entity_field: str | None = None, run_source: str | None = None,
                 feed_list: str | None = None, metric: str | None = None):
        super().__init__(
            id=id, label=label, group=group, status=status, method=method,
            cadence=cadence, note=note, issue=issue, lens=lens,
            entity_field=entity_field, run_source=run_source, feed_list=feed_list,
            metric=metric,
        )


# --- The roadmap ---------------------------------------------------------------------
# `lens` joins to feed.source_health (narrative freshness per lens).
# `run_source` joins to feed.source_runs (per-ingester run telemetry, #76).
# `entity_field` counts feed entities carrying that block — the coverage number that
# matters for a financial source: how many competitors actually get the metric.
# `feed_list` counts a top-level feed list, for sources that publish rows rather than
# per-entity blocks (CVM statements, reputation).
ROADMAP: list[Row] = [
    # --- Sinais: the 3x/day lens pipeline ------------------------------------------
    Row("news", "Imprensa e trade press", "sinais", "live", lens="news",
        run_source="Trade press", method="RSS direto", cadence="3×/dia",
        note="19 feeds BR diretos (Valor, Brazil Journal, NeoFeed, Exame/InfoMoney)"),
    Row("regulatory", "BCB normativos", "sinais", "live", lens="regulatory",
        run_source="BCB normativos", method="API", cadence="3×/dia"),
    Row("fatos", "CVM fatos relevantes", "sinais", "live", lens="fatos",
        run_source="CVM fatos relevantes", method="IPE", cadence="3×/dia"),
    Row("dou", "Diário Oficial da União", "sinais", "live", lens="dou",
        run_source="Diário Oficial", method="API", cadence="3×/dia"),
    Row("competitor", "CVM fundos", "sinais", "live", lens="funds",
        run_source="CVM funds", method="CSV", cadence="3×/dia"),
    Row("ofertas", "CVM ofertas", "sinais", "live", lens="ofertas",
        run_source="CVM ofertas", method="CSV", cadence="3×/dia"),
    Row("inf_diario_moves", "CVM informe diário", "sinais", "live", lens="inf_diario",
        run_source="CVM Informe Diário", method="CSV", cadence="3×/dia"),
    Row("new_entrants", "Entrantes regulados", "sinais", "live", lens="entrants",
        run_source="BCB autorizações", method="API", cadence="3×/dia",
        note="BCB autorizações + SUSEP + SPA + PREVIC"),
    Row("pix_moves", "BCB Pix", "sinais", "live", lens="pix",
        run_source="BCB Pix", method="API", cadence="3×/dia"),
    Row("juros_moves", "BCB juros médios", "sinais", "live", lens="juros",
        run_source="BCB juros médios", method="API", cadence="3×/dia"),
    Row("market", "Macro (Copom/Focus)", "sinais", "live", lens="market",
        run_source="BCB macro", method="API", cadence="3×/dia"),
    Row("sec_filings", "SEC EDGAR", "sinais", "live", lens="sec",
        run_source="SEC EDGAR", method="API", cadence="3×/dia"),
    Row("sanctions", "CEIS/CNEP sanções", "sinais", "live", lens="sanctions",
        run_source="CEIS/CNEP sanctions", method="CSV", cadence="3×/dia", issue="60"),
    Row("cade", "CADE atos de concentração", "sinais", "live", lens="antitrust",
        run_source="CADE antitrust", method="scrape", cadence="3×/dia", issue="61"),
    Row("fiagro_moves", "FIAGRO / agri-funds", "sinais", "live", lens="funds",
        run_source="FIAGRO moves", method="CSV", cadence="3×/dia"),
    Row("contracts", "PNCP contratos públicos", "sinais", "gated", lens="contracts",
        method="API", cadence="3×/dia", issue="62",
        note="construída e default-OFF (ONCA_PNCP_CONTRATOS) — volume alto, valor por confirmar"),
    Row("consumidor_gov", "consumidor.gov.br", "sinais", "gated",
        method="API credenciada", cadence="—", issue="63",
        note="token-gated; a credencial emitida foi recusada pelo endpoint — inerte"),

    # --- Fundamentos: the monthly OncaFinancialsPipeline ----------------------------
    Row("balancete", "COSIF balancete (doc 4010)", "fundamentos", "live",
        entity_field="balancete", method="CSV mensal", cadence="mensal", issue="149",
        note="resolução CNPJ-first; COSIF escreve BANCO como BCO"),
    Row("resultados", "COSIF resultados — custo de crédito e eficiência", "fundamentos",
        "live", entity_field="resultados", method="CSV mensal", cadence="mensal",
        issue="146", note="PDD líquida ÷ carteira, anualizada; dois portões de denominador"),
    Row("fundamentals", "IF.data fundamentos (ROE/ROA/alavancagem)", "fundamentos", "live",
        entity_field="fundamentals", run_source="IF.data market", method="API",
        cadence="trimestral"),
    Row("soundness", "BCB solvência (Índice de Basileia)", "fundamentos", "live",
        entity_field="soundness", method="CSV", cadence="trimestral"),
    Row("inadimplencia", "BCB inadimplência (15+ dias, PF/PJ)", "fundamentos", "live",
        entity_field="inadimplencia", method="CSV", cadence="mensal"),
    Row("pilar3_km1", "Pilar 3 KM1 (LCR/NSFR)", "fundamentos", "live",
        entity_field="pilar3_km1", method="PDF/XLSX", cadence="trimestral",
        note="liquidez não existe no IF.data"),
    Row("financial_tone", "Tom financeiro (FinBERT-PT-BR)", "fundamentos", "live",
        entity_field="financial_tone", method="SageMaker batch", cadence="mensal"),
    Row("cvm_financials", "CVM DFP/ITR (demonstrações auditadas)", "fundamentos", "live",
        feed_list="financials", method="pacote CSV", cadence="trimestral", issue="145",
        note="438 emissores; PL tem de casar pelo RÓTULO — CD_CONTA fixo lê Provisões"),
    Row("market_share", "IF.data participação de mercado", "fundamentos", "live",
        entity_field="market_share_pct", run_source="IF.data market", method="API",
        cadence="trimestral"),

    # --- Registro: the entity universe ----------------------------------------------
    Row("entity_discovery", "Descoberta de entidades", "registro", "live",
        run_source="entities auto-create", method="derivada", cadence="3×/dia",
        metric="entities", note="ADR 011 — duas verticais, portão de qualidade de nome"),
    Row("receita_qsa", "Receita QSA (controladores)", "registro", "live",
        run_source="Receita QSA", method="bulk", cadence="sob demanda"),
    Row("receita_cnae", "Receita bulk CNAE", "registro", "live",
        run_source="Receita bulk CNAE", method="bulk", cadence="sob demanda",
        note="estoura o orçamento de 480s com frequência — ver telemetria"),
    Row("consorcio", "BCB consórcio (roster)", "registro", "live",
        run_source="entity discovery consórcio", method="OLINDA", cadence="3×/dia",
        issue="46"),
    Row("fii", "CVM FII (roster)", "registro", "live",
        run_source="entity discovery FII", method="CSV", cadence="3×/dia"),

    # --- Avaliadas: routes measured and decided. The number IS the finding. ----------
    Row("gdelt", "GDELT GKG", "avaliadas", "rejected", method="BigQuery",
        metric="7,4% de correspondência de entidades em PT",
        note="GKG é metadado de documento, não carrega demonstrações; V2.1Amounts não "
             "vincula emissor/linha/período. Mede-se e não serve para PT-BR."),
    Row("google_search", "Google Search / CSE", "avaliadas", "rejected", method="—",
        metric="robots.txt: Disallow /search",
        note="devolve links, não demonstrações; a Custom Search API é paga e limitada. "
             "search_expansion (Tavily) já cobre o único papel útil, descoberta de URL."),
    Row("mziq_geral", "MZiQ — adaptador geral de RI", "avaliadas", "rejected",
        method="API de catálogo", issue="148",
        metric="6 planilhas confirmadas · critério de corte era 20",
        note="população endereçável (20 no teto) é MENOR do que o que já se ingere por "
             "COSIF (134/mês) e CVM (438/tri), e os 6 emissores já são cobertos por ambos"),
    Row("mziq_curado", "MZiQ — 6 emissores curados (NIM, eficiência, Basileia)", "avaliadas",
        "planned", method="API de catálogo", cadence="trimestral", issue="152",
        note="métricas que NÃO existem em COSIF nem na CVM; escopo honesto é curado, "
             "não geral — e o custo-eficiência do COSIF é 2,15× o valor arquivado"),
    Row("mziq_api", "MZiQ — API de catálogo aberta", "avaliadas", "live",
        method="POST apicatalog.mziq.com", metric="sem token · 20 anos de histórico",
        issue="147", note="o 401 da ADR 028 era artefato de método: responde POST, "
                          "recusa GET. Reutilizável para qualquer documento de RI."),
    Row("x_twitter", "X / Twitter", "avaliadas", "planned", method="API paga",
        note="ADR 008 — desenhada, não construída"),
    Row("jucesp", "JUCESP (juntas comerciais)", "avaliadas", "planned", method="scrape",
        note="backlog — sem rota estável"),
]


def _entity_counts(feed: dict[str, Any]) -> dict[str, int]:
    """How many feed entities carry each financial block. This is the coverage number a
    reader cares about: not 'the ingester ran' but 'how many competitors got the metric'."""
    out: dict[str, int] = {}
    for e in feed.get("entities") or []:
        if not isinstance(e, dict):
            continue
        for k, v in e.items():
            if v not in (None, {}, [], ""):
                out[k] = out.get(k, 0) + 1
    return out


def build(feed: dict[str, Any], *, n_entities: int | None = None) -> dict[str, Any]:
    """Attach measured numbers to the declared roadmap and tally it.

    `n_entities` is the registry size (not the feed's), passed in by the caller so this
    module stays I/O-free.
    """
    health = {r.get("lens"): r for r in (feed.get("source_health") or [])
              if isinstance(r, dict)}
    runs = {r.get("source"): r for r in (feed.get("source_runs") or [])
            if isinstance(r, dict)}
    ecount = _entity_counts(feed)
    n_feed_entities = len(feed.get("entities") or [])

    rows: list[dict[str, Any]] = []
    for spec in ROADMAP:
        r = dict(spec)
        status = r["status"]
        metric, band, stale = r.get("metric"), None, None

        if r.get("lens") and r["lens"] in health:
            h = health[r["lens"]]
            metric = metric or "%d narrativas" % (h.get("docs") or 0)
            stale = h.get("staleness_days")
        if r.get("feed_list"):
            n = len(feed.get(r["feed_list"]) or [])
            metric = "%d registros" % n
            if status == "live" and n == 0:
                status = "silent"
        if r.get("entity_field"):
            n = ecount.get(r["entity_field"], 0)
            metric = "%d de %d entidades" % (n, n_feed_entities)
            # #145/#150: a financial source that reaches nobody is broken, not quiet.
            if status == "live" and n == 0:
                status = "silent"
        if r.get("metric") == "entities" and n_entities is not None:
            metric = "%d entidades no registro" % n_entities
        if r.get("run_source") and r["run_source"] in runs:
            rr = runs[r["run_source"]]
            band = rr.get("band")
            # Run telemetry OUTRANKS the lens proxy for staleness, and must never be
            # masked by it. They measure different things: the lens proxy sees how fresh
            # this source's NARRATIVES are, so a source that stopped running still looks
            # fresh for as long as its old narratives sit in the window. That is the exact
            # blind spot #76 built this telemetry to cover — Trade press last succeeded 11
            # days ago while its lens read 0 days stale.
            if rr.get("staleness_days") is not None:
                stale = rr["staleness_days"]
            # Telemetry outranks the declaration: an erroring ingester is not "live".
            if status == "live" and band == "error":
                status = "silent"

        r.update(status=status, status_label=STATUS_LABEL[status],
                 metric=metric or "—", band=band, staleness_days=stale)
        rows.append(r)

    rows.sort(key=lambda o: (GROUPS.index(o["group"]) if o["group"] in GROUPS else 9,
                             STATUSES.index(o["status"]) if o["status"] in STATUSES else 9,
                             o["label"].lower()))
    tally = {s: sum(1 for o in rows if o["status"] == s) for s in STATUSES}
    tally = {k: v for k, v in tally.items() if v}
    return {
        "rows": rows,
        "tally": tally,
        "groups": [{"key": g, "label": GROUP_LABEL[g],
                    "n": sum(1 for o in rows if o["group"] == g)} for g in GROUPS],
        # The headline: routes actually delivering, over routes that exist at all.
        "n_total": len(rows),
        "n_live": tally.get("live", 0),
        "n_attention": tally.get("silent", 0),
    }
