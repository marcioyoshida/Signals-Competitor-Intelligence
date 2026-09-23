import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.diff import engine as diff_engine  # noqa: E402
from src.ingest import raw_writer, tenant_s3  # noqa: E402


class FakeS3Client:
    """Enough of boto3's S3 client surface for tenant_s3: paginated listing
    (`get_paginator("list_objects_v2")`) and `get_object`."""

    def __init__(self, objects: dict[str, dict]):
        # key -> {"body": bytes, "etag": str, "last_modified": dt.datetime}
        self._objects = objects
        self.put_calls: list[tuple[str, str]] = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        client = self

        class _Paginator:
            def paginate(self, Bucket, Prefix=""):
                contents = [
                    {
                        "Key": key,
                        "ETag": f'"{meta["etag"]}"',
                        "LastModified": meta.get("last_modified"),
                    }
                    for key, meta in client._objects.items()
                    if key.startswith(Prefix)
                ]
                yield {"Contents": contents}

        return _Paginator()

    def get_object(self, Bucket, Key):
        meta = self._objects[Key]
        body = meta["body"]

        class _Body:
            def read(self_inner):
                return body

        return {"Body": _Body()}

    def put_object(self, Bucket, Key, Body):
        self.put_calls.append((Bucket, Key))


def _reset_state(monkeypatch, tmp_path):
    monkeypatch.setattr(diff_engine, "STATE_DIR", tmp_path)


# --- _configured_buckets --------------------------------------------------

def test_unconfigured_buckets_yields_no_docs(monkeypatch):
    monkeypatch.delenv("ONCA_TENANT_PRIVATE_BUCKETS", raising=False)
    assert tenant_s3.collect() == []


def test_parses_bucket_and_prefix_pairs(monkeypatch):
    monkeypatch.setenv("ONCA_TENANT_PRIVATE_BUCKETS", " tenant-docs:deals/ , tenant-notes ")
    assert tenant_s3._configured_buckets() == [
        ("tenant-docs", "deals/"),
        ("tenant-notes", ""),
    ]


# --- collect() -------------------------------------------------------------

def test_collect_returns_new_text_docs(monkeypatch, tmp_path):
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setenv("ONCA_TENANT_PRIVATE_BUCKETS", "tenant-docs:deals/")
    fake = FakeS3Client(
        {
            "deals/memo1.txt": {
                "body": b"Acme is exploring a sale process.",
                "etag": "aaa",
                "last_modified": dt.datetime(2026, 9, 1),
            }
        }
    )

    docs = tenant_s3.collect(s3=fake, state=diff_engine.JsonState("tenant_s3_test1"))

    assert len(docs) == 1
    doc = docs[0]
    assert doc["source"] == "tenant_s3"
    assert doc["kind"] == "tenant_private"
    assert doc["bucket"] == "tenant-docs"
    assert doc["key"] == "deals/memo1.txt"
    assert doc["title"] == "memo1.txt"
    assert doc["text"] == "Acme is exploring a sale process."
    assert doc["url"] == "s3://tenant-docs/deals/memo1.txt"
    assert doc["date"] == "2026-09-01"
    assert doc["id"] == "tenant-docs/deals/memo1.txt#aaa"


def test_collect_skips_folder_markers_and_non_text_extensions(monkeypatch, tmp_path):
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setenv("ONCA_TENANT_PRIVATE_BUCKETS", "tenant-docs")
    fake = FakeS3Client(
        {
            "deals/": {"body": b"", "etag": "x", "last_modified": None},
            "deals/deck.pdf": {"body": b"%PDF-1.4...", "etag": "y", "last_modified": None},
            "deals/note.txt": {"body": b"real content", "etag": "z", "last_modified": None},
        }
    )

    docs = tenant_s3.collect(s3=fake, state=diff_engine.JsonState("tenant_s3_test2"))

    assert [d["key"] for d in docs] == ["deals/note.txt"]


def test_collect_skips_objects_over_the_size_cap(monkeypatch, tmp_path):
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setenv("ONCA_TENANT_PRIVATE_BUCKETS", "tenant-docs")
    monkeypatch.setattr(tenant_s3, "MAX_OBJECT_BYTES", 10)
    fake = FakeS3Client(
        {"big.txt": {"body": b"way more than ten bytes of content", "etag": "a", "last_modified": None}}
    )

    docs = tenant_s3.collect(s3=fake, state=diff_engine.JsonState("tenant_s3_test3"))
    assert docs == []


