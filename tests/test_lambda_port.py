import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.diff.engine import DynamoDbState, DynamoDbValueState, detect_moves
from src.ingest import lambda_port


@pytest.fixture(autouse=True)
def _auto_stub_external_sources(monkeypatch):
    """Keep lambda tests off live juros/ofertas APIs unless a test overrides."""
    monkeypatch.setattr(lambda_port.bcb_juros, "fetch_daily", lambda **kwargs: [])
    monkeypatch.setattr(
        lambda_port.bcb_juros,
        "filter_rates",
        lambda rows, institutions=None, modalities=None: rows,
    )
    monkeypatch.setattr(lambda_port.bcb_juros, "for_moves", lambda rows: rows)
    monkeypatch.setattr(lambda_port.cvm_ofertas, "fetch_recent", lambda **kwargs: [])
    monkeypatch.setattr(lambda_port.sec_filings, "fetch_filings", lambda *a, **k: [])
    monkeypatch.setattr(lambda_port.cvm_inf_diario, "fetch_latest", lambda **kwargs: [])
    monkeypatch.setattr(lambda_port.cvm_inf_diario, "for_moves", lambda rows: rows)


class FakeStateTable:
    """In-memory stand-in for the DynamoDB state table, keyed like the real one."""

    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}

    def get_item(self, Key):
        item = self.items.get((Key["source"], Key["id"]))
        return {"Item": item} if item else {}

    def put_item(self, Item):
        self.items[(Item["source"], Item["id"])] = Item


def _stub_juros(monkeypatch, rows=None):
    monkeypatch.setattr(lambda_port.bcb_juros, "fetch_daily", lambda **kwargs: rows or [])
    monkeypatch.setattr(
        lambda_port.bcb_juros,
        "filter_rates",
        lambda rows, institutions=None, modalities=None: rows,
    )
    monkeypatch.setattr(lambda_port.bcb_juros, "for_moves", lambda rows: rows)
    monkeypatch.setattr(
        lambda_port.bcb_juros,
        "DEFAULT_MODALITY_FILTERS",
        ["Cartão de crédito - rotativo"],
    )


def _stub_core_ingesters(monkeypatch):
    """Stub sources that every handler test must not hit live."""
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", lambda base_date: {})
    monkeypatch.setattr(lambda_port.bcb_autorizacoes, "fetch_authorized", lambda: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "fetch_recent", lambda anomes=None, resource=None, top=10000: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "by_institution", lambda rows, watchlist_ispb=None: [])
    _stub_juros(monkeypatch)


def test_lambda_handler_returns_digest_payload_when_ingesters_fail(monkeypatch):
    monkeypatch.setattr(lambda_port, "_new_since_last_run", lambda source, docs, seed_if_empty=False: docs)
    monkeypatch.setattr(lambda_port, "_moves_since_last_run", lambda *a, **k: [])
    _stub_core_ingesters(monkeypatch)

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["regulatory"]["count"] == 0
    assert payload["competitor"]["count"] == 0
    assert payload["market"]["count"] == 0
    assert payload["new_entrants"]["count"] == 0
    assert payload["pix_moves"]["move_count"] == 0
    assert payload["juros_moves"]["move_count"] == 0
    assert payload["ofertas"]["count"] == 0
    assert payload["ofertas"]["new_count"] == 0
    assert payload["sec_filings"]["count"] == 0
    assert payload["sec_filings"]["new_count"] == 0
    assert payload["inf_diario_moves"]["funds_tracked"] == 0
    assert payload["inf_diario_moves"]["move_count"] == 0


def test_lambda_handler_passes_watchlist_config_to_ingesters(monkeypatch):
    captured = {}

    def fake_fetch_recent(days=7, types=None):
        captured["days"] = days
        return []

    def fake_fetch_funds(watchlist_admins=None):
        captured["watchlist_admins"] = watchlist_admins
        return []

    def fake_by_institution(rows, watchlist_ispb=None):
        captured["watchlist_ispb"] = watchlist_ispb
        return []

    monkeypatch.setattr(lambda_port, "_new_since_last_run", lambda source, docs, seed_if_empty=False: docs)
    monkeypatch.setattr(lambda_port, "_moves_since_last_run", lambda *a, **k: [])
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", fake_fetch_recent)
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", fake_fetch_funds)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", lambda base_date: {})
    monkeypatch.setattr(lambda_port.bcb_autorizacoes, "fetch_authorized", lambda: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "fetch_recent", lambda **kwargs: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "by_institution", fake_by_institution)
    monkeypatch.setenv("ONCA_LOOKBACK_DAYS", "14")
    monkeypatch.setenv("ONCA_COMPETITORS", "ITAU,BTG PACTUAL")
    monkeypatch.setenv("ONCA_COMPETITOR_ISPB", "18236120,60701190")

    lambda_port.lambda_handler({}, None)

    assert captured["days"] == 14
    assert captured["watchlist_admins"] == ["ITAU", "BTG PACTUAL"]
    assert captured["watchlist_ispb"] == ["18236120", "60701190"]


