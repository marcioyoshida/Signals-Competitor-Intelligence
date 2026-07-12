import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.diff.engine import DynamoDbState
from src.ingest import lambda_port


class FakeStateTable:
    """In-memory stand-in for the DynamoDB state table, keyed like the real one."""

    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}

    def get_item(self, Key):
        item = self.items.get((Key["source"], Key["id"]))
        return {"Item": item} if item else {}

    def put_item(self, Item):
        self.items[(Item["source"], Item["id"])] = Item


def test_lambda_handler_returns_digest_payload_when_ingesters_fail(monkeypatch):
    monkeypatch.setattr(lambda_port, "_new_since_last_run", lambda source, docs: docs)
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", lambda base_date: {})

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["regulatory"]["count"] == 0
    assert payload["competitor"]["count"] == 0
    assert payload["market"]["count"] == 0


def test_lambda_handler_resolves_institution_names_in_market_share(monkeypatch):
    monkeypatch.setattr(lambda_port, "_new_since_last_run", lambda source, docs: docs)
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(
        lambda_port.bcb_ifdata,
        "fetch_institutions",
        lambda base_date=None: [{"CodInst": "00068987", "NomeColuna": "Ativo Total", "Saldo": 100.0}],
    )
    monkeypatch.setattr(
        lambda_port.bcb_ifdata, "fetch_institution_names", lambda base_date: {"00068987": "Banco Exemplo"}
    )

    response = lambda_port.lambda_handler({}, None)

    payload = json.loads(response["body"])
    assert payload["market"]["items"] == [{"institution": "Banco Exemplo", "value": 100.0, "share_pct": 100.0}]


def test_lambda_handler_continues_when_normativos_fetch_raises(monkeypatch):
    def broken_fetch_recent(days=7, types=None):
        raise RuntimeError("BCB search page is down")

    monkeypatch.setattr(lambda_port, "_new_since_last_run", lambda source, docs: docs)
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", broken_fetch_recent)
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", lambda base_date: {})

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["regulatory"]["count"] == 0
    assert payload["regulatory"]["items"] == []


def test_lambda_handler_continues_when_cvm_funds_fetch_raises(monkeypatch):
    def broken_fetch_funds(watchlist_admins=None):
        raise RuntimeError("CVM CSV endpoint timed out")

    monkeypatch.setattr(lambda_port, "_new_since_last_run", lambda source, docs: docs)
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", broken_fetch_funds)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", lambda base_date: {})

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["competitor"]["count"] == 0
    assert payload["competitor"]["items"] == []


def test_lambda_handler_continues_when_ifdata_base_date_lookup_raises(monkeypatch):
    def broken_latest_base_date():
        raise RuntimeError("Could not determine an IF.data base date")

    monkeypatch.setattr(lambda_port, "_new_since_last_run", lambda source, docs: docs)
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", broken_latest_base_date)

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["market"]["count"] == 0
    assert payload["market"]["items"] == []


def test_lambda_handler_continues_when_institution_names_lookup_raises(monkeypatch):
    def broken_fetch_institution_names(base_date):
        raise RuntimeError("IfDataCadastro endpoint timed out")

    monkeypatch.setattr(lambda_port, "_new_since_last_run", lambda source, docs: docs)
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(
        lambda_port.bcb_ifdata,
        "fetch_institutions",
        lambda base_date=None: [{"CodInst": "00068987", "NomeColuna": "Ativo Total", "Saldo": 100.0}],
    )
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", broken_fetch_institution_names)

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["market"]["count"] == 0
    assert payload["market"]["items"] == []


def test_lambda_handler_continues_when_all_ingesters_raise(monkeypatch):
    monkeypatch.setattr(lambda_port, "_new_since_last_run", lambda source, docs: docs)
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
    monkeypatch.setattr(lambda_port, "_new_since_last_run", lambda source, docs: docs)
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", lambda base_date: {})
    monkeypatch.setenv("ONCA_DIGESTS_BUCKET", "test-bucket")

    class BrokenS3Client:
        def put_object(self, **kwargs):
            raise RuntimeError("upload failed")

    monkeypatch.setattr(lambda_port.boto3, "client", lambda *args, **kwargs: BrokenS3Client())

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["source"] == "lambda_port"


def test_lambda_handler_reports_only_new_normativos_across_two_runs(monkeypatch):
    # There's no real second daily snapshot yet, so mock two successive
    # fetch_recent results against one persistent fake state table to prove
    # the diff wiring: doc A and B are "seen" on day 1, only C is new on day 2.
    fake_table = FakeStateTable()
    monkeypatch.setattr(lambda_port, "DynamoDbState", lambda source: DynamoDbState(source, table=fake_table))
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", lambda base_date: {})

    doc_a = {"id": "bcb:a", "subject": "Resolução A"}
    doc_b = {"id": "bcb:b", "subject": "Resolução B"}
    doc_c = {"id": "bcb:c", "subject": "Resolução C"}

    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [doc_a, doc_b])
    day_one = json.loads(lambda_port.lambda_handler({}, None)["body"])
    assert day_one["regulatory"]["count"] == 2
    assert day_one["regulatory"]["new_count"] == 2
    assert day_one["regulatory"]["items"] == [doc_a, doc_b]

    monkeypatch.setattr(
        lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [doc_a, doc_b, doc_c]
    )
    day_two = json.loads(lambda_port.lambda_handler({}, None)["body"])
    assert day_two["regulatory"]["count"] == 3
    assert day_two["regulatory"]["new_count"] == 1
    assert day_two["regulatory"]["items"] == [doc_c]


def test_lambda_handler_treats_all_as_new_when_diff_state_unavailable(monkeypatch):
    class BrokenState:
        def __init__(self, source):
            self.source = source

        def load(self):
            raise RuntimeError("DynamoDB table unreachable")

    monkeypatch.setattr(lambda_port, "DynamoDbState", BrokenState)
    monkeypatch.setattr(
        lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [{"id": "bcb:a"}]
    )
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", lambda base_date: {})

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["regulatory"]["count"] == 1
    assert payload["regulatory"]["new_count"] == 1
