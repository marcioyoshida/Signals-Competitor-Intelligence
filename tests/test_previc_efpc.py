"""#79 (E2) — PREVIC EFPC base-cadastral XLSX parser (stdlib, header-name mapping)."""
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import previc_efpc as pe

_COLS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _make_xlsx(rows: list[list[str]], *, start_col: int = 1) -> bytes:
    """Minimal .xlsx matching the PREVIC layout: a title row, then header, then data. ``start_col``
    shifts columns right (real file starts at column B) so header-name mapping is exercised."""
    strings: list[str] = []
    idx: dict[str, int] = {}

    def sid(s: str) -> int:
        if s not in idx:
            idx[s] = len(strings)
            strings.append(s)
        return idx[s]

    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    sheet_rows = []
    for ri, row in enumerate(rows, start=1):
        cells = []
        for ci, val in enumerate(row):
            if val == "":
                continue
            ref = f"{_COLS[ci + start_col]}{ri}"
            cells.append(f'<c r="{ref}" t="s"><v>{sid(val)}</v></c>')
        sheet_rows.append(f'<row r="{ri}">{"".join(cells)}</row>')
    sheet_xml = f'<worksheet xmlns="{ns}"><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    ss_xml = (f'<sst xmlns="{ns}" count="{len(strings)}" uniqueCount="{len(strings)}">'
              + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        z.writestr("xl/sharedStrings.xml", ss_xml)
    return buf.getvalue()


_HEADER = ["Código da EFPC", "Razão Social", "Sigla EFPC", "CNPJ",
           "Situação Detalhada EFPC", "Situacao", "UF"]
_ROWS = [
    ["Base Cadastral Entidades"],                                  # title row (offset)
    [],                                                            # blank row
    _HEADER,
    ["0396-2", "ABBOTTPREV PREVIDENCIA PRIVADA", "ABBOTTPREV", "03.443.973/0001-93",
     "NORMAL - EM FUNCIONAMENTO", "NORMAL", "SP"],
    ["0269-3", "ABRILPREV SOCIEDADE DE PREVIDENCIA", "ABRILPREV", "73.000.838/0001-59",
     "ENCERRADA", "ENCERRADA", "SP"],
    ["9999-9", "SEM CNPJ EFPC", "SEMCNPJ", "", "NORMAL", "NORMAL", "RJ"],
]


def test_parses_efpc_with_title_offset_and_header_mapping():
    recs = pe.parse_entities(_make_xlsx(_ROWS))
    assert len(recs) == 3  # title/blank/header excluded
    r = recs[0]
    assert r["name"] == "ABBOTTPREV PREVIDENCIA PRIVADA" and r["cnpj"] == "03443973000193"
    assert r["sigla"] == "ABBOTTPREV" and r["codigo_efpc"] == "0396-2"
    assert r["situation"] == "NORMAL" and r["uf"] == "SP"
    assert r["id"] == "previc-efpc:03443973000193" and r["is_fintech"] is False
    assert r["license_class"].startswith("EFPC")


def test_situation_carries_and_id_falls_back_to_codigo():
    recs = {r["name"]: r for r in pe.parse_entities(_make_xlsx(_ROWS))}
    assert recs["ABRILPREV SOCIEDADE DE PREVIDENCIA"]["situation"] == "ENCERRADA"
    nocnpj = recs["SEM CNPJ EFPC"]
    assert nocnpj["cnpj"] is None and nocnpj["id"] == "previc-efpc:9999-9"


def test_no_header_returns_empty():
    assert pe.parse_entities(_make_xlsx([["Base"], ["x", "y"]])) == []
