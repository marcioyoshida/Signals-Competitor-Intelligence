import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import lambda_port


def test_lambda_handler_returns_digest_payload_when_ingesters_fail(monkeypatch):
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["regulatory"]["count"] == 0
    assert payload["competitor"]["count"] == 0
    assert payload["market"]["count"] == 0


def test_lambda_handler_continues_when_normativos_fetch_raises(monkeypatch):
    def broken_fetch_recent(days=7, types=None):
        raise RuntimeError("BCB search page is down")

    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", broken_fetch_recent)
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["regulatory"]["count"] == 0
    assert payload["regulatory"]["items"] == []


def test_lambda_handler_continues_when_cvm_funds_fetch_raises(monkeypatch):
    def broken_fetch_funds(watchlist_admins=None):
        raise RuntimeError("CVM CSV endpoint timed out")

    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", broken_fetch_funds)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["competitor"]["count"] == 0
    assert payload["competitor"]["items"] == []


def test_lambda_handler_continues_when_ifdata_base_date_lookup_raises(monkeypatch):
    def broken_latest_base_date():
        raise RuntimeError("Could not determine an IF.data base date")

    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", broken_latest_base_date)

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["market"]["count"] == 0
    assert payload["market"]["items"] == []


def test_lambda_handler_continues_when_all_ingesters_raise(monkeypatch):
    monkeypatch.setattr(
        lambda_port.bcb_normativos,
        "fetch_recent",
        lambda days=7, types=None: (_ for _ in ()).throw(RuntimeError("normativos down")),
    )
    monkeypatch.setattr(
        lambda_port.cvm_fundos,
        "fetch_funds",
        lambda watchlist_admins=None: (_ for _ in ()).throw(RuntimeError("cvm down")),
    )
    monkeypatch.setattr(
        lambda_port.bcb_ifdata,
        "latest_base_date",
        lambda: (_ for _ in ()).throw(RuntimeError("ifdata down")),
    )

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["regulatory"]["count"] == 0
    assert payload["competitor"]["count"] == 0
    assert payload["market"]["count"] == 0


def test_lambda_handler_continues_when_s3_upload_fails(monkeypatch):
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])
    monkeypatch.setenv("ONCA_DIGESTS_BUCKET", "test-bucket")

    class BrokenS3Client:
        def put_object(self, **kwargs):
            raise RuntimeError("upload failed")

    monkeypatch.setattr(lambda_port.boto3, "client", lambda *args, **kwargs: BrokenS3Client())

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["source"] == "lambda_port"
