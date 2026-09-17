"""bedrock_llm.converse() — text-only behavior must stay unchanged; `images` (ADR 027 #132,
the Bedrock-vision QA check) is additive."""
from __future__ import annotations

from unittest import mock

from src.synth import bedrock_llm


def _fake_client(response_text="ok"):
    client = mock.Mock()
    client.converse.return_value = {
        "output": {"message": {"content": [{"text": response_text}]}},
        "usage": {"inputTokens": 12, "outputTokens": 3},
    }
    return client


def test_text_only_call_has_no_image_blocks():
    client = _fake_client()
    with mock.patch("boto3.client", return_value=client):
        out = bedrock_llm.converse("hello")
    assert out == "ok"
    content = client.converse.call_args.kwargs["messages"][0]["content"]
    assert content == [{"text": "hello"}]


def test_images_are_appended_as_image_content_blocks():
    client = _fake_client()
    png_bytes = b"\x89PNG\r\n\x1a\nfakebytes"
    with mock.patch("boto3.client", return_value=client):
        out = bedrock_llm.converse("describe this", images=[png_bytes])
    assert out == "ok"
    content = client.converse.call_args.kwargs["messages"][0]["content"]
    assert content == [
        {"text": "describe this"},
        {"image": {"format": "png", "source": {"bytes": png_bytes}}},
    ]


def test_multiple_images_each_get_their_own_block():
    client = _fake_client()
    with mock.patch("boto3.client", return_value=client):
        bedrock_llm.converse("compare these", images=[b"one", b"two"])
    content = client.converse.call_args.kwargs["messages"][0]["content"]
    assert len(content) == 3
    assert content[1]["image"]["source"]["bytes"] == b"one"
    assert content[2]["image"]["source"]["bytes"] == b"two"


def test_bedrock_failure_with_images_still_returns_none_not_raises():
    with mock.patch("boto3.client", side_effect=RuntimeError("denied")):
        out = bedrock_llm.converse("describe this", images=[b"whatever"])
    assert out is None
