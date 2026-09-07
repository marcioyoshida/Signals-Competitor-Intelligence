"""#77 (E1) — SUSEP supervised-entities registry parser (insurance new-entrant feed)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import susep_entidades as se

# The SES file ships with a LEADING BLANK LINE before the header (live-verified 2026-09-06).
_CSV = (
    "\n"
    "CodigoFIP;NomeEntidade;CNPJ\n"
    "01007;SABEMI SEGURADORA S.A.;87163234000138\n"
    "02682;BTG Pactual Vida e Previdência S.A.;19449767000120\n"
    "03000;IRB BRASIL RESSEGUROS S.A.;33376989000191\n"
    "04000;BRASILCAP CAPITALIZACAO S.A.;15138043000105\n"
    "05000;SEM CNPJ SEGURADORA;\n"
)


def test_parse_skips_leading_blank_and_reads_columns():
    rows = se.parse_entities(_CSV)
    assert len(rows) == 5
    r = rows[0]
    assert r["name"] == "SABEMI SEGURADORA S.A." and r["cnpj"] == "87163234000138"
    assert r["codigo_fip"] == "01007" and r["source"] == "SUSEP-Entidades"
    assert r["is_fintech"] is False and r["situation"] == "supervisionada"


def test_class_inference():
    by = {r["name"]: r["license_class"] for r in se.parse_entities(_CSV)}
    assert by["IRB BRASIL RESSEGUROS S.A."] == "Resseguradora"
    assert by["BRASILCAP CAPITALIZACAO S.A."] == "Capitalização"
    assert by["BTG Pactual Vida e Previdência S.A."] == "Previdência aberta (EAPC)"
    assert by["SABEMI SEGURADORA S.A."] == "Seguradora"


def test_stable_id_prefers_cnpj_then_codigo():
    rows = {r["name"]: r for r in se.parse_entities(_CSV)}
    assert rows["SABEMI SEGURADORA S.A."]["id"] == "susep-ent:87163234000138"
    # no CNPJ → falls back to CodigoFIP (still a stable delta key)
    assert rows["SEM CNPJ SEGURADORA"]["id"] == "susep-ent:05000"
    assert rows["SEM CNPJ SEGURADORA"]["cnpj"] is None


def test_dedup_and_missing_name_skipped():
    dup = _CSV + "01007;SABEMI SEGURADORA S.A.;87163234000138\n;;\n"
    rows = se.parse_entities(dup)
    assert len(rows) == 5  # duplicate CNPJ collapsed, nameless row skipped
