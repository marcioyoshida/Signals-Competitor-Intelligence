"""#102 (E/#14 Stage 1): CVM FII informe-mensal structured source + discover wiring."""
import io
import sys
import zipfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import cvm_fii
from src.synth import entity_discovery as ed


class _FakeTable:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def get_item(self, Key):
        it = self.items.get(Key["pk"])
        return {"Item": it} if it else {}

    def put_item(self, Item):
        self.items[Item["pk"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["pk"], None)

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


_GERAL_HDR = ("Tipo_Fundo_Classe;CNPJ_Fundo_Classe;Data_Referencia;Versao;Nome_Fundo_Classe;"
              "Data_Funcionamento;Publico_Alvo;Codigo_ISIN;Quantidade_Cotas_Emitidas;"
              "Segmento_Atuacao;Tipo_Gestao;Mercado_Negociacao_Bolsa;Nome_Administrador;"
              "CNPJ_Administrador;Site")
_AP_HDR = "CNPJ_Fundo_Classe;Data_Referencia;Versao;Total_Investido;Total_Passivo"


def _geral_row(cnpj, ref, ver, name, isin, seg="Logística", listed="S"):
    return (f"Classe;{cnpj};{ref};{ver};{name};1994-11-24;INVESTIDORES EM GERAL;{isin};"
            f"2800149;{seg};Definida;{listed};HEDGE INVESTMENTS DTVM LTDA;07253654000176;x.com")


def _ap_row(cnpj, ref, ver, total):
    return f"{cnpj};{ref};{ver};{total};1533619.84"


def _make_zip(year: int, geral_rows, ap_rows) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"inf_mensal_fii_geral_{year}.csv",
                   (_GERAL_HDR + "\n" + "\n".join(geral_rows) + "\n").encode("latin-1"))
        z.writestr(f"inf_mensal_fii_ativo_passivo_{year}.csv",
                   (_AP_HDR + "\n" + "\n".join(ap_rows) + "\n").encode("latin-1"))
        z.writestr(f"inf_mensal_fii_complemento_{year}.csv", b"x\n")
    return buf.getvalue()


class _Resp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


def _patch(monkeypatch, content):
    monkeypatch.setattr(cvm_fii.requests, "get", lambda *a, **k: _Resp(content))


def test_fetch_fii_parses_latest_month_ticker_and_size_proxy(monkeypatch):
    zbytes = _make_zip(
        2026,
        [
            # two months for the same fund → keep the latest Data_Referencia
            _geral_row("28.757.546/0001-00", "2026-01-01", "1", "XP MALLS FII", "BRXPMLCTF017"),
            _geral_row("28.757.546/0001-00", "2026-02-01", "1", "XP MALLS FII", "BRXPMLCTF017"),
            _geral_row("11.728.688/0001-47", "2026-02-01", "1", "PATRIA LOG FII", "BRHGLGCTF012"),
        ],
        [
            _ap_row("28.757.546/0001-00", "2026-01-01", "1", "7000000000"),
            _ap_row("28.757.546/0001-00", "2026-02-01", "1", "7350000000"),
            _ap_row("11.728.688/0001-47", "2026-02-01", "1", "8294000000"),
        ],
    )
    _patch(monkeypatch, zbytes)
    rows = cvm_fii.fetch_fii(2026)
    by = {r["cnpj"]: r for r in rows}
    assert len(rows) == 2
    xp = by["28757546000100"]
    assert xp["ticker"] == "XPML11" and xp["industry"] == "real-estate-funds"
    assert xp["as_of"] == "2026-02-01" and xp["pl"] == 7350000000.0  # latest month's proxy
    assert xp["discovery_source"] == "cvm_fii" and xp["fund_class"] == "FII"
    assert by["11728688000147"]["ticker"] == "HGLG11"


def test_fetch_fii_min_pl_floor_drops_micro(monkeypatch):
    zbytes = _make_zip(
        2026,
        [_geral_row("28.757.546/0001-00", "2026-02-01", "1", "XP MALLS FII", "BRXPMLCTF017"),
         _geral_row("00.000.000/0001-00", "2026-02-01", "1", "MICRO FII", "BRMICRCTF010")],
        [_ap_row("28.757.546/0001-00", "2026-02-01", "1", "7350000000"),
         _ap_row("00.000.000/0001-00", "2026-02-01", "1", "1000000")],
    )
    _patch(monkeypatch, zbytes)
    rows = cvm_fii.fetch_fii(2026, min_pl=100_000_000)
    assert [r["cnpj"] for r in rows] == ["28757546000100"]  # micro dropped


def test_discover_creates_fii_as_real_estate_fund():
    """The shared industry-parametric engine creates an FII entity (not agri-funds)."""
    table = _FakeTable()
    rows = [{
        "fund_name": "XP MALLS FII", "cnpj": "28757546000100", "isin": "BRXPMLCTF017",
        "ticker": "XPML11", "admin": "XP", "manager": None, "pl": 7.35e9,
        "industry": "real-estate-funds", "fund_class": "FII", "discovery_source": "cvm_fii",
        "as_of": "2026-02-01", "url": cvm_fii.DATASET_URL,
    }]
    report = ed.discover_fiagro(industry="real-estate-funds", rows=rows, auto_create=True, table=table)
    assert report["created"] == ["xpml11"]
    ent = table.items["ENT#xpml11"]
    assert ent["industries"] == ["real-estate-funds"] and ent["ticker"] == "XPML11"
    assert "28757546" in ent["cnpj_roots"]


def test_profile_generalized_for_fii():
    p = ed._profile_from_fiagro({
        "fund_name": "XP MALLS FII", "cnpj": "28757546000100", "ticker": "XPML11",
        "isin": "BRXPMLCTF017", "industry": "real-estate-funds", "fund_class": "FII",
        "discovery_source": "cvm_fii", "pl": 7.35e9,
    })
    assert p["industries"] == ["real-estate-funds"] and p["source"] == "cvm_fii"
    assert p["ticker"] == "XPML11" and p["auto_ok"] is True
    # FIAGRO defaults preserved when the row omits the new fields
    q = ed._profile_from_fiagro({"fund_name": "KINEA AGRO", "cnpj": "41745701000137",
                                 "ticker": "KNCA11", "isin": "BRKNCACTF014"})
    assert q["industries"] == ["agri-funds"] and q["source"] == "cvm_fiagro"
