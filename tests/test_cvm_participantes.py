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


# --- #80: extended cadastro coverage ---------------------------------------------
def test_adm_carteira_cadastro_added_asset_management():
    c = cp.CADASTROS["adm_carteira"]
    assert c["url"].startswith("https://dados.cvm.gov.br/dados/ADM_CART/") and c["csv"].endswith("_pj.csv")
    assert c["industry"] == "asset-management" and c["source"] == "CVM-AdmCarteira"
    # adm_fii was verified-but-dropped (cancellations-only) — must not be shipped as an entrant feed
    assert "adm_fii" not in cp.CADASTROS


def test_adm_carteira_pj_schema_parses_like_consultores():
    csv = ("CNPJ;DENOM_SOCIAL;DENOM_COMERC;DT_REG;DT_CANCEL;MOTIVO_CANCEL;SIT;DT_INI_SIT;"
           "CATEG_REG;UF;SITE_ADMIN\n"
           "35.098.686/0001-80;10B GESTORA DE RECURSOS LTDA.;10B;2020-08-11;;;"
           "EM FUNCIONAMENTO NORMAL;2020-08-11;Gestor de Carteira;SP;https://10b.com.br\n")
    recs = cp.parse_csv(csv, "CVM-AdmCarteira", "asset-management")
    assert len(recs) == 1 and recs[0]["industry"] == "asset-management"
    assert recs[0]["cnpj"] == "35.098.686/0001-80" and recs[0]["name"].startswith("10B GESTORA")


def test_fetch_handles_both_zip_and_direct_csv(monkeypatch):
    """#80: CVM serves cadastros as a DADOS zip OR a direct .csv — _fetch_zip_csv must read both."""
    import io
    import zipfile

    class _Resp:
        def __init__(self, content):
            self.content = content
        def raise_for_status(self):
            pass

    # direct CSV (no PK zip signature)
    monkeypatch.setattr(cp.requests, "get", lambda *a, **k: _Resp(b"CNPJ;DENOM_SOCIAL;SIT\n1;A;X\n"))
    assert cp._fetch_zip_csv("https://x/cad_adm_fii.csv", "cad_adm_fii.csv").startswith("CNPJ;")
    # zip resource
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("cad_x_pj.csv", "CNPJ;DENOM_SOCIAL;SIT\n2;B;Y\n")
    monkeypatch.setattr(cp.requests, "get", lambda *a, **k: _Resp(buf.getvalue()))
    assert "DENOM_SOCIAL" in cp._fetch_zip_csv("https://x/cad_x.zip", "cad_x_pj.csv")
