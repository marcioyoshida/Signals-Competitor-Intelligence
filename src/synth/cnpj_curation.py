"""P1 follow-up — curate CNPJ roots onto tracked entities (verified before write).

`watchlist_qsa` (P1) fetches a tracked entity's QSA *by its CNPJ root* from the
registry — but most curated entities were seeded for alias/news resolution and carry
no `cnpj_roots`, so QSA coverage is bounded. This tool fills that gap for the core
Brazilian financial-services entities, and does so **safely**: a candidate CNPJ is
never trusted on my say-so. Each is **verified against BrasilAPI** — the entity's own
registry aliases must actually appear in the fetched razão social / nome fantasia —
before `entity_registry.add_cnpj_roots` writes it. A wrong or transposed CNPJ points to
a different company, fails the name check, and is reported, never written.

Scope: banks, fintechs, payments, insurers, brokers/asset managers, data & consórcio —
where a Brazilian QSA carries real people. Excluded on purpose: offshore betting/crypto
brands (no Brazilian operating CNPJ that matters here) and FII/FIAGRO tickers (funds —
their "QSA" is the administrator, a company, not a control cohort).

`verify` is pure (fetcher injected) so tests need no network. Run:
  python -m src.synth.cnpj_curation            # dry-run: fetch, verify, report
  python -m src.synth.cnpj_curation --apply    # write the verified roots
"""
from __future__ import annotations

import sys
import time
import unicodedata
from typing import Any, Callable

from src.ingest import receita_cnpj

# entity_id -> candidate 8-digit CNPJ root (matriz raiz). Best public knowledge;
# every one is verified against BrasilAPI's razão social before it is written.
SEED_CNPJ_ROOTS: dict[str, str] = {
    # Banks
    "itau": "60701190",
    "bradesco": "60746948",
    "santander": "90400888",
    "bb": "00000000",
    "caixa": "00360305",
    "btg": "30306294",
    "safra": "58160789",
    "original": "92894922",
    "c6": "31872495",
    # Fintechs / payments
    "nubank": "18236120",        # Nu Pagamentos
    "inter": "00416968",         # Banco Inter
    "picpay": "22896431",
    "pagseguro": "08561701",     # PagSeguro Internet
    "stone": "16501555",         # Stone Instituição de Pagamento
    "mercado_pago": "10573521",
    "neon": "20855875",          # Neon Pagamentos
    "creditas": "23361442",
    "infinitepay": "18189547",   # CloudWalk
    "crefisa": "60779196",
    # Insurers
    "porto_seguro": "61198164",
    "caixa_seguridade": "17960987",
    "bb_seguridade": "17344597",
    "icatu": "42283770",         # Icatu Seguros
    # Data & consórcio
    "serasa": "62173620",        # Serasa
    "sinqia": "04065791",
}


def _fold(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).upper()


def _entity_tokens(ent: dict[str, Any]) -> list[str]:
    """Distinctive name tokens (>=4 chars) from the entity's curated aliases."""
    forms = list(ent.get("alias_forms") or []) + list(ent.get("aliases") or [])
    forms.append(ent.get("display_name") or "")
    toks: set[str] = set()
    for f in forms:
        for t in _fold(f).replace("/", " ").split():
            t = t.strip(".,()")
            if len(t) >= 4 and not t.startswith("TICKER:"):
                toks.add(t)
    return sorted(toks)


def verify(
    ent: dict[str, Any],
    root: str,
    *,
    fetcher: Callable[[str | None], dict[str, Any] | None] = receita_cnpj.fetch_cnpj,
) -> dict[str, Any]:
    """Fetch the candidate CNPJ and confirm it is really this entity.

    Match = a distinctive registry alias token appears in the razão social or nome
    fantasia. Returns {ok, root, razao, matched, reason}.
    """
    data = fetcher(root)
    if not data:
        return {"ok": False, "root": root, "razao": None, "matched": None,
                "reason": "fetch_failed"}
    razao = _fold(data.get("razao_social"))
    fant = _fold(data.get("nome_fantasia"))
    matched = next((t for t in _entity_tokens(ent) if t in razao or t in fant), None)
    return {
        "ok": bool(matched),
        "root": root,
        "razao": data.get("razao_social"),
        "matched": matched,
        "reason": "name_match" if matched else "name_mismatch",
    }


def curate(
    seed: dict[str, str] | None = None,
    *,
    fetcher: Callable[[str | None], dict[str, Any] | None] = receita_cnpj.fetch_cnpj,
    apply: bool = False,
    table: Any | None = None,
    pause_sec: float = 0.25,
) -> list[dict[str, Any]]:
    """Verify each seeded CNPJ against BrasilAPI; write the verified ones when apply."""
    from src.synth import entity_registry

    seed = seed if seed is not None else SEED_CNPJ_ROOTS
    report: list[dict[str, Any]] = []
    for entity_id, root in seed.items():
        ent = entity_registry.get_entity(entity_id, table=table)
        if not ent:
            report.append({"entity": entity_id, "ok": False, "reason": "no_entity"})
            continue
        if ent.get("cnpj_roots"):
            report.append({"entity": entity_id, "ok": True, "reason": "already_has_root",
                           "root": ent["cnpj_roots"][0], "written": False})
            continue
        res = verify(ent, root, fetcher=fetcher)
        written = False
        if res["ok"] and apply:
            written = bool(entity_registry.add_cnpj_roots(entity_id, [root], table=table))
        report.append({"entity": entity_id, **res, "written": written})
        if pause_sec:
            time.sleep(pause_sec)
    return report


def _print(report: list[dict[str, Any]]) -> None:
    ok = [r for r in report if r.get("ok")]
    bad = [r for r in report if not r.get("ok")]
    for r in report:
        mark = "✓" if r.get("ok") else "✗"
        extra = (f" root={r.get('root')} matched={r.get('matched')} razao=«{r.get('razao')}»"
                 if r.get("razao") else f" ({r.get('reason')})")
        wrote = " [written]" if r.get("written") else ""
        print(f"  {mark} {r['entity']}{extra}{wrote}")
    print(f"\nverified {len(ok)}/{len(report)} · rejected {len(bad)} "
          f"· written {sum(1 for r in report if r.get('written'))}")


if __name__ == "__main__":  # pragma: no cover
    _print(curate(apply="--apply" in sys.argv))
