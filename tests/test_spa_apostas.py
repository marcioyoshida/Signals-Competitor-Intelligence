"""#78 (E3) — SPA/MF authorized betting-operators XLSX parser (stdlib, no openpyxl)."""
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import spa_apostas as sp

_COLS = "ABCDEFG"


def _make_xlsx(rows: list[list[str]]) -> bytes:
    """Build a minimal .xlsx (shared strings + one sheet) matching the SPA layout the parser reads.
    Blank cells are omitted (as real xlsx do) so the column-ref mapping is exercised."""
    strings: list[str] = []
    idx: dict[str, int] = {}

    def sid(s: str) -> int:
        if s not in idx:
            idx[s] = len(strings)
            strings.append(s)
        return idx[s]

    sheet_rows = []
    for ri, row in enumerate(rows, start=1):
        cells = []
        for ci, val in enumerate(row):
            if val == "":
                continue  # omit blanks, like Excel
            ref = f"{_COLS[ci]}{ri}"
            cells.append(f'<c r="{ref}" t="s"><v>{sid(val)}</v></c>')
        sheet_rows.append(f'<row r="{ri}">{"".join(cells)}</row>')
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    sheet_xml = f'<worksheet xmlns="{ns}"><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    ss_items = "".join(f"<si><t>{s}</t></si>" for s in strings)
    ss_xml = f'<sst xmlns="{ns}" count="{len(strings)}" uniqueCount="{len(strings)}">{ss_items}</sst>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        z.writestr("xl/sharedStrings.xml", ss_xml)
    return buf.getvalue()


_ROWS = [
    ["Título longo a ignorar", "", "", "", "", "", ""],
    ["", "PORTARIA DE AUTORIZAÇÃO", "DENOMINAÇÃO SOCIAL DA EMPRESA", "CNPJ", "MARCAS", "DOMÍNIOS", "NÚMERO E ANO DO REQUERIMENTO"],
    ["1", "SPA/MF nº 797, de 23 de março de 2026", "BPX BETS SPORTS GROUP LTDA", "55.590.815/0001-60", "VAIDEBET", "vaidebet.bet.br", " 0059/2024"],
    ["", "", "", "", "BETPIX365", "betpix365.bet.br", ""],
    ["", "", "", "", "OBABET", "obabet.bet.br", ""],
    ["2", "SPA/MF nº 400", "NOSSO TIME IGAMING LTDA", "60.828.451/0001-43", "JOGA JUNTO", "jogajunto.bet.br", "0100/2024"],
]


def test_parses_companies_and_groups_brands():
    recs = sp.parse_authorized(_make_xlsx(_ROWS))
    assert len(recs) == 2  # two companies, not five rows
    bpx = recs[0]
    assert bpx["name"] == "BPX BETS SPORTS GROUP LTDA"
    assert bpx["cnpj"] == "55590815000160" and bpx["id"] == "spa-bet:55590815000160"
    assert bpx["brands"] == ["VAIDEBET", "BETPIX365", "OBABET"]  # brand-only rows carried forward
    assert bpx["domains"] == ["vaidebet.bet.br", "betpix365.bet.br", "obabet.bet.br"]
    assert bpx["license_class"] == "Casa de apostas (SPA/MF)" and bpx["is_fintech"] is False
    assert bpx["portaria"].startswith("SPA/MF nº 797")


def test_second_company_is_separate():
    recs = sp.parse_authorized(_make_xlsx(_ROWS))
    assert recs[1]["name"] == "NOSSO TIME IGAMING LTDA" and recs[1]["brands"] == ["JOGA JUNTO"]


def test_header_detected_before_data():
    # rows before the CNPJ header must not become records
    recs = sp.parse_authorized(_make_xlsx(_ROWS))
    assert all(r["name"] not in ("", "Título longo a ignorar") for r in recs)
