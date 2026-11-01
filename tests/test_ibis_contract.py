"""#293: Onça pins Ibis's contract (1.26.0) and validates what it consumes against it."""
import json
from pathlib import Path

import jsonschema
import pytest

from src.dashboard import ibis

PIN = Path(__file__).parent / "contracts" / "ibis" / "1.26.0"


def _schema(name):
    return json.loads((PIN / name).read_text())


def test_recorded_delivery_is_a_valid_webhook_event():
    jsonschema.validate(json.loads((PIN / "recorded_webhook_event.json").read_text()),
                        _schema("WebhookEvent.schema.json"))


def test_recorded_answer_is_a_valid_answer():
    p = PIN / "recorded_answer.json"
    if not p.exists():
        pytest.skip("recorded after Onça's Ibis key exists")
    jsonschema.validate(json.loads(p.read_text()), _schema("Answer.schema.json"))
    view = ibis._answer_view(json.loads(p.read_text()))
    assert view and view["text"] == json.loads(p.read_text())["text"]


def test_the_fields_onca_stores_exist_in_the_pinned_schema():
    w = _schema("WebhookEvent.schema.json")
    assert {"text_pt_br", "disclaimer_pt_br", "watch", "entry", "event", "delivery_id"} <= set(w["properties"])
    assert {"url", "register", "regulator", "name_as_published"} <= set(w["$defs"]["EntryRef"]["properties"])
    assert {"evidence", "detected_at", "type"} <= set(w["$defs"]["Event"]["properties"])
    assert "lookup_key" in w["$defs"]["Watch"]["properties"]


def test_onca_uses_only_routes_the_pinned_openapi_declares():
    paths = _schema("openapi.json")["paths"]
    for method, path in (("post", "/v1/watchlist"), ("delete", "/v1/watchlist"), ("post", "/v1/screen"),
                         ("get", "/v1/check"), ("put", "/v1/webhook"), ("post", "/v1/webhook/test")):
        assert method in paths[path], (method, path)
    assert "servers" not in _schema("openapi.json")       # the Ibis endpoint stays out of this repo


def test_recorded_fixture_names_no_real_company():
    d = json.loads((PIN / "recorded_webhook_event.json").read_text())
    assert d["entry"]["name_as_published"] == "Exemplo Cripto GmbH"
    assert d["entry"]["url"].startswith("https://example.org/")
