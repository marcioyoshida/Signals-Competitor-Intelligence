import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import bcb_normativos


def test_fetch_recent_returns_empty_on_request_error(monkeypatch, capsys):
    def fake_get(*args, **kwargs):
        raise requests.HTTPError("bad request")

    monkeypatch.setattr(bcb_normativos.requests, "get", fake_get)

    result = bcb_normativos.fetch_recent(days=7)

    assert result == []
    assert "Warning: BCB normativos fetch failed" in capsys.readouterr().out
