"""consumidor.gov.br complaints ingester (#63)."""
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import consumidor_gov as cg

# Microdata CSV (;-delim, real column names incl. accents). 3 ACME rows + 1 OUTRA row.
_HEADER = ("Gestor;UF;Nome Fantasia;Segmento de Mercado;Respondida;"
           "Avaliação Reclamação;Situação;Nota do Consumidor")
_CSV = "\n".join([
    _HEADER,
    "SP;SP;ACME BANK;Bancos;S;Resolvida;Finalizada avaliada;5",
    "RJ;RJ;ACME BANK;Bancos;S;Não Resolvida;Finalizada avaliada;2",
    "MG;MG;ACME BANK;Bancos;N;Não Resolvida;Finalizada não avaliada;",
    "SP;SP;OUTRA LTDA;Varejo;S;Resolvida;Finalizada avaliada;4",
])


def _zip_bytes(csv_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("finalizadas_2026-07.csv", csv_text.encode("latin-1"))
    return buf.getvalue()


def test_aggregate_from_zip_and_raw_csv():
    for blob in (_zip_bytes(_CSV), _CSV.encode("latin-1")):
        rows = cg.aggregate(blob, min_complaints=1)
        acme = next(r for r in rows if r["company"] == "ACME BANK")
        assert acme["complaints"] == 3
        assert acme["answered_rate"] == round(2 / 3, 3)
        assert acme["resolved_rate"] == round(1 / 3, 3)
        assert acme["avg_score"] == round((5 + 2) / 2, 2)   # only the 2 rated rows
        assert acme["segment"] == "Bancos"


def test_aggregate_min_complaints_floor():
    rows = cg.aggregate(_CSV.encode("latin-1"), min_complaints=3)
    assert {r["company"] for r in rows} == {"ACME BANK"}    # OUTRA (1 row) dropped


def test_fetch_indicators_uses_catalog_resource():
    res = {"link": "https://x/finalizadas_2026-07.zip", "atualizado": "2026-08-06"}
    rows = cg.fetch_indicators(
        resource_finder=lambda: res,
        downloader=lambda url: _zip_bytes(_CSV) if url == res["link"] else None,
        min_complaints=1)
    assert rows and rows[0]["as_of"] == "2026-08-06"


def test_fetch_indicators_inert_without_resource():
    assert cg.fetch_indicators(resource_finder=lambda: None) == []   # no token/resource -> []


def test_map_resolves_by_name_and_keeps_best():
    rows = cg.aggregate(_CSV.encode("latin-1"), min_complaints=1)
    recs = cg.map_to_entities(
        rows, resolver=lambda item: ["acme"] if "ACME" in (item["title"] or "").upper() else [])
    assert len(recs) == 1
    assert recs[0]["entity"] == "acme" and recs[0]["id"] == "consumidor:acme"
    assert recs[0]["complaints"] == 3


def test_summarize_and_store():
    rows = cg.aggregate(_CSV.encode("latin-1"), min_complaints=1)
    recs = cg.map_to_entities(rows, resolver=lambda item: ["acme"]
                              if "ACME" in item["title"].upper() else ["outra"])
    s = cg.summarize(recs)
    assert s["total"] == 2 and s["worst_resolution"][0]["resolved_rate"] <= 1
    merged = cg.merge({"records": {"acme": recs[0]}}, recs)
    assert merged["count"] == 2 and cg.list_records(merged)[0]["complaints"] >= 1
