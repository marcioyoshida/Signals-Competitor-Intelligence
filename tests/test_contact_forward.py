import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from src.dashboard import contact_forward as cf  # noqa: E402

PLAIN_MIME = (
    b"From: Prospect <buyer@example.com>\r\n"
    b"To: contato@onssa.org\r\n"
    b"Subject: Interesse em Onca\r\n"
    b"Date: Mon, 21 Sep 2026 12:00:00 +0000\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Ola, gostaria de saber mais sobre o plano Entry.\r\n"
)

MULTIPART_MIME = (
    b"From: Prospect <buyer@example.com>\r\n"
    b"To: contato@onssa.org\r\n"
    b"Subject: Duvida\r\n"
    b"Content-Type: multipart/alternative; boundary=\"B\"\r\n"
    b"\r\n"
    b"--B\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Corpo em texto simples.\r\n"
    b"--B\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"\r\n"
    b"<p>Corpo em HTML.</p>\r\n"
    b"--B--\r\n"
)

NO_BODY_MIME = (
    b"From: buyer@example.com\r\n"
    b"Subject: Vazio\r\n"
    b"Content-Type: text/html\r\n"
    b"\r\n"
    b"<p>so html</p>\r\n"
)


class FakeSes:
    def __init__(self):
        self.sent = []

    def send_email(self, **kw):
        self.sent.append(kw)
        return {"MessageId": "fake"}


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        class Body:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

        return {"Body": Body(self.objects[(Bucket, Key)])}


# --- build_forward -----------------------------------------------------------

def test_build_forward_extracts_sender_subject_and_body():
    parts = cf.build_forward(PLAIN_MIME, "ops@example.com")
    assert parts["reply_to"] == "Prospect <buyer@example.com>"
    assert "Interesse em Onca" in parts["subject"]
    assert "gostaria de saber mais" in parts["text"]
    assert parts["to"] == "ops@example.com"


def test_build_forward_prefers_plain_text_part_of_a_multipart_message():
    parts = cf.build_forward(MULTIPART_MIME, "ops@example.com")
    assert "Corpo em texto simples." in parts["text"]
    assert "<p>" not in parts["text"]


def test_build_forward_never_crashes_on_an_html_only_message():
    # fail closed on content, not on delivery: an unreadable body must still forward
    # with a placeholder, not raise and drop the message.
    parts = cf.build_forward(NO_BODY_MIME, "ops@example.com")
    assert "sem corpo" in parts["text"]
    assert parts["reply_to"] == "buyer@example.com"


# --- forward_object ------------------------------------------------------------

def test_forward_object_sends_via_ses_from_the_role_address(monkeypatch):
    monkeypatch.setenv("ONCA_CONTACT_FORWARD_TO", "ops@example.com")
    s3 = FakeS3({("bucket", "contato/msg1"): PLAIN_MIME})
    ses = FakeSes()

    report = cf.forward_object("bucket", "contato/msg1", s3=s3, ses=ses)

    assert report["status"] == "forwarded"
    assert len(ses.sent) == 1
    sent = ses.sent[0]
    assert sent["Source"] == cf.FROM_ADDRESS
    assert sent["Destination"]["ToAddresses"] == ["ops@example.com"]
    assert "Prospect <buyer@example.com>" in sent["ReplyToAddresses"][0]


def test_forward_object_fails_closed_with_no_configured_recipient(monkeypatch):
    monkeypatch.delenv("ONCA_CONTACT_FORWARD_TO", raising=False)
    s3 = FakeS3({("bucket", "k"): PLAIN_MIME})
    with pytest.raises(RuntimeError):
        cf.forward_object("bucket", "k", s3=s3, ses=FakeSes())


# --- lambda_handler --------------------------------------------------------------

def test_handler_forwards_every_record_in_an_s3_event(monkeypatch):
    monkeypatch.setenv("ONCA_CONTACT_FORWARD_TO", "ops@example.com")
    s3 = FakeS3({("bucket", "contato/a"): PLAIN_MIME, ("bucket", "contato/b"): MULTIPART_MIME})
    ses = FakeSes()
    monkeypatch.setattr(cf, "_s3_client", lambda client=None: client or s3)
    monkeypatch.setattr(cf, "_client", lambda client=None: client or ses)

    event = {"Records": [
        {"s3": {"bucket": {"name": "bucket"}, "object": {"key": "contato/a"}}},
        {"s3": {"bucket": {"name": "bucket"}, "object": {"key": "contato/b"}}},
    ]}
    out = cf.lambda_handler(event)
    assert len(out["forwarded"]) == 2
    assert all(r["status"] == "forwarded" for r in out["forwarded"])
    assert len(ses.sent) == 2


def test_handler_url_decodes_the_object_key(monkeypatch):
    monkeypatch.setenv("ONCA_CONTACT_FORWARD_TO", "ops@example.com")
    key = "contato/2026 09 21/msg"
    encoded = urllib.parse.quote_plus(key)
    s3 = FakeS3({("bucket", key): PLAIN_MIME})
    ses = FakeSes()
    monkeypatch.setattr(cf, "_s3_client", lambda client=None: client or s3)
    monkeypatch.setattr(cf, "_client", lambda client=None: client or ses)

    event = {"Records": [
        {"s3": {"bucket": {"name": "bucket"}, "object": {"key": encoded}}},
    ]}
    out = cf.lambda_handler(event)
    assert out["forwarded"][0]["status"] == "forwarded"


def test_handler_one_bad_record_does_not_block_the_others(monkeypatch):
    monkeypatch.setenv("ONCA_CONTACT_FORWARD_TO", "ops@example.com")
    s3 = FakeS3({("bucket", "contato/good"): PLAIN_MIME})
    ses = FakeSes()
    monkeypatch.setattr(cf, "_s3_client", lambda client=None: client or s3)
    monkeypatch.setattr(cf, "_client", lambda client=None: client or ses)

    event = {"Records": [
        {"s3": {"bucket": {"name": "bucket"}, "object": {"key": "contato/missing"}}},
        {"s3": {"bucket": {"name": "bucket"}, "object": {"key": "contato/good"}}},
    ]}
    out = cf.lambda_handler(event)
    statuses = [r["status"] for r in out["forwarded"]]
    assert statuses == ["error", "forwarded"]


def test_handler_ignores_malformed_records():
    out = cf.lambda_handler({"Records": [{"s3": {}}, {}]})
    assert out["forwarded"] == []
