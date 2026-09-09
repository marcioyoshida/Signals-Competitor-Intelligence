"""#104 (#14 Stage 2) — Receita Federal CNPJ bulk → CNAE-filtered candidate discovery.

The Receita "Estabelecimentos" open-data dump lists every establishment in Brazil with its
principal CNAE. Filtering to the financial-services CNAE ranges (64/65/66) yields companies
that operate in our verticals but are not yet in the registry — Stage 2 of the entity-
discovery pipeline (bulk enrichment), complementing the official-register syncs (Stages 1).

SCOPE / HONESTY: **propose-only** throughout (ADR 011 §4: a CNPJ-only, news-grade candidate is
never auto-created at scale — radar tier `identified`; a curator promotes it). The dump is NOT
one 60M-row monolith — it is split into 10 numbered Estabelecimentos shards (LIVE-verified
2026-09-08); shards 1-9 (~320-350MB compressed each) fit a single Lambda invocation, shard 0
(2.0GB) is excluded. `fetch_shard()` streams ONE shard live (rotating by day-of-year), which is
the DEFAULT path. A pre-staged partition (`ONCA_RECEITA_BULK_KEY`/`URL` — e.g. from a future
Athena/Glue step over the full dataset) still overrides it if configured. Source-honesty note:
the official gov.br download path now redirects through an SSO login a Lambda can't complete,
so the only currently Lambda-reachable host is a **third-party community mirror**
(Cloudflare-cached, updated monthly) — acceptable only because this is propose-only.

Estabelecimentos layout (documented; `;`-delimited, latin-1, quoted, NO header, ~30 cols):
  0 CNPJ_BASICO 1 CNPJ_ORDEM 2 CNPJ_DV 3 MATRIZ_FILIAL 4 NOME_FANTASIA 5 SITUACAO_CADASTRAL
  6 DATA_SITUACAO 7 MOTIVO 8 CIDADE_EXTERIOR 9 PAIS 10 DATA_INICIO 11 CNAE_PRINCIPAL
  12 CNAE_SECUNDARIA 13.. endereço … (UF near the end). We use the columns we key on by index.
"""
from __future__ import annotations

import csv
import io
import re
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
    source: str | Iterable[str], *, active_only: bool = True, matriz_only: bool = True,
) -> list[dict[str, Any]]:
    """Parse an Estabelecimentos CSV partition → FS-CNAE candidate records.

    Keeps only financial-services principal CNAEs; by default only ACTIVE (situação 02)
    HEAD offices (matriz) — branches share the base CNPJ and would duplicate. Pure/no network.

    `source` is either the full text (str — the pre-staged-partition path, small enough to hold
    in memory) or a line-iterable / file-like object (the live shard-fetch path: a decompressed
    Estabelecimentos file can be 1.5GB+, so `csv.reader` iterates it lazily instead of the whole
    thing being materialized as one string first).
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    reader = csv.reader(io.StringIO(source) if isinstance(source, str) else source, delimiter=";")
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


# --- live shard fetch (#104 unblock, 2026-09-08) ----------------------------------------
#
# The dump is NOT one 60M-row monolith — it is 10 numbered Estabelecimentos shards. Shard 0 is
# ~6x larger than the rest (2.0GB compressed vs ~320-350MB) and is excluded so a single shard
# fits a Lambda's ephemeral storage/time budget; shards 1-9 rotate by day-of-year so the full
# roster cycles roughly every 9 daily runs (the dump itself only refreshes monthly).
#
# HONESTY on the source: the official gov.br download path now redirects through an SSO login a
# Lambda cannot complete (confirmed live 2026-09-08 — even the actively-maintained community ETL
# project's own code has to bypass it and hit the origin IP directly). The only currently
# Lambda-reachable host is a THIRD-PARTY community mirror (Cloudflare-cached, updated monthly).
# Acceptable ONLY because this feeds PROPOSE-ONLY discovery (ADR 011 §4) — a stale/wrong mirror
# can at worst miss or misname a review-queue candidate, never corrupt the registry.
_MIRROR_BASE = "https://dados-abertos-rf-cnpj.casadosdados.com.br/arquivos"
_DATE_RE = re.compile(r'href="(\d{4}-\d{2}-\d{2})/?"')
SHARDS = tuple(range(1, 10))


def shard_for_day(day_of_year: int) -> int:
    return SHARDS[day_of_year % len(SHARDS)]


def _http_get_text(url: str) -> str:
    import requests
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def latest_dump_date(*, fetcher: Any = None) -> str | None:
    """Discover the most recent dated dump folder on the mirror index. None on ANY failure
    (network, no dates found) — fail-closed, the caller then skips this run's fetch rather
    than guess a date."""
    try:
        html = (fetcher or _http_get_text)(f"{_MIRROR_BASE}/")
        dates = _DATE_RE.findall(html or "")
        return max(dates) if dates else None
    except Exception as exc:  # pragma: no cover - network best-effort
        print(f"Warning: Receita mirror index unreachable: {exc}")
        return None


def _download_to(url: str, path: str, *, deadline: float | None = None) -> None:
    """Stream a shard zip to disk in chunks, aborting cooperatively at `deadline` (monotonic
    time) rather than waiting out a slow/huge transfer past the source's budget."""
    import time as _time

    import requests
    with requests.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if deadline is not None and _time.monotonic() > deadline:
                    raise TimeoutError(f"Receita shard download exceeded its budget: {url}")
                f.write(chunk)


def fetch_shard(
    shard: int, *, dump_date: str | None = None, dest_dir: str = "/tmp",
    deadline: float | None = None, index_fetcher: Any = None, downloader: Any = None,
) -> list[dict[str, Any]]:
    """Download ONE Estabelecimentos shard from the mirror and parse it into FS-CNAE candidate
    records. Streams the zip to disk (`zipfile` needs a seekable source to read the central
    directory) then streams the CSV member OUT of it lazily via `parse_estabelecimentos` — never
    holds the full decompressed file (1GB+) in memory. Best-effort: any failure (network, disk,
    corrupt zip, deadline exceeded) returns ``[]``, never raises to the ingest caller."""
    import os as _os
    import zipfile

    date = dump_date or latest_dump_date(fetcher=index_fetcher)
    if not date:
        return []
    url = f"{_MIRROR_BASE}/{date}/Estabelecimentos{shard}.zip"
    zpath = _os.path.join(dest_dir, f"receita_estab{shard}.zip")
    try:
        (downloader or _download_to)(url, zpath, deadline=deadline)
        with zipfile.ZipFile(zpath) as zf:
            # RFB names the member an opaque internal code (not "Estabelecimentos{n}.csv") —
            # each shard zip holds exactly one member, so just take it.
            member = zf.namelist()[0]
            with zf.open(member) as raw:
                lines = io.TextIOWrapper(raw, encoding="latin-1", newline="")
                return parse_estabelecimentos(lines)
    except Exception as exc:  # pragma: no cover - network/disk best-effort
        print(f"Warning: Receita shard {shard} ({date}) fetch/parse failed: {exc}")
        return []
    finally:
        try:
            _os.remove(zpath)
        except OSError:
            pass
