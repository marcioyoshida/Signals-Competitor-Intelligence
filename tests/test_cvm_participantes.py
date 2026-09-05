"""CVM market-participant registry ingester (Job 1 / E5) — parsing + entrant normalization."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import cvm_participantes as cp

_CSV = (
    "CNPJ;DENOM_SOCIAL;DENOM_COMERC;DT_REG;DT_CANCEL;MOTIVO_CANCEL;SIT;DT_INI_SIT;UF;SITE_ADMIN\n"
    "52.237.218/0001-68;1 TO 1 CAPITAL LTDA;1 TO 1 CAPITAL;2026-03-28;;;EM FUNCIONAMENTO NORMAL;2026-03-28;SP;http://x\n"
    "11.111.111/0001-11;CANCELADA LTDA;;2019-01-01;2020-01-01;PEDIDO;CANCELADA;2020-01-01;RJ;\n"
    "52.237.218/0001-68;1 TO 1 CAPITAL LTDA;1 TO 1 CAPITAL;2026-03-28;;;EM FUNCIONAMENTO NORMAL;2026-03-28;SP;http://x\n"
)


def test_parse_keeps_active_dedups_and_normalizes():
    recs = cp.parse_csv(_CSV, "CVM-Consultores", "advisory")
    assert len(recs) == 1  # cancelled dropped; duplicate CNPJ deduped
    r = recs[0]
    assert r["id"] == "cvm-part:52.237.218/0001-68" and r["cnpj"] == "52.237.218/0001-68"
    assert r["name"] == "1 TO 1 CAPITAL LTDA" and r["brand"] == "1 TO 1 CAPITAL"
    assert r["industry"] == "advisory" and r["registered"] == "2026-03-28"
    assert r["kind"] == "competitor" and r["source"] == "CVM-Consultores"


def test_parse_drops_rows_without_cnpj():
    recs = cp.parse_csv("CNPJ;DENOM_SOCIAL;SIT\n;NO CNPJ;EM FUNCIONAMENTO NORMAL\n", "S", "advisory")
    assert recs == []


def test_cadastros_spec_points_at_ckan_authoritative_url():
    c = cp.CADASTROS["consultores"]
    assert c["url"].startswith("https://dados.cvm.gov.br/dados/") and c["url"].endswith(".zip")
    assert c["industry"] == "advisory"
