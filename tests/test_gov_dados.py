"""dados.gov.br catalog client (#63 route)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import gov_dados as gd


def _fetcher(responses):
    def f(url, params, headers):
        assert headers.get("chave-api-dados-abertos") == "TOK"     # auth header sent
        for frag, body in responses.items():
            if frag in url and (params is None or frag in url):
                return body
        return None
    return f


def setup_function(_):
    gd.clear_cache()
    gd._TOKEN_CACHE["v"] = "TOK"                                    # inject a token


def test_no_token_is_fail_closed(monkeypatch):
    gd.clear_cache()
    monkeypatch.delenv("GOV_DADOS_TOKEN", raising=False)
    monkeypatch.setattr(gd, "token", lambda: None)
    assert gd.search_datasets("x") == []
    assert gd.find_resource("x") is None


def test_search_and_resources_and_find():
    responses = {
        "/conjuntos-dados?": None,  # unused; requests use params not query in URL here
    }

    def fetcher(url, params, headers):
        assert headers["chave-api-dados-abertos"] == "TOK"
        if url.endswith("/conjuntos-dados"):
            assert params == {"nomeConjuntoDados": "consumidor.gov.br"}
            return [{"id": "ds1", "title": "Consumidor.gov"}, {"nome": "no-id"}]
        if url.endswith("/conjuntos-dados/ds1"):
            return {"recursos": [
                {"link": "https://x/2026.pdf", "formato": "pdf", "nomeArquivo": "manual.pdf"},
                {"link": "https://x/finalizadas_2026-07.zip", "formato": "csv",
                 "nomeArquivo": "finalizadas_2026-07.zip", "titulo": "Dados"},
            ]}
        return None

    ds = gd.search_datasets("consumidor.gov.br", fetcher=fetcher)
    assert [d["id"] for d in ds] == ["ds1"]                        # the id-less row dropped
    res = gd.dataset_resources("ds1", fetcher=fetcher)
    assert len(res) == 2 and res[0]["formato"] == "PDF"
    found = gd.find_resource("consumidor.gov.br", name_contains="finalizadas", fetcher=fetcher)
    assert found["link"].endswith("finalizadas_2026-07.zip") and found["dataset_id"] == "ds1"


def test_find_resource_format_filter():
    def fetcher(url, params, headers):
        if url.endswith("/conjuntos-dados"):
            return [{"id": "ds1"}]
        return {"recursos": [{"link": "u.pdf", "formato": "PDF"}]}
    assert gd.find_resource("q", formato="CSV", fetcher=fetcher) is None