def test_lambda_handler_resolves_institution_names_in_market_share(monkeypatch):
    monkeypatch.setattr(lambda_port, "_new_since_last_run", lambda source, docs, seed_if_empty=False: docs)
    monkeypatch.setattr(lambda_port, "_moves_since_last_run", lambda *a, **k: [])
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
    monkeypatch.setattr(lambda_port.bcb_autorizacoes, "fetch_authorized", lambda: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "fetch_recent", lambda **kwargs: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "by_institution", lambda rows, watchlist_ispb=None: [])

    response = lambda_port.lambda_handler({}, None)

    payload = json.loads(response["body"])
    assert payload["market"]["items"] == [{"institution": "Banco Exemplo", "value": 100.0, "share_pct": 100.0}]


def test_lambda_handler_continues_when_normativos_fetch_raises(monkeypatch):
    def broken_fetch_recent(days=7, types=None):
        raise RuntimeError("BCB search page is down")

    monkeypatch.setattr(lambda_port, "_new_since_last_run", lambda source, docs, seed_if_empty=False: docs)
    monkeypatch.setattr(lambda_port, "_moves_since_last_run", lambda *a, **k: [])
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", broken_fetch_recent)
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", lambda base_date: {})
    monkeypatch.setattr(lambda_port.bcb_autorizacoes, "fetch_authorized", lambda: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "fetch_recent", lambda **kwargs: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "by_institution", lambda rows, watchlist_ispb=None: [])

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["regulatory"]["count"] == 0
    assert payload["regulatory"]["items"] == []


def test_lambda_handler_continues_when_cvm_funds_fetch_raises(monkeypatch):
    def broken_fetch_funds(watchlist_admins=None):
        raise RuntimeError("CVM CSV endpoint timed out")

    monkeypatch.setattr(lambda_port, "_new_since_last_run", lambda source, docs, seed_if_empty=False: docs)
    monkeypatch.setattr(lambda_port, "_moves_since_last_run", lambda *a, **k: [])
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", broken_fetch_funds)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", lambda base_date: {})
    monkeypatch.setattr(lambda_port.bcb_autorizacoes, "fetch_authorized", lambda: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "fetch_recent", lambda **kwargs: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "by_institution", lambda rows, watchlist_ispb=None: [])

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["competitor"]["count"] == 0
    assert payload["competitor"]["items"] == []


def test_lambda_handler_continues_when_ifdata_base_date_lookup_raises(monkeypatch):
    def broken_latest_base_date():
        raise RuntimeError("Could not determine an IF.data base date")

    monkeypatch.setattr(lambda_port, "_new_since_last_run", lambda source, docs, seed_if_empty=False: docs)
    monkeypatch.setattr(lambda_port, "_moves_since_last_run", lambda *a, **k: [])
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", broken_latest_base_date)
    monkeypatch.setattr(lambda_port.bcb_autorizacoes, "fetch_authorized", lambda: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "fetch_recent", lambda **kwargs: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "by_institution", lambda rows, watchlist_ispb=None: [])

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["market"]["count"] == 0
    assert payload["market"]["items"] == []


def test_lambda_handler_continues_when_institution_names_lookup_raises(monkeypatch):
    def broken_fetch_institution_names(base_date):
        raise RuntimeError("IfDataCadastro endpoint timed out")

    monkeypatch.setattr(lambda_port, "_new_since_last_run", lambda source, docs, seed_if_empty=False: docs)
    monkeypatch.setattr(lambda_port, "_moves_since_last_run", lambda *a, **k: [])
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(
        lambda_port.bcb_ifdata,
        "fetch_institutions",
        lambda base_date=None: [{"CodInst": "00068987", "NomeColuna": "Ativo Total", "Saldo": 100.0}],
    )
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", broken_fetch_institution_names)
    monkeypatch.setattr(lambda_port.bcb_autorizacoes, "fetch_authorized", lambda: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "fetch_recent", lambda **kwargs: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "by_institution", lambda rows, watchlist_ispb=None: [])

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["market"]["count"] == 0
    assert payload["market"]["items"] == []


