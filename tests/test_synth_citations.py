import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth.citations import (
    collect_allowed_urls,
    enforce_citations,
    extract_urls,
    scrub_fake_url_tokens,
)


def test_collect_allowed_urls_normalizes():
    sources = [
        {"url": "https://www.bcb.gov.br/demo/resolucao-1."},
        {"url": "https://dados.cvm.gov.br/dataset/oferta-distrib"},
        {"id": "no-url"},
    ]
    allowed = collect_allowed_urls(sources)
    assert "https://www.bcb.gov.br/demo/resolucao-1" in allowed
    assert "https://dados.cvm.gov.br/dataset/oferta-distrib" in allowed


def test_enforce_drops_sentence_with_unknown_url():
    sources = [{"url": "https://www.bcb.gov.br/ok", "id": "a"}]
    text = (
        "Regulatory change is material. "
        "See https://www.bcb.gov.br/ok for the filing. "
        "Ignore https://evil.example/fake claim."
    )
    result = enforce_citations(text, sources)
    assert result["ok"]
    assert "evil.example" not in result["narrative"]
    assert "https://www.bcb.gov.br/ok" in result["narrative"]
    assert any(c["url"] == "https://www.bcb.gov.br/ok" for c in result["citations"])
    assert "https://evil.example/fake" in result["dropped_urls"]


def test_enforce_rejects_narrative_with_only_bad_urls():
    sources = [{"url": "https://www.bcb.gov.br/ok"}]
    result = enforce_citations("See https://evil.example/x only.", sources)
    assert result["ok"] is False
    assert result["narrative"] == ""


def test_extract_urls():
    urls = extract_urls("a https://x.com/y, and https://z.com/w.")
    assert "https://x.com/y" in urls
    assert "https://z.com/w" in urls


def test_scrub_fake_url_tokens_removes_nonlink_placeholders():
    assert scrub_fake_url_tokens("está ativo (URL: None).") == "está ativo."
    assert scrub_fake_url_tokens("fundo (URL: CVM-Ofertas) e outro.") == "fundo e outro."
    # Real links survive; adjacent fake token is removed.
    assert (
        scrub_fake_url_tokens("veja https://x.gov/a (URL: BCB-Auth) aqui.")
        == "veja https://x.gov/a aqui."
    )


def test_scrub_fake_url_tokens_handles_angle_brackets():
    # Source-name pseudo-link in angle brackets is dropped.
    assert (
        scrub_fake_url_tokens("autorização do BC <BCB-Autorizacoes>.")
        == "autorização do BC."
    )
    # Real link wrapped in angle brackets is unwrapped, not dropped.
    assert (
        scrub_fake_url_tokens("perda <https://dados.cvm.gov.br/x> registrada.")
        == "perda https://dados.cvm.gov.br/x registrada."
    )


def test_enforce_strips_fake_url_then_cites_from_sources():
    sources = [{"url": "https://dados.cvm.gov.br/oferta", "id": "o1", "source": "CVM"}]
    result = enforce_citations("Novo fundo lançado (URL: CVM-Ofertas).", sources)
    assert result["ok"]
    assert "URL:" not in result["narrative"]
    assert any(c.get("url") == "https://dados.cvm.gov.br/oferta" for c in result["citations"])
