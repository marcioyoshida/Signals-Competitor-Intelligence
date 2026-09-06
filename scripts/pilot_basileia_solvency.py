"""ADR 022 pilot — extract Basileia/solvency (and probe liquidity) from the IF.data flow.

Finding (this script proves it on real data):

  SOLVENCY — fully extractable from IF.data **Relatório 5 "Informações de Capital"**
    (TipoInstituicao=1): Índice de Basileia, Índice de Capital Nível I (Tier 1), Índice de
    Capital Principal (CET1), Razão de Alavancagem, Índice de Imobilização, plus the full RWA
    breakdown (crédito / mercado / operacional). Values are stored as fractions → ×100 for %.
    A solvency `band` is derived from the regulatory floor (8% + 2.5% conservation buffer ≈
    10.5% practical minimum). This is Tier-A of ADR 022 §1.

  LIQUIDITY — **NOT in IF.data.** No relatório carries an LCR/NSFR/liquidez column (verified:
    the Relatório catalog has no liquidity report, and Resumo/Capital have no liquidity field).
    True liquidity (LCR/NSFR) is published by BCB separately and appears in the banks' Pilar 3
    reports → it comes from ADR 022 Phase 6 (Pilar 3 PDFs via the Bedrock+KB path) or a dedicated
    LCR source, NOT this flow. A balance-sheet liquidity *proxy* (TVM+disponibilidades vs passivo)
    is possible but must be labelled inference, never presented as the LCR.

NB the prudential-conglomerate CodInst under TipoInstituicao=1 (capital report) DIFFER from the
TipoInstituicao=2 codes used by the market-share flow — resolve against the '<NAME> - PRUDENCIAL'
cadastro entries, do not reuse the tipo=2 codes.

Run:  .venv/bin/python scripts/pilot_basileia_solvency.py
"""
from __future__ import annotations

import json
import re
import sys
import time

import requests

BASE = "https://olinda.bcb.gov.br/olinda/servico/IFDATA/versao/v1/odata"
QUARTERS = [202509, 202512, 202603]
# entity_id : (CodInst prudencial in Relatório 5, label) — '<NAME> - PRUDENCIAL' cadastro entries.
PILOT = {
    "itau": ("C0080099", "Itaú"), "bradesco": ("C0080075", "Bradesco"),
    "santander": ("C0080185", "Santander"), "btg": ("C0080336", "BTG Pactual"),
    "xp": ("C0082475", "XP"), "nubank": ("C0084693", "Nubank"),
}
METRICS = {
    "indice_basileia": "Índice de Basileia",
    "capital_nivel_i": "Índice de Capital Nível I",
    "capital_principal": "Índice de Capital Principal",
    "razao_alavancagem": "Razão de Alavancagem",
    "indice_imobilizacao": "Índice de Imobilização",
}


def _get(path: str, tries: int = 8) -> list[dict]:
    """IF.data OData is flaky (transient 500s) and wraps JSON in /* */ — retry + unwrap."""
    for i in range(tries):
        r = requests.get(f"{BASE}/{path}", timeout=180, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            time.sleep(3 + 2 * i)
            continue
        b = re.sub(r"^\s*/\*|\*/\s*$", "", r.text.strip()).strip()
        if not b:
            time.sleep(3 + 2 * i)
            continue
        return json.loads(b).get("value", [])
    raise RuntimeError(f"IF.data failed after {tries}: {path[:60]}")


def _capital(anomes: int) -> list[dict]:
    return _get(f"IfDataValores(AnoMes=@A,TipoInstituicao=@T,Relatorio=@R)"
                f"?@A={anomes}&@T=1&@R='5'&$format=json")


def _colmatch(nome: str) -> str | None:
    for key, pref in METRICS.items():
        if (nome or "").strip().startswith(pref):
            return key
    return None


def _band(basileia_pct: float | None) -> str | None:
    if basileia_pct is None:
        return None
    if basileia_pct < 10.5:   # below floor + conservation buffer
        return "frágil"
    if basileia_pct < 13.0:
        return "atenção"
    return "sólido"


def main() -> None:
    codes = {c for c, _ in PILOT.values()}
    by_code = {c: eid for eid, (c, _) in PILOT.items()}
    traj = {eid: {m: {} for m in METRICS} for eid in PILOT}
    for q in QUARTERS:
        rows = _capital(q)
        for r in rows:
            c = r.get("CodInst")
            if c not in codes:
                continue
            m = _colmatch(r.get("NomeColuna"))
            if m and r.get("Saldo") is not None:
                traj[by_code[c]][m][q] = round(float(r["Saldo"]) * 100, 2)  # fraction → %
        print(f"  fetched Relatório 5 {q}: {len(rows)} rows", file=sys.stderr)

    out = {"quarters": QUARTERS,
           "source": "BCB IF.data Relatório 5 'Informações de Capital' (TipoInstituicao=1)",
           "liquidity": "NOT available in IF.data — see Pilar 3 (ADR 022 Phase 6) or a dedicated LCR source",
           "entities": {}}
    print(f"\n{'entity':12}{'Basileia%':>10}{'Tier1%':>8}{'CET1%':>7}{'Alav%':>7}{'Imob%':>7}  band     trajetória Basileia (Δpp)")
    for eid, (code, label) in PILOT.items():
        t = traj[eid]
        first, last = QUARTERS[0], QUARTERS[-1]
        bas_series = [t["indice_basileia"].get(q) for q in QUARTERS]
        bas = t["indice_basileia"].get(last)
        dpp = (round(bas - t["indice_basileia"][first], 2)
               if bas is not None and t["indice_basileia"].get(first) is not None else None)
        out["entities"][eid] = {
            "label": label, "cod_inst": code,
            "latest": {m: t[m].get(last) for m in METRICS},
            "basileia_series": bas_series, "basileia_delta_pp_yr": dpp,
            "solvency_band": _band(bas),
        }
        g = lambda m: t[m].get(last)  # noqa: E731
        print(f"{label:12}{bas!s:>10}{g('capital_nivel_i')!s:>8}{g('capital_principal')!s:>7}"
              f"{g('razao_alavancagem')!s:>7}{g('indice_imobilizacao')!s:>7}  {str(_band(bas)):8} {bas_series} Δ{dpp}")
    with open("/tmp/pilot_basileia.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print("\nwrote /tmp/pilot_basileia.json")


if __name__ == "__main__":
    main()
