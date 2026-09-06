"""ADR 022 pilot — validate the balancete/resultados soundness flow end-to-end, incl.
FinBERT-PT-BR run locally, over a small pilot set of prudential conglomerates.

Two halves, both grounded in REAL BCB IF.data (no fabricated numbers):

  Half 1 (numeric trajectory)  — pull IF.data `Relatorio='T'` across N quarters for the
    pilot prudential-conglomerate codes, extract the balance-sheet lines that define a
    soundness trajectory (Ativo Total, Carteira de Crédito, Depósitos), derive YoY moves
    and the crédito/ativo direction (the leading-indicator slope of ADR 022 §2 / Tier B).
  Half 2 (financial tone)      — run FinBERT-PT-BR (lucas-leme/FinBERT-PT-BR) LOCALLY on
    deterministic pt-BR statements OF THE REAL FIGURES (not scraped filings), producing a
    net tone in [-1, 1] = P(POSITIVE) - P(NEGATIVE). Validates the §3 feature wiring.

This is a *quarterly* pilot: IF.data is quarterly, so it validates the trajectory MECHANICS
and the FinBERT flow. Production Tier B is the genuinely monthly COSIF balancete (doc 4010);
the tone corpus in production is the Pilar 3 / results-release TEXT, not fact-paraphrases.

Run:  HF_HOME=./.hf_cache .venv/bin/python scripts/pilot_soundness_finbert.py
Deps: torch (CPU), transformers, pandas — see the ADR pilot notes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.ingest import bcb_ifdata as ifd  # noqa: E402

# entity_id : (CodInst prudencial, label) — real conglomerate codes ranked by Ativo Total.
PILOT = {
    "itau":          ("C0010069", "Itaú Unibanco"),
    "bancodobrasil": ("C0049906", "Banco do Brasil"),
    "btg":           ("C0049944", "BTG Pactual"),
    "xp":            ("C0052120", "XP"),
    "nubank":        ("C0052058", "Nubank"),
}
QUARTERS = [202506, 202509, 202512, 202603]        # oldest → newest (1yr)
LINES = ["Ativo Total", "Carteira de Crédito", "Depósitos", "Captações"]
MODEL = "lucas-leme/FinBERT-PT-BR"


def _pick_line(colname: str) -> str | None:
    for line in LINES:
        if colname.strip().startswith(line):
            return line
    return None


def build_numeric_trajectory() -> dict:
    codes = {c for c, _ in PILOT.values()}
    data = {c: {line: {} for line in LINES} for c in codes}
    for q in QUARTERS:
        rows = ifd.fetch_institutions(q)
        for r in rows:
            c = r.get("CodInst")
            if c not in codes or r.get("Saldo") is None:
                continue
            line = _pick_line(r.get("NomeColuna") or "")
            if not line:
                continue
            cur = data[c][line].get(q)  # keep headline aggregate: shortest column wins
            if cur is None or len(r["NomeColuna"]) < cur[1]:
                data[c][line][q] = (float(r["Saldo"]), len(r["NomeColuna"]))
        print(f"  fetched {q}: {len(rows)} rows", file=sys.stderr)

    def series(d):  # {q:(val,_)} -> [R$ bi per quarter]
        return [round(d[q][0] / 1e9, 2) if q in d else None for q in QUARTERS]

    def yoy(s):
        return round(100 * (s[-1] - s[0]) / s[0], 1) if s[0] and s[-1] else None

    out = {"quarters": QUARTERS, "unit": "R$ bilhões", "entities": {}}
    for eid, (code, label) in PILOT.items():
        d = data[code]
        ativo, credito, dep = series(d["Ativo Total"]), series(d["Carteira de Crédito"]), series(d["Depósitos"])
        cr_at = [round(100 * credito[i] / ativo[i], 1) if credito[i] and ativo[i] else None
                 for i in range(len(QUARTERS))]
        out["entities"][eid] = {
            "label": label, "cod_inst": code,
            "ativo_total": ativo, "carteira_credito": credito, "depositos": dep,
            "credito_sobre_ativo_pct": cr_at,
            "traj_ativo_yoy_pct": yoy(ativo), "traj_credito_yoy_pct": yoy(credito),
            "traj_credito_ativo_delta_pp": (round(cr_at[-1] - cr_at[0], 1)
                                            if cr_at[0] is not None and cr_at[-1] is not None else None),
        }
    return out


def _facts_ptbr(e: dict) -> list[str]:
    """Deterministic pt-BR statements of the REAL figures — not scraped filings."""
    a, c, dpp = e["traj_ativo_yoy_pct"], e["traj_credito_yoy_pct"], e["traj_credito_ativo_delta_pp"]
    fs = []
    if a is not None:
        fs.append(f"O ativo total {'cresceu' if a >= 0 else 'recuou'} {abs(a):.1f}% em doze meses.")
    if c is not None:
        fs.append(f"A carteira de crédito {'expandiu' if c >= 0 else 'contraiu'} {abs(c):.1f}% no período.")
    if dpp is not None:
        d = "aumentou" if dpp > 0 else ("reduziu" if dpp < 0 else "manteve-se")
        fs.append(f"A relação crédito sobre ativo {d} {abs(dpp):.1f} ponto(s) percentual(is), "
                  f"indicando {'maior' if dpp > 0 else 'menor'} exposição ao risco de crédito.")
    return fs


def add_financial_tone(traj: dict) -> dict:
    from transformers import pipeline
    clf = pipeline("text-classification", model=MODEL, top_k=None)

    def tone(sentences):
        nets = []
        for s in sentences:
            sc = {d["label"]: d["score"] for d in clf(s)[0]}
            nets.append(sc.get("POSITIVE", 0.0) - sc.get("NEGATIVE", 0.0))
        return round(sum(nets) / len(nets), 3) if nets else None

    for e in traj["entities"].values():
        fs = _facts_ptbr(e)
        e["tone_facts_ptbr"] = fs
        e["financial_tone_net"] = tone(fs)      # [-1, 1]
    return traj


def main() -> None:
    traj = build_numeric_trajectory()
    traj = add_financial_tone(traj)
    out_path = Path("/tmp/pilot_combined.json")
    out_path.write_text(json.dumps(traj, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'entity':16} {'ativoΔ%':>7} {'crédΔ%':>7} {'cr/at pp':>8} {'tone':>6}")
    for e in traj["entities"].values():
        print(f"{e['label']:16} {e['traj_ativo_yoy_pct']!s:>7} {e['traj_credito_yoy_pct']!s:>7} "
              f"{e['traj_credito_ativo_delta_pp']!s:>8} {e['financial_tone_net']!s:>6}")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