def test_collect_dedupes_across_runs_via_seen_state(monkeypatch, tmp_path):
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setenv("ONCA_TENANT_PRIVATE_BUCKETS", "tenant-docs")
    fake = FakeS3Client(
        {"memo.txt": {"body": b"unchanged content", "etag": "same", "last_modified": None}}
    )
    state = diff_engine.JsonState("tenant_s3_test4")

    first = tenant_s3.collect(s3=fake, state=state)
    second = tenant_s3.collect(s3=fake, state=state)

    assert len(first) == 1
    assert second == []


def test_collect_treats_an_edited_object_as_new(monkeypatch, tmp_path):
    # Same key, new etag (the object was overwritten in place) must resolve
    # to a NEW doc id — a corrected memo re-uploaded over the old one must
    # re-ingest, not vanish into the seen-set forever.
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setenv("ONCA_TENANT_PRIVATE_BUCKETS", "tenant-docs")
    state = diff_engine.JsonState("tenant_s3_test5")

    fake_v1 = FakeS3Client({"memo.txt": {"body": b"draft", "etag": "v1", "last_modified": None}})
    first = tenant_s3.collect(s3=fake_v1, state=state)
    assert len(first) == 1

    fake_v2 = FakeS3Client({"memo.txt": {"body": b"final", "etag": "v2", "last_modified": None}})
    second = tenant_s3.collect(s3=fake_v2, state=state)
    assert len(second) == 1
    assert second[0]["text"] == "final"


def test_collect_skips_empty_or_whitespace_only_objects(monkeypatch, tmp_path):
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setenv("ONCA_TENANT_PRIVATE_BUCKETS", "tenant-docs")
    fake = FakeS3Client(
        {"blank.txt": {"body": b"   \n  ", "etag": "a", "last_modified": None}}
    )
    docs = tenant_s3.collect(s3=fake, state=diff_engine.JsonState("tenant_s3_test6"))
    assert docs == []


# --- ingest() ----------------------------------------------------------------

def test_ingest_no_ops_without_a_raw_bucket(monkeypatch, tmp_path):
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.delenv("ONCA_RAW_BUCKET", raising=False)
    monkeypatch.setenv("ONCA_TENANT_PRIVATE_BUCKETS", "tenant-docs")
    fake = FakeS3Client({"memo.txt": {"body": b"content", "etag": "a", "last_modified": None}})

    assert tenant_s3.ingest(s3=fake, state=diff_engine.JsonState("tenant_s3_test7")) == []
    assert fake.put_calls == []


def test_ingest_writes_new_docs_into_the_configured_raw_bucket(monkeypatch, tmp_path):
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setenv("ONCA_TENANT_PRIVATE_BUCKETS", "tenant-docs:deals/")
    fake = FakeS3Client(
        {
            "deals/memo.txt": {
                "body": b"Acme is exploring a sale process.",
                "etag": "aaa",
                "last_modified": None,
            }
        }
    )
    # ingest() reads through the `s3` it's given, but hands writing off to
    # raw_writer.write_raw_documents, which (matching every other ingester's
    # convention) opens its own client — so the write side is exercised by
    # patching THAT client, same as tests/test_raw_writer.py.
    write_fake = FakeS3Client({})
    monkeypatch.setattr(raw_writer.boto3, "client", lambda *a, **k: write_fake)

    written = tenant_s3.ingest(
        raw_bucket="onca-tenant-raw-test",
        s3=fake,
        state=diff_engine.JsonState("tenant_s3_test8"),
    )

    assert written == ["tenant_s3/tenant-docs/deals/memo.txt#aaa.txt"]
    assert write_fake.put_calls == [
        ("onca-tenant-raw-test", "tenant_s3/tenant-docs/deals/memo.txt#aaa.txt"),
        ("onca-tenant-raw-test", "tenant_s3/tenant-docs/deals/memo.txt#aaa.txt.metadata.json"),
    ]


# --- raw_writer wiring for kind == "tenant_private" -------------------------

def test_raw_writer_writes_tenant_private_text_verbatim():
    doc = {
        "id": "tenant-docs/deals/memo.txt#aaa",
        "source": "tenant_s3",
        "kind": "tenant_private",
        "title": "memo.txt",
        "text": "Acme is exploring a sale process.",
        "date": "2026-09-01",
        "url": "s3://tenant-docs/deals/memo.txt",
    }
    assert raw_writer._document_text(doc) == "Acme is exploring a sale process."
    attrs = raw_writer._metadata_attributes(doc)
    assert attrs["name"] == "memo.txt"
    assert attrs["date"] == "2026-09-01"
    assert attrs["url"] == "s3://tenant-docs/deals/memo.txt"
