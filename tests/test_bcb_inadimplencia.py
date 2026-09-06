"""ADR 022 Tier-2 — inadimplência / NPL (offline, fixture-based)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import bcb_inadimplencia as ina


def _pf_rows(code, overdue, total):
    return [{"CodInst": code, "NomeColuna": "Vencido a Partir de 15 Dias", "Saldo": overdue},
            {"CodInst": code, "NomeColuna": "Total da Carteira de Pessoa Física", "Saldo": total},
            {"CodInst": code, "NomeColuna": "A Vencer em até 90 Dias", "Saldo": 999}]  # ignored


def test_sum_by_code_sums_only_the_named_columns():
    rows = _pf_rows("C1", 10.0, 100.0) + _pf_rows("C1", 5.0, 50.0)  # two modalidade rows
    s = ina._sum_by_code(rows, "Vencido a Partir de 15 Dias", "Total da Carteira de Pessoa Física")
    assert s["C1"]["overdue"] == 15.0 and s["C1"]["total"] == 150.0


def test_compute_npl_combines_pf_pj(monkeypatch):
    def fake_fetch(base_date, rel):
        if rel == ina.REL_PF:
            return [{"CodInst": "C1", "NomeColuna": "Vencido a Partir de 15 Dias", "Saldo": 18.31e9},
                    {"CodInst": "C1", "NomeColuna": "Total da Carteira de Pessoa Física", "Saldo": 588.11e9}]
        return [{"CodInst": "C1", "NomeColuna": "Vencido a Partir de 15 Dias", "Saldo": 4.85e9},
                {"CodInst": "C1", "NomeColuna": "Total da Carteira de Pessoa Jurídica", "Saldo": 584.24e9}]
    monkeypatch.setattr(ina, "_fetch", fake_fetch)
    m = ina.compute_npl(202603)["C1"]
    assert m["npl_pf"] == 3.11 and m["npl_pj"] == 0.83
    assert m["npl_total"] == 1.98             # (18.31+4.85)/(588.11+584.24)
    assert m["carteira_bi"] == 1172.35


def test_npl_bands():
    assert ina.npl_band(12.9) == "elevada" and ina.npl_band(4.8) == "atenção"
    assert ina.npl_band(2.6) == "baixa" and ina.npl_band(None) is None


def test_map_to_entities_resolves_prudencial_and_bands():
    npl = {"C1": {"npl_pf": 12.85, "npl_pj": 14.38, "npl_total": 12.93, "carteira_bi": 193.7}}
    recs = ina.map_to_entities(npl, {"C1": "NUBANK - PRUDENCIAL"},
                               resolver=lambda i: ["nubank"], base_date=202603)
    assert len(recs) == 1 and recs[0]["entity"] == "nubank"
    assert recs[0]["band"] == "elevada" and recs[0]["npl_total"] == 12.93
    proj = ina.npl_by_entity(ina.merge(None, recs))
    assert proj["nubank"]["npl_total"] == 12.93 and proj["nubank"]["band"] == "elevada"
