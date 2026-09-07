"""#104 (#14 Stage 2) — Receita Federal CNPJ bulk → CNAE-filtered candidate discovery.

The Receita "Estabelecimentos" open-data dump lists every establishment in Brazil with its
principal CNAE. Filtering to the financial-services CNAE ranges (64/65/66) yields companies
that operate in our verticals but are not yet in the registry — Stage 2 of the entity-
discovery pipeline (bulk enrichment), complementing the official-register syncs (Stages 1).

SCOPE / HONESTY: this module is the **parse + filter + CNPJ-dedup propose** core, which is
pure and unit-tested. It is **propose-only** (ADR 011 §4: a CNPJ-only, news-grade candidate
is never auto-created at scale — radar tier `identified`; a curator promotes it). It does NOT
ship a live full-dump fetch: the dump is ~60M rows across multi-GB partitions, which is not
feasible inside the 15-min ingest Lambda. Wiring (below, gated OFF) reads a **pre-staged
partition** (an S3 object / URL holding a single Estabelecimentos CSV, ideally already
CNAE-filtered by an Athena/Glue step). Standing up that staging step is the infra follow-up.

Estabelecimentos layout (documented; `;`-delimited, latin-1, quoted, NO header, ~30 cols):
  0 CNPJ_BASICO 1 CNPJ_ORDEM 2 CNPJ_DV 3 MATRIZ_FILIAL 4 NOME_FANTASIA 5 SITUACAO_CADASTRAL
  6 DATA_SITUACAO 7 MOTIVO 8 CIDADE_EXTERIOR 9 PAIS 10 DATA_INICIO 11 CNAE_PRINCIPAL
  12 CNAE_SECUNDARIA 13.. endereço … (UF near the end). We use the columns we key on by index.
"""
from __future__ import annotations

import csv
import io
from typing import Any, Iterable

# Financial-services CNAE divisions: 64 = serviços financeiros, 65 = seguros/previdência/
# saúde suplementar, 66 = atividades auxiliares (corretoras, gestão de fundos, câmbio…).
FS_CNAE_DIVISIONS = ("64", "65", "66")

# Coarse CNAE-group → Onça industry. Ambiguous groups map to None (curator assigns on review).
# Keyed by the 4-digit CNAE group (chars 0-3, dot stripped). Precision > recall — an unmapped
# FS CNAE still becomes a candidate, just without a proposed industry.
_CNAE_INDUSTRY: dict[str, str] = {
    "6421": "banking", "6422": "banking", "6423": "banking", "6424": "banking",
    "6431": "banking", "6435": "fintech",  # crédito/financiamento (SCFI-like)
    "6438": "banking",
    "6440": "fintech",   # arrendamento mercantil / crédito
    "6461": "investment-banking", "6462": "investment-banking", "6463": "investment-banking",
    "6470": "asset-management",   # fundos de investimento
    "6491": "fintech", "6492": "fintech", "6493": "fintech",
    "6499": "fintech",   # outras atividades de serviços financeiros
    "6511": "insurance", "6512": "insurance",       # seguros de vida / não-vida
    "6520": "insurance",                             # resseguros
    "6530": "closed-pension",                        # previdência complementar (fechada)
    "6541": "insurance", "6542": "insurance", "6550": "insurance",  # capitalização / saúde
    "6611": "financial-data-analytics",             # administração de bolsas/mercados
    "6612": "advisory",   # corretoras/distribuidoras de títulos e valores
    "6613": "acquiring",  # administração de cartões
    "6619": "fintech",    # outras atividades auxiliares
    "6621": "insurance", "6622": "insurance", "6629": "insurance",  # corretagem/aux. seguros
    "6630": "asset-management",   # gestão de fundos
}


def cnae_to_industry(cnae: str | None) -> str | None:
    """Map a CNAE code (any punctuation) to an Onça industry, or None if unmapped/non-FS."""
    digits = "".join(ch for ch in str(cnae or "") if ch.isdigit())
    if len(digits) < 4 or digits[:2] not in FS_CNAE_DIVISIONS:
        return None
    return _CNAE_INDUSTRY.get(digits[:4])


def is_fs_cnae(cnae: str | None) -> bool:
    digits = "".join(ch for ch in str(cnae or "") if ch.isdigit())
    return len(digits) >= 2 and digits[:2] in FS_CNAE_DIVISIONS


def parse_estabelecimentos(
    text: str, *, active_only: bool = True, matriz_only: bool = True,
) -> list[dict[str, Any]]:
    """Parse an Estabelecimentos CSV partition → FS-CNAE candidate records.

    Keeps only financial-services principal CNAEs; by default only ACTIVE (situação 02)
    HEAD offices (matriz) — branches share the base CNPJ and would duplicate. Pure/no network.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    reader = csv.reader(io.StringIO(text), delimiter=";")
    for row in reader:
        if len(row) < 12:
            continue
        basico, ordem, dv = (row[0] or "").strip(), (row[1] or "").strip(), (row[2] or "").strip()
        matriz = (row[3] or "").strip()          # 1 = matriz, 2 = filial
        fantasia = (row[4] or "").strip()
        situacao = (row[5] or "").strip()        # 02 = ativa
        cnae = (row[11] or "").strip()
        if not basico or not is_fs_cnae(cnae):
            continue
        if active_only and situacao != "02":
            continue
        if matriz_only and matriz not in ("1", ""):
            continue
        cnpj = "".join(ch for ch in (basico + ordem + dv) if ch.isdigit())
        if len(cnpj) != 14 or basico in seen:
            continue
        seen.add(basico)
        uf = next((c.strip() for c in row[-6:] if len(c.strip()) == 2 and c.strip().isalpha()), None)
        out.append({
            "id": f"receita:{basico}",
            "source": "Receita-CNPJ-bulk",
            "kind": "competitor",
            "cnpj": cnpj,
            "name": fantasia or None,
            "cnae": cnae,
            "industry": cnae_to_industry(cnae),
            "uf": uf,
            "registry": "receita_estabelecimentos",
            "discovery_source": "receita_bulk",
        })
    return out


def propose_candidates(
    rows: Iterable[dict[str, Any]], *, max_propose: int = 200, table: Any | None = None,
) -> dict[str, Any]:
    """Dedup FS-CNAE candidates against the registry by CNPJ root; PROPOSE the new ones
    (never auto-create — ADR 011 §4). Returns a report. Idempotent by (kind, key)."""
    from src.synth import entity_registry as er

    report: dict[str, Any] = {"seen": 0, "already": 0, "proposed": [], "no_name": 0}
    budget = max_propose
    for r in rows:
        report["seen"] += 1
        root = "".join(ch for ch in str(r.get("cnpj") or "") if ch.isdigit())[:8]
        if not root:
            continue
        if er.resolve_by_cnpj(root, table=table):
            report["already"] += 1
            continue
        if budget <= 0:
            continue
        name = (r.get("name") or "").strip()
        if not name:
            report["no_name"] += 1  # a CNPJ with no trade name → needs the Empresas join first
            continue
        pid = er.propose_review(
            kind="discovery", key=f"receita:{root}", proposed=name,
            reason="receita_cnae",
            hint=f"receita_bulk cnpj={root} cnae={r.get('cnae')} industry={r.get('industry') or '-'} uf={r.get('uf') or '-'}",
            confidence="cnpj",
            payload={"profile": r, "source": "receita_bulk", "cnpj": root,
                     "industry": r.get("industry")},
            table=table)
        report["proposed"].append(pid or root)
        budget -= 1
    return report