def test_lambda_handler_continues_when_all_ingesters_raise(monkeypatch):
    monkeypatch.setattr(lambda_port, "_new_since_last_run", lambda source, docs, seed_if_empty=False: docs)
    monkeypatch.setattr(lambda_port, "_moves_since_last_run", lambda *a, **k: [])
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
    monkeypatch.setattr(
        lambda_port.bcb_autorizacoes,
        "fetch_authorized",
        lambda: (_ for _ in ()).throw(RuntimeError("auth down")),
    )
    monkeypatch.setattr(
        lambda_port.bcb_pix,
        "fetch_recent",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("pix down")),
    )

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["regulatory"]["count"] == 0
    assert payload["competitor"]["count"] == 0
    assert payload["market"]["count"] == 0
    assert payload["new_entrants"]["count"] == 0
    assert payload["pix_moves"]["move_count"] == 0


def test_lambda_handler_continues_when_s3_upload_fails(monkeypatch):
    monkeypatch.setattr(lambda_port, "_new_since_last_run", lambda source, docs, seed_if_empty=False: docs)
    monkeypatch.setattr(lambda_port, "_moves_since_last_run", lambda *a, **k: [])
    _stub_core_ingesters(monkeypatch)
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
    monkeypatch.setattr(
        lambda_port, "DynamoDbValueState", lambda source: DynamoDbValueState(source, table=fake_table)
    )
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", lambda base_date: {})
    monkeypatch.setattr(lambda_port.bcb_autorizacoes, "fetch_authorized", lambda: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "fetch_recent", lambda **kwargs: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "by_institution", lambda rows, watchlist_ispb=None: [])

    doc_a = {"id": "bcb:a", "subject": "Resolução A"}
    doc_b = {"id": "bcb:b", "subject": "Resolução B"}
    doc_c = {"id": "bcb:c", "subject": "Resolução C"}

    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [doc_a, doc_b])
    day_one = json.loads(lambda_port.lambda_handler({}, None)["body"])
    assert day_one["regulatory"]["count"] == 2
    assert day_one["regulatory"]["new_count"] == 2
    assert day_one["regulatory"]["items"] == [{**doc_a, "is_new": True}, {**doc_b, "is_new": True}]

    monkeypatch.setattr(
        lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [doc_a, doc_b, doc_c]
    )
    day_two = json.loads(lambda_port.lambda_handler({}, None)["body"])
    assert day_two["regulatory"]["count"] == 3
    assert day_two["regulatory"]["new_count"] == 1
    assert day_two["regulatory"]["items"] == [{**doc_c, "is_new": True}]


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
    monkeypatch.setattr(lambda_port.bcb_autorizacoes, "fetch_authorized", lambda: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "fetch_recent", lambda **kwargs: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "by_institution", lambda rows, watchlist_ispb=None: [])

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["regulatory"]["count"] == 1
    assert payload["regulatory"]["new_count"] == 1


def test_autorizacoes_first_run_seeds_silently(monkeypatch):
    fake_table = FakeStateTable()
    monkeypatch.setattr(lambda_port, "DynamoDbState", lambda source: DynamoDbState(source, table=fake_table))
    monkeypatch.setattr(
        lambda_port, "DynamoDbValueState", lambda source: DynamoDbValueState(source, table=fake_table)
    )
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", lambda base_date: {})
    monkeypatch.setattr(lambda_port.bcb_pix, "fetch_recent", lambda **kwargs: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "by_institution", lambda rows, watchlist_ispb=None: [])

    entity_a = {"id": "bcb-auth:1", "name": "Fintech A", "kind": "competitor", "source": "BCB-Autorizacoes"}
    entity_b = {"id": "bcb-auth:2", "name": "Fintech B", "kind": "competitor", "source": "BCB-Autorizacoes"}

    monkeypatch.setattr(
        lambda_port.bcb_autorizacoes, "fetch_authorized", lambda: [entity_a, entity_b]
    )
    day_one = json.loads(lambda_port.lambda_handler({}, None)["body"])
    assert day_one["new_entrants"]["count"] == 2
    assert day_one["new_entrants"]["new_count"] == 0
    assert day_one["new_entrants"]["items"] == []

    entity_c = {"id": "bcb-auth:3", "name": "Fintech C", "kind": "competitor", "source": "BCB-Autorizacoes"}
    monkeypatch.setattr(
        lambda_port.bcb_autorizacoes,
        "fetch_authorized",
        lambda: [entity_a, entity_b, entity_c],
    )
    day_two = json.loads(lambda_port.lambda_handler({}, None)["body"])
    assert day_two["new_entrants"]["count"] == 3
    assert day_two["new_entrants"]["new_count"] == 1
    assert day_two["new_entrants"]["items"] == [{**entity_c, "is_new": True}]


