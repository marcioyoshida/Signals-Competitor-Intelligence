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
