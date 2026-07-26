import csv
import io
import zipfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import cvm_fundos


def _zip_from_rows(files: dict[str, tuple[list[str], list[dict]]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, (fieldnames, rows) in files.items():
            text = io.StringIO()
            w = csv.DictWriter(text, fieldnames=fieldnames, delimiter=";", lineterminator="\n")
            w.writeheader()
            for row in rows:
                w.writerow(row)
            zf.writestr(name, text.getvalue().encode("latin-1"))
    return buf.getvalue()


def test_fetch_funds_from_rcvm175_registry(monkeypatch):
    fundo_fields = [
        "ID_Registro_Fundo",
        "CNPJ_Fundo",
        "Situacao",
        "Denominacao_Social",
        "Administrador",
        "Gestor",
        "Data_Registro",
        "Data_Constituicao",
        "Tipo_Fundo",
        "Data_Adaptacao_RCVM175",
    ]
    classe_fields = [
        "ID_Registro_Fundo",
        "ID_Registro_Classe",
        "CNPJ_Classe",
        "Situacao",
        "Denominacao_Social",
        "Tipo_Classe",
        "Data_Registro",
        "Data_Inicio",
        "Data_Constituicao",
        "Classificacao",
    ]
    payload = _zip_from_rows(
        {
            "registro_fundo.csv": (
                fundo_fields,
                [
                    {
                        "ID_Registro_Fundo": "1",
                        "CNPJ_Fundo": "12345678000190",
                        "Situacao": "Em Funcionamento Normal",
                        "Denominacao_Social": "FUNDO ALPHA",
                        "Administrador": "BTG PACTUAL SERVIÇOS FINANCEIROS S/A DTVM",
                        "Gestor": "GESTOR X",
                        "Data_Registro": "2026-01-15",
                        "Data_Constituicao": "2026-01-15",
                        "Tipo_Fundo": "FIF",
                        "Data_Adaptacao_RCVM175": "2026-01-15",
                    },
                    {
                        "ID_Registro_Fundo": "2",
                        "CNPJ_Fundo": "99999999000199",
                        "Situacao": "Cancelado",
                        "Denominacao_Social": "FUNDO DEAD",
                        "Administrador": "ITAU UNIBANCO S.A.",
                        "Gestor": "",
                        "Data_Registro": "2020-01-01",
                        "Data_Constituicao": "2020-01-01",
                        "Tipo_Fundo": "FIF",
                        "Data_Adaptacao_RCVM175": "",
                    },
                    {
                        "ID_Registro_Fundo": "3",
                        "CNPJ_Fundo": "11111111000111",
                        "Situacao": "Em Funcionamento Normal",
                        "Denominacao_Social": "FUNDO BETA",
                        "Administrador": "OTHER ADMIN SA",
                        "Gestor": "",
                        "Data_Registro": "2026-03-01",
                        "Data_Constituicao": "2026-03-01",
                        "Tipo_Fundo": "FIF",
                        "Data_Adaptacao_RCVM175": "",
                    },
                ],
            ),
            "registro_classe.csv": (
                classe_fields,
                [
                    {
                        "ID_Registro_Fundo": "1",
                        "ID_Registro_Classe": "10",
                        "CNPJ_Classe": "12345678000191",
                        "Situacao": "Em Funcionamento Normal",
                        "Denominacao_Social": "FUNDO ALPHA CLASSE A",
                        "Tipo_Classe": "FIF Class",
                        "Data_Registro": "2026-01-20",
                        "Data_Inicio": "2026-01-20",
                        "Data_Constituicao": "2026-01-20",
                        "Classificacao": "",
                    }
                ],
            ),
        }
    )

    class FakeResp:
        content = payload

        def raise_for_status(self):
            return None

    monkeypatch.setattr(cvm_fundos.requests, "get", lambda *a, **k: FakeResp())
    rows = cvm_fundos.fetch_funds(watchlist_admins=["BTG"])
    ids = {r["id"] for r in rows}
    assert "cvm:fund:12345678000190" in ids
    assert "cvm:class:12345678000191" in ids
    assert "cvm:fund:99999999000199" not in ids  # cancelled
    assert "cvm:fund:11111111000111" not in ids  # other admin
    alpha = next(r for r in rows if r["id"] == "cvm:fund:12345678000190")
    assert "BTG" in (alpha["admin"] or "").upper()
    assert alpha["registered"] == "2026-01-15"
    assert alpha["url"] == cvm_fundos.DATASET_URL


def test_empty_watchlist_returns_empty():
    assert cvm_fundos.fetch_funds([]) == []