def test_pix_moves_detected_across_two_runs(monkeypatch):
    fake_table = FakeStateTable()
    monkeypatch.setattr(lambda_port, "DynamoDbState", lambda source: DynamoDbState(source, table=fake_table))
    monkeypatch.setattr(
        lambda_port, "DynamoDbValueState", lambda source: DynamoDbValueState(source, table=fake_table)
    )
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", lambda base_date: {})
    monkeypatch.setattr(lambda_port.bcb_autorizacoes, "fetch_authorized", lambda: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "fetch_recent", lambda **kwargs: [{"raw": True}])
    monkeypatch.setenv("ONCA_PIX_MOVE_THRESHOLD_PCT", "10.0")

    month_one = [{"ispb": "111", "institution": "Bank A", "tx_value": 100.0, "tx_count": 10}]
    month_two = [{"ispb": "111", "institution": "Bank A", "tx_value": 150.0, "tx_count": 15}]

    monkeypatch.setattr(lambda_port.bcb_pix, "by_institution", lambda rows, watchlist_ispb=None: month_one)
    day_one = json.loads(lambda_port.lambda_handler({}, None)["body"])
    assert day_one["pix_moves"]["institutions_tracked"] == 1
    assert day_one["pix_moves"]["move_count"] == 0  # baseline seed

    monkeypatch.setattr(lambda_port.bcb_pix, "by_institution", lambda rows, watchlist_ispb=None: month_two)
    day_two = json.loads(lambda_port.lambda_handler({}, None)["body"])
    assert day_two["pix_moves"]["move_count"] == 1
    move = day_two["pix_moves"]["items"][0]
    assert move["ispb"] == "111"
    assert move["pct_change"] == 50.0
    assert move["prev_value"] == 100.0


def test_sec_filings_first_run_seeds_silently(monkeypatch):
    fake_table = FakeStateTable()
    monkeypatch.setattr(lambda_port, "DynamoDbState", lambda source: DynamoDbState(source, table=fake_table))
    monkeypatch.setattr(
        lambda_port, "DynamoDbValueState", lambda source: DynamoDbValueState(source, table=fake_table)
    )
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", lambda base_date: {})
    monkeypatch.setattr(lambda_port.bcb_autorizacoes, "fetch_authorized", lambda: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "fetch_recent", lambda **kwargs: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "by_institution", lambda rows, watchlist_ispb=None: [])
    monkeypatch.setattr(lambda_port.cvm_ofertas, "fetch_recent", lambda **kwargs: [])
    monkeypatch.setenv("ONCA_SEC_TICKERS", "STNE,NU")

    f1 = {
        "id": "sec:1:acc-a",
        "source": "SEC-EDGAR",
        "kind": "competitor",
        "ticker": "STNE",
        "form": "6-K",
        "company": "StoneCo",
        "filed": "2026-06-01",
        "url": "https://www.sec.gov/Archives/edgar/data/1/a.htm",
    }
    f2 = {**f1, "id": "sec:1:acc-b", "form": "20-F"}

    monkeypatch.setattr(lambda_port.sec_filings, "fetch_filings", lambda *a, **k: [f1, f2])
    day_one = json.loads(lambda_port.lambda_handler({}, None)["body"])
    assert day_one["sec_filings"]["count"] == 2
    assert day_one["sec_filings"]["new_count"] == 0

    f3 = {**f1, "id": "sec:1:acc-c", "form": "6-K"}
    monkeypatch.setattr(lambda_port.sec_filings, "fetch_filings", lambda *a, **k: [f1, f2, f3])
    day_two = json.loads(lambda_port.lambda_handler({}, None)["body"])
    assert day_two["sec_filings"]["count"] == 3
    assert day_two["sec_filings"]["new_count"] == 1
    assert day_two["sec_filings"]["items"] == [{**f3, "is_new": True}]


