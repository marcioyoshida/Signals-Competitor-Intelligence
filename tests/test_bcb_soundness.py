"""ADR 022 Tier A — BCB prudential solvency ingester (offline, fixture-based)."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import bcb_soundness as snd

# Long-format capital rows as IF.data returns them (índices are FRACTIONS in Saldo).
_ROWS = [
    {"CodInst": "C0010069", "NomeColuna": "Índice de Basileia  (n) = (e) / (j)", "Saldo": 0.1477},
    {"CodInst": "C0010069", "NomeColuna": "Índice de Capital Nível I  (m) = (c) / (j)", "Saldo": 0.134},
    {"CodInst": "C0010069", "NomeColuna": "Índice de Capital Principal  (l) = (a) / (j)", "Saldo": 0.1197},
    {"CodInst": "C0010069", "NomeColuna": "Razão de Alavancagem  (o) = (c) / (k)", "Saldo": 0.0648},
    {"CodInst": "C0010069", "NomeColuna": "Índice de Imobilização  (p)", "Saldo": 0.2074},
    {"CodInst": "C0052475", "NomeColuna": "Índice de Basileia  (n) = (e) / (j)", "Saldo": 0.1193},
    {"CodInst": "C0052475", "NomeColuna": "Ativos Ponderados pelo Risco (RWA)", "Saldo": 999.0},  # ignored
    {"CodInst": "C0099999", "NomeColuna": "Índice de Imobilização  (p)", "Saldo": 0.5},  # no Basileia
]
_NAMES = {"C0010069": "ITAU - PRUDENCIAL", "C0052475": "XP - PRUDENCIAL", "C0099999": "SOMEONE - PRUDENCIAL"}


def test_extract_scales_fraction_to_percent_and_ignores_other_columns():
    s = snd.extract_solvency(_ROWS)
    assert s["C0010069"]["indice_basileia"] == 14.77       # 0.1477 → %
    assert s["C0010069"]["capital_nivel_i"] == 13.4
    assert s["C0010069"]["indice_imobilizacao"] == 20.74
    assert "rwa" not in s["C0052475"] and s["C0052475"]["indice_basileia"] == 11.93


def test_band_thresholds():
    assert snd.soundness_band(15.0) == "sólido"
    assert snd.soundness_band(11.93) == "atenção"
    assert snd.soundness_band(10.4) == "frágil"
    assert snd.soundness_band(-5.0) == "frágil"   # negative-capital micro-institution
    assert snd.soundness_band(None) is None


def _resolver(item):
    return {"ITAU": ["itau"], "XP": ["xp"]}.get((item.get("institution") or "").upper(), [])


def test_map_to_entities_strips_prudencial_suffix_and_resolves():
    recs = snd.map_to_entities(snd.extract_solvency(_ROWS), _NAMES,
                               resolver=_resolver, base_date=202603, today=dt.date(2026, 9, 6))
    by = {r["entity"]: r for r in recs}
    assert set(by) == {"itau", "xp"}                       # SOMEONE has no Basileia → dropped
    assert by["itau"]["indice_basileia"] == 14.77 and by["itau"]["band"] == "sólido"
    assert by["xp"]["band"] == "atenção" and by["xp"]["cod_inst"] == "C0052475"
    assert by["itau"]["id"] == "bcb-soundness:itau"


def test_map_skips_unresolved():
    recs = snd.map_to_entities(snd.extract_solvency(_ROWS), _NAMES, resolver=lambda i: [])
    assert recs == []


def test_map_keeps_highest_basileia_on_duplicate_entity():
    rows = [
        {"CodInst": "A", "NomeColuna": "Índice de Basileia", "Saldo": 0.12},
        {"CodInst": "B", "NomeColuna": "Índice de Basileia", "Saldo": 0.16},
    ]
    names = {"A": "ITAU FIN - PRUDENCIAL", "B": "ITAU - PRUDENCIAL"}
    recs = snd.map_to_entities(snd.extract_solvency(rows), names, resolver=lambda i: ["itau"])
    assert len(recs) == 1 and recs[0]["indice_basileia"] == 16.0


def test_merge_and_projection():
    recs = snd.map_to_entities(snd.extract_solvency(_ROWS), _NAMES, resolver=_resolver, base_date=202603)
    idx = snd.merge(None, recs, today=dt.date(2026, 9, 6))
    assert idx["count"] == 2 and idx["as_of"] == "2026-09-06"
    proj = snd.soundness_by_entity(idx)
    assert proj["xp"]["indice_basileia"] == 11.93 and proj["xp"]["band"] == "atenção"
    assert proj["itau"]["base_date"] == 202603
    # merge is upsert: a newer record replaces the entity in place
    idx2 = snd.merge(idx, [{"entity": "xp", "indice_basileia": 12.5, "band": "atenção"}])
    assert idx2["count"] == 2 and idx2["records"]["xp"]["indice_basileia"] == 12.5


def test_summarize_weakest_first():
    recs = snd.map_to_entities(snd.extract_solvency(_ROWS), _NAMES, resolver=_resolver, base_date=202603)
    s = snd.summarize(recs)
    assert s["kind"] == "prudential_solvency" and s["total"] == 2
    assert s["weakest"][0]["entity"] == "xp"   # lowest Basileia first


def test_merge_carries_base_date():
    recs = snd.map_to_entities(snd.extract_solvency(_ROWS), _NAMES, resolver=_resolver, base_date=202603)
    assert snd.merge(None, recs)["base_date"] == 202603
    # a later merge whose records omit base_date keeps the prior one
    idx = snd.merge(snd.merge(None, recs), [{"entity": "xp", "indice_basileia": 12.5}])
    assert idx["base_date"] == 202603


def test_run_noops_when_quarter_unchanged(monkeypatch):
    monkeypatch.setattr(snd, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(snd, "load_index", lambda bucket, s3=None: {"base_date": 202603, "count": 5})
    # fetch must NOT be called on a no-op
    monkeypatch.setattr(snd, "fetch_capital", lambda b: (_ for _ in ()).throw(AssertionError("fetched")))
    out = snd.run(bucket="b")
    assert out["status"] == "noop" and out["base_date"] == 202603 and out["records"] == 5


def test_run_fetches_when_quarter_changed(monkeypatch):
    monkeypatch.setattr(snd, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(snd, "load_index", lambda bucket, s3=None: {"base_date": 202512, "count": 5})
    monkeypatch.setattr(snd, "fetch_capital", lambda b: _ROWS)
    monkeypatch.setattr(snd, "fetch_institution_names", lambda b: _NAMES)
    monkeypatch.setattr("src.synth.entities.resolve_entities",
                        lambda item: _resolver(item))
    captured = {}
    monkeypatch.setattr(snd, "update_store",
                        lambda recs, bucket, s3=None, today=None: captured.update(n=len(recs)))
    out = snd.run(bucket="b")
    assert out["status"] == "ok" and out["base_date"] == 202603 and out["mapped"] == 2
    assert captured["n"] == 2
