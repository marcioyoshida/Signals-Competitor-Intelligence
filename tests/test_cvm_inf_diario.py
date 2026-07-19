import io
import zipfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import cvm_inf_diario


def _zip_csv(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, text.encode("latin-1"))
    return buf.getvalue()


def test_select_competency_date_skips_sparse_late_day():
    rows = (
        [{"date": "2026-07-16"} for _ in range(100)]
        + [{"date": "2026-07-15"} for _ in range(100)]
        + [{"date": "2026-07-17"} for _ in range(3)]  # late sparse
    )
    assert cvm_inf_diario._select_competency_date(rows) == "2026-07-16"


def test_load_month_aggregates_subclasses(monkeypatch):
    header = (
        "TP_FUNDO_CLASSE;CNPJ_FUNDO_CLASSE;ID_SUBCLASSE;DT_COMPTC;"
        "VL_TOTAL;VL_QUOTA;VL_PATRIM_LIQ;CAPTC_DIA;RESG_DIA;NR_COTST\n"
    )
    body = (
        header
        + "FIF;12.345.678/0001-90;A;2026-07-16;100;1;1000;50;10;2\n"
        + "FIF;12.345.678/0001-90;B;2026-07-16;200;1;2000;0;5;3\n"
        + "FIF;99.999.999/0001-00;;2026-07-16;50;1;500;0;0;1\n"
    )
    payload = _zip_csv("inf.csv", body)

    class FakeResp:
        content = payload

        def raise_for_status(self):
            return None

    monkeypatch.setattr(cvm_inf_diario.requests, "get", lambda *a, **k: FakeResp())
    rows = cvm_inf_diario._load_month_for_cnpjs("202607", {"12345678000190"})
    assert len(rows) == 1
    r = rows[0]
    assert r["cnpj"] == "12345678000190"
    assert r["pl"] == 3000.0
    assert r["captacao"] == 50.0
    assert r["resgate"] == 15.0
    assert r["net_flow"] == 35.0
    assert r["move_key"] == "12345678000190"


def test_fetch_latest_uses_registry_and_latest_full_day(monkeypatch):
    class_meta = {
        "12345678000190": {
            "admin": "ITAU UNIBANCO S.A.",
            "manager": "",
            "fund_name": "FUND A",
            "class_name": "FUND A CLASS",
            "tipo_classe": "FIF",
        }
    }
    month_rows = [
        {
            "cnpj": "12345678000190",
            "date": "2026-07-16",
            "pl": 1_000_000.0,
            "net_flow": 100.0,
            "captacao": 100.0,
            "resgate": 0.0,
            "move_key": "12345678000190",
            "id": "cvm-inf:12345678000190:2026-07-16",
            "source": "CVM-InfDiario",
            "kind": "competitor",
            "url": "u",
            "yyyymm": "202607",
        },
        {
            "cnpj": "12345678000190",
            "date": "2026-07-17",
            "pl": 10.0,
            "net_flow": 0.0,
            "captacao": 0.0,
            "resgate": 0.0,
            "move_key": "12345678000190",
            "id": "cvm-inf:12345678000190:2026-07-17",
            "source": "CVM-InfDiario",
            "kind": "competitor",
            "url": "u",
            "yyyymm": "202607",
        },
    ]
    # need many rows on 16th so select_competency_date prefers it
    month_rows = month_rows[:1] * 20 + month_rows[1:]

    monkeypatch.setattr(
        cvm_inf_diario, "resolve_watchlist_classes", lambda *a, **k: class_meta
    )
    monkeypatch.setattr(
        cvm_inf_diario, "_load_month_for_cnpjs", lambda *a, **k: month_rows
    )
    out = cvm_inf_diario.fetch_latest(watchlist_admins=["ITAU"])
    assert len(out) == 20
    assert out[0]["date"] == "2026-07-16"
    assert out[0]["admin"] == "ITAU UNIBANCO S.A."
    assert out[0]["fund_name"] == "FUND A CLASS"


def test_empty_watchlist_returns_empty(monkeypatch):
    monkeypatch.setattr(cvm_inf_diario, "resolve_watchlist_classes", lambda *a, **k: {})
    assert cvm_inf_diario.fetch_latest(watchlist_admins=[]) == []