def test_ofertas_first_run_seeds_silently(monkeypatch):
    fake_table = FakeStateTable()
    monkeypatch.setattr(lambda_port, "DynamoDbState", lambda source: DynamoDbState(source, table=fake_table))
    monkeypatch.setattr(
        lambda_port, "DynamoDbValueState", lambda source: DynamoDbValueState(source, table=fake_table)
    )
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", lambda base_date: {})
    monkeypatch.setattr(lambda_port.bcb_autorizacoes, "fetch_authorized", lambda: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "fetch_recent", lambda **kwargs: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "by_institution", lambda rows, watchlist_ispb=None: [])

    o1 = {
        "id": "cvm-oferta:r160:1",
        "source": "CVM-Ofertas",
        "kind": "competitor",
        "issuer": "ACME",
        "security": "Debêntures",
        "event_date": "2026-07-01",
        "url": "https://dados.cvm.gov.br/dataset/oferta-distrib",
    }
    o2 = {**o1, "id": "cvm-oferta:r160:2", "issuer": "BETA"}

    monkeypatch.setattr(lambda_port.cvm_ofertas, "fetch_recent", lambda **kwargs: [o1, o2])
    day_one = json.loads(lambda_port.lambda_handler({}, None)["body"])
    assert day_one["ofertas"]["count"] == 2
    assert day_one["ofertas"]["new_count"] == 0

    o3 = {**o1, "id": "cvm-oferta:r160:3", "issuer": "GAMA"}
    monkeypatch.setattr(lambda_port.cvm_ofertas, "fetch_recent", lambda **kwargs: [o1, o2, o3])
    day_two = json.loads(lambda_port.lambda_handler({}, None)["body"])
    assert day_two["ofertas"]["count"] == 3
    assert day_two["ofertas"]["new_count"] == 1
    assert day_two["ofertas"]["items"] == [{**o3, "is_new": True}]


def test_juros_moves_detected_across_two_runs(monkeypatch):
    fake_table = FakeStateTable()
    monkeypatch.setattr(lambda_port, "DynamoDbState", lambda source: DynamoDbState(source, table=fake_table))
    monkeypatch.setattr(
        lambda_port, "DynamoDbValueState", lambda source: DynamoDbValueState(source, table=fake_table)
    )
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", lambda base_date: {})
    monkeypatch.setattr(lambda_port.bcb_autorizacoes, "fetch_authorized", lambda: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "fetch_recent", lambda **kwargs: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "by_institution", lambda rows, watchlist_ispb=None: [])
    monkeypatch.setenv("ONCA_JUROS_MOVE_THRESHOLD_PCT", "10.0")
    monkeypatch.setenv("ONCA_JUROS_USE_DEFAULT_MODALITIES", "false")

    day1 = [
        {
            "move_key": "123|Cartão rotativo",
            "rate_year": 200.0,
            "institution": "Bank A",
            "modality": "Cartão rotativo",
            "cnpj8": "123",
        }
    ]
    day2 = [
        {
            "move_key": "123|Cartão rotativo",
            "rate_year": 250.0,
            "institution": "Bank A",
            "modality": "Cartão rotativo",
            "cnpj8": "123",
        }
    ]

    monkeypatch.setattr(lambda_port.bcb_juros, "fetch_daily", lambda **kwargs: day1)
    monkeypatch.setattr(
        lambda_port.bcb_juros, "filter_rates", lambda rows, institutions=None, modalities=None: rows
    )
    monkeypatch.setattr(lambda_port.bcb_juros, "for_moves", lambda rows: rows)

    first = json.loads(lambda_port.lambda_handler({}, None)["body"])
    assert first["juros_moves"]["series_tracked"] == 1
    assert first["juros_moves"]["move_count"] == 0

    monkeypatch.setattr(lambda_port.bcb_juros, "fetch_daily", lambda **kwargs: day2)
    second = json.loads(lambda_port.lambda_handler({}, None)["body"])
    assert second["juros_moves"]["move_count"] == 1
    move = second["juros_moves"]["items"][0]
    assert move["pct_change"] == 25.0
    assert move["prev_value"] == 200.0


