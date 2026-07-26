import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import raw_writer


class FakeS3Client:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body


def test_write_raw_documents_writes_text_and_metadata_for_regulatory_doc(monkeypatch):
    fake = FakeS3Client()
    monkeypatch.setattr(raw_writer.boto3, "client", lambda *a, **k: fake)

    doc = {
        "id": "bcb:Comunicado:45546",
        "source": "BCB",
        "kind": "regulatory",
        "doc_type": "Comunicado",
        "number": "45546",
        "date": "2026-07-10",
        "subject": "Divulga as condições de oferta pública",
        "url": "https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?tipo=Comunicado&numero=45546",
    }

    written = raw_writer.write_raw_documents("onca-raw-test", [doc])

    assert written == ["BCB/bcb:Comunicado:45546.txt"]
    text = fake.objects["BCB/bcb:Comunicado:45546.txt"].decode("utf-8")
    assert "Comunicado N° 45546" in text
    assert "Divulga as condições de oferta pública" in text

    metadata = json.loads(fake.objects["BCB/bcb:Comunicado:45546.txt.metadata.json"])
    assert metadata == {
        "metadataAttributes": {
            "source": "BCB",
            "kind": "regulatory",
            "doc_type": "Comunicado",
            "date": "2026-07-10",
            "url": doc["url"],
        }
    }


def test_write_raw_documents_writes_text_and_metadata_for_competitor_doc(monkeypatch):
    fake = FakeS3Client()
    monkeypatch.setattr(raw_writer.boto3, "client", lambda *a, **k: fake)

    doc = {
        "id": "cvm:fund:06.537.068/0001-90",
        "source": "CVM",
        "kind": "competitor",
        "fund_name": "AMAZÔNIA CREDIT 90",
        "fund_class": "",
        "admin": "BANCO DA AMAZÔNIA S.A.",
        "manager": "",
        "registered": "2004-08-02",
        "started": "2004-08-02",
    }

    written = raw_writer.write_raw_documents("onca-raw-test", [doc])

    assert written == ["CVM/cvm:fund:06.537.068/0001-90.txt"]
    text = fake.objects[written[0]].decode("utf-8")
    assert "AMAZÔNIA CREDIT 90" in text
    assert "BANCO DA AMAZÔNIA S.A." in text

    # No url and no fund_class value for this doc — must be omitted, not crash or write empty strings.
    # fund_name is surfaced as citation metadata "name".
    metadata = json.loads(fake.objects[f"{written[0]}.metadata.json"])
    assert metadata == {
        "metadataAttributes": {
            "source": "CVM",
            "kind": "competitor",
            "date": "2004-08-02",
            "name": "AMAZÔNIA CREDIT 90",
        }
    }


def test_write_raw_documents_returns_empty_list_for_empty_input(monkeypatch):
    fake = FakeS3Client()
    monkeypatch.setattr(raw_writer.boto3, "client", lambda *a, **k: fake)

    assert raw_writer.write_raw_documents("onca-raw-test", []) == []
    assert fake.objects == {}


def test_write_raw_documents_includes_sec_filing_body(monkeypatch):
    fake = FakeS3Client()
    monkeypatch.setattr(raw_writer.boto3, "client", lambda *a, **k: fake)

    doc = {
        "id": "sec:1745431:0001-26-000001",
        "source": "SEC-EDGAR",
        "kind": "competitor",
        "ticker": "STNE",
        "form": "6-K",
        "company": "StoneCo Ltd.",
        "filed": "2026-06-01",
        "accession": "0001-26-000001",
        "subject": "Earnings release for Q1 2026",
        "text": "StoneCo reported total payment volume of R$ 100 billion.",
        "url": "https://www.sec.gov/Archives/edgar/data/1745431/0001/a.htm",
    }

    written = raw_writer.write_raw_documents("onca-raw-test", [doc])
    assert written == ["SEC-EDGAR/sec:1745431:0001-26-000001.txt"]
    text = fake.objects[written[0]].decode("utf-8")
    assert "STNE 6-K" in text
    assert "StoneCo Ltd." in text
    assert "Earnings release for Q1 2026" in text
    assert "total payment volume" in text
    assert doc["url"] in text

    metadata = json.loads(fake.objects[f"{written[0]}.metadata.json"])
    assert metadata["metadataAttributes"]["source"] == "SEC-EDGAR"
    assert metadata["metadataAttributes"]["doc_type"] == "6-K"
    assert metadata["metadataAttributes"]["url"] == doc["url"]
