"""ADR 022 Tier-1 — competitor fundamentals (offline, fixture-based)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import bcb_fundamentals as f

# raw R$ (Basileia as fraction); month 202603 → YTD 3 months → annualise ×4
_ROWS = [
    {"CodInst": "C1", "NomeColuna": "Ativo Total", "Saldo": 100e9},
    {"CodInst": "C1", "NomeColuna": "Carteira de Crédito", "Saldo": 50e9},
    {"CodInst": "C1", "NomeColuna": "Captações", "Saldo": 80e9},
    {"CodInst": "C1", "NomeColuna": "Lucro Líquido", "Saldo": 2e9},        # YTD 3mo
    {"CodInst": "C1", "NomeColuna": "Patrimônio Líquido", "Saldo": 10e9},
    {"CodInst": "C1", "NomeColuna": "Índice de Basileia", "Saldo": 0.15},
    {"CodInst": "C1", "NomeColuna": "Patrimônio de Referência para Comparação com o RWA  (e)", "Saldo": 9e9},  # not PL
    {"CodInst": "C2", "NomeColuna": "Ativo Total", "Saldo": 50e9},
    {"CodInst": "C2", "NomeColuna": "Carteira de Crédito", "Saldo": 50e9},
    {"CodInst": "C2", "NomeColuna": "Lucro Líquido", "Saldo": -1e9},        # loss
    {"CodInst": "C2", "NomeColuna": "Patrimônio Líquido", "Saldo": 5e9},
]
_NAMES = {"C1": "ALFA - PRUDENCIAL", "C2": "BETA - PRUDENCIAL"}


def test_extract_avoids_pr_collision_with_pl():
    v = f.extract_lines(_ROWS)["C1"]
    assert v["pl"] == 10e9 and v["ativo"] == 100e9 and v["basileia"] == 0.15
    # 'Patrimônio de Referência…' must NOT be captured as pl


def test_ratios_annualise_and_derive():
    recs = f.map_to_entities(f.extract_lines(_ROWS), _NAMES,
                             resolver=lambda i: [i["institution"].lower().split()[0]], base_date=202603)
    a = {r["entity"]: r for r in recs}["alfa"]
    assert a["roe_pct"] == 80.0            # 2/10=20% ×4 (YTD annualise)
    assert a["roa_pct"] == 8.0             # 2/100=2% ×4
    assert a["leverage"] == 10.0           # 100/10
    assert a["credito_captacoes_pct"] == 62.5   # 50/80
    assert a["basileia_headroom_pp"] == 4.5     # 15 − 10.5
    assert a["carteira_share_pct"] == 50.0      # 50 of (50+50) total
    assert a["lucro_share_pct"] == 100.0        # only ALFA has positive lucro (2 of 2)


def test_negative_roe_kept():
    recs = f.map_to_entities(f.extract_lines(_ROWS), _NAMES,
                             resolver=lambda i: [i["institution"].lower().split()[0]], base_date=202603)
    beta = {r["entity"]: r for r in recs}["beta"]
    assert beta["roe_pct"] == -80.0        # -1/5=-20% ×4


def test_projection_and_merge():
    recs = f.map_to_entities(f.extract_lines(_ROWS), _NAMES,
                             resolver=lambda i: [i["institution"].lower().split()[0]], base_date=202603)
    idx = f.merge(None, recs)
    assert idx["base_date"] == 202603 and idx["count"] == 2
    proj = f.fundamentals_by_entity(idx)
    assert proj["alfa"]["roe_pct"] == 80.0 and "leverage" in proj["alfa"]