def test_autorizacoes_state_failure_does_not_flood_new_entrants(monkeypatch):
    class BrokenState:
        def __init__(self, source):
            self.source = source
            self.seen = set()

        def load(self):
            raise RuntimeError("DynamoDB table unreachable")

    monkeypatch.setattr(lambda_port, "DynamoDbState", BrokenState)
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", lambda base_date: {})
    monkeypatch.setattr(
        lambda_port.bcb_autorizacoes,
        "fetch_authorized",
        lambda: [{"id": f"bcb-auth:{i}"} for i in range(100)],
    )
    monkeypatch.setattr(lambda_port.bcb_pix, "fetch_recent", lambda **kwargs: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "by_institution", lambda rows, watchlist_ispb=None: [])

    payload = json.loads(lambda_port.lambda_handler({}, None)["body"])
    assert payload["new_entrants"]["count"] == 100
    assert payload["new_entrants"]["new_count"] == 0


class FakeBedrockAgentClient:
    def __init__(self):
        self.ingestion_jobs = []

    def start_ingestion_job(self, knowledgeBaseId, dataSourceId):
        self.ingestion_jobs.append((knowledgeBaseId, dataSourceId))


def _stub_all_ingesters(monkeypatch, normativos=None, funds=None, entrants=None):
    monkeypatch.setattr(lambda_port, "_new_since_last_run", lambda source, docs, seed_if_empty=False: docs)
    monkeypatch.setattr(lambda_port, "_moves_since_last_run", lambda *a, **k: [])
    monkeypatch.setattr(lambda_port.bcb_normativos, "fetch_recent", lambda days=7, types=None: normativos or [])
    monkeypatch.setattr(lambda_port.cvm_fundos, "fetch_funds", lambda watchlist_admins=None: funds or [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "latest_base_date", lambda: 202603)
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institutions", lambda base_date=None: [])
    monkeypatch.setattr(lambda_port.bcb_ifdata, "fetch_institution_names", lambda base_date: {})
    monkeypatch.setattr(lambda_port.bcb_autorizacoes, "fetch_authorized", lambda: entrants or [])
    monkeypatch.setattr(lambda_port.bcb_pix, "fetch_recent", lambda **kwargs: [])
    monkeypatch.setattr(lambda_port.bcb_pix, "by_institution", lambda rows, watchlist_ispb=None: [])
    _stub_juros(monkeypatch)


def test_lambda_handler_writes_new_docs_and_triggers_kb_sync(monkeypatch):
    doc = {"id": "bcb:a", "source": "BCB", "kind": "regulatory", "subject": "x"}
    _stub_all_ingesters(monkeypatch, normativos=[doc])
    monkeypatch.setenv("ONCA_RAW_BUCKET", "onca-raw-test")
    monkeypatch.setenv("ONCA_KB_ID", "kb-123")
    monkeypatch.setenv("ONCA_KB_DATA_SOURCE_ID", "ds-456")

    captured = {}

    def fake_write_raw_documents(bucket, docs):
        captured["bucket"] = bucket
        captured["docs"] = docs
        return [f"BCB/{d['id']}.txt" for d in docs]

    monkeypatch.setattr(lambda_port.raw_writer, "write_raw_documents", fake_write_raw_documents)

    fake_bedrock = FakeBedrockAgentClient()
    monkeypatch.setattr(lambda_port.boto3, "client", lambda *args, **kwargs: fake_bedrock)

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    assert captured["bucket"] == "onca-raw-test"
    assert captured["docs"] == [doc]
    assert fake_bedrock.ingestion_jobs == [("kb-123", "ds-456")]


def test_lambda_handler_writes_new_entrants_to_corpus(monkeypatch):
    entrant = {
        "id": "bcb-auth:99",
        "source": "BCB-Autorizacoes",
        "kind": "competitor",
        "name": "New IP",
        "cnpj": "123",
    }
    _stub_all_ingesters(monkeypatch, entrants=[entrant])
    monkeypatch.setenv("ONCA_RAW_BUCKET", "onca-raw-test")
    monkeypatch.setenv("ONCA_KB_ID", "kb-123")
    monkeypatch.setenv("ONCA_KB_DATA_SOURCE_ID", "ds-456")

    captured = {}

    def fake_write_raw_documents(bucket, docs):
        captured["docs"] = docs
        return [f"{d['source']}/{d['id']}.txt" for d in docs]

    monkeypatch.setattr(lambda_port.raw_writer, "write_raw_documents", fake_write_raw_documents)
    fake_bedrock = FakeBedrockAgentClient()
    monkeypatch.setattr(lambda_port.boto3, "client", lambda *args, **kwargs: fake_bedrock)

    lambda_port.lambda_handler({}, None)

    assert captured["docs"] == [entrant]
    assert fake_bedrock.ingestion_jobs == [("kb-123", "ds-456")]


def test_lambda_handler_skips_kb_sync_when_no_new_docs(monkeypatch):
    _stub_all_ingesters(monkeypatch)
    monkeypatch.setenv("ONCA_RAW_BUCKET", "onca-raw-test")
    monkeypatch.setenv("ONCA_KB_ID", "kb-123")
    monkeypatch.setenv("ONCA_KB_DATA_SOURCE_ID", "ds-456")

    calls = []
    monkeypatch.setattr(lambda_port.raw_writer, "write_raw_documents", lambda bucket, docs: calls.append(docs) or [])

    fake_bedrock = FakeBedrockAgentClient()
    monkeypatch.setattr(lambda_port.boto3, "client", lambda *args, **kwargs: fake_bedrock)

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    assert calls == [[]]
    assert fake_bedrock.ingestion_jobs == []


def test_lambda_handler_skips_corpus_write_when_raw_bucket_not_configured(monkeypatch):
    doc = {"id": "bcb:a", "source": "BCB", "kind": "regulatory", "subject": "x"}
    _stub_all_ingesters(monkeypatch, normativos=[doc])

    called = []
    monkeypatch.setattr(lambda_port.raw_writer, "write_raw_documents", lambda bucket, docs: called.append(1) or [])

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    assert called == []


def test_lambda_handler_continues_when_raw_corpus_write_fails(monkeypatch):
    doc = {"id": "bcb:a", "source": "BCB", "kind": "regulatory", "subject": "x"}
    _stub_all_ingesters(monkeypatch, normativos=[doc])
    monkeypatch.setenv("ONCA_RAW_BUCKET", "onca-raw-test")

    def broken_write(bucket, docs):
        raise RuntimeError("S3 write failed")

    monkeypatch.setattr(lambda_port.raw_writer, "write_raw_documents", broken_write)

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["regulatory"]["new_count"] == 1


def test_lambda_handler_continues_when_kb_sync_fails(monkeypatch):
    doc = {"id": "bcb:a", "source": "BCB", "kind": "regulatory", "subject": "x"}
    _stub_all_ingesters(monkeypatch, normativos=[doc])
    monkeypatch.setenv("ONCA_RAW_BUCKET", "onca-raw-test")
    monkeypatch.setenv("ONCA_KB_ID", "kb-123")
    monkeypatch.setenv("ONCA_KB_DATA_SOURCE_ID", "ds-456")
    monkeypatch.setattr(lambda_port.raw_writer, "write_raw_documents", lambda bucket, docs: ["BCB/bcb:a.txt"])

    class BrokenBedrockClient:
        def start_ingestion_job(self, **kwargs):
            raise RuntimeError("KB sync failed")

    monkeypatch.setattr(lambda_port.boto3, "client", lambda *args, **kwargs: BrokenBedrockClient())

    response = lambda_port.lambda_handler({}, None)

    assert response["statusCode"] == 200


def test_detect_moves_with_dynamodb_value_state():
    fake = FakeStateTable()
    state = DynamoDbValueState("bcb_pix", table=fake)
    items_v1 = [{"ispb": "1", "tx_value": 10.0}]
    assert detect_moves("bcb_pix", items_v1, "ispb", "tx_value", min_pct=10, state=state) == []

    state2 = DynamoDbValueState("bcb_pix", table=fake)
    items_v2 = [{"ispb": "1", "tx_value": 20.0}]
    moves = detect_moves("bcb_pix", items_v2, "ispb", "tx_value", min_pct=10, state=state2)
    assert len(moves) == 1
    assert moves[0]["pct_change"] == 100.0
