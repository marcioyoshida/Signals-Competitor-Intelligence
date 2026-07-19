"""Write diffed regulatory/competitor documents to the raw corpus bucket.

Feeds the Bedrock Knowledge Base's S3 data source: one text object per
document plus a `.metadata.json` sidecar (Bedrock's documented convention —
same key + `.metadata.json`, same folder) carrying the fields needed for
citations (source, url, date, doc_type). Market share is aggregate numeric
data, not a citable document, so it's never written here.
"""
from __future__ import annotations

import json
from typing import Any

import boto3


def _document_text(doc: dict[str, Any]) -> str:
    if doc.get("kind") == "regulatory":
        return f"{doc.get('doc_type')} N° {doc.get('number')}\n\n{doc.get('subject') or ''}"
    if doc.get("kind") == "competitor":
        # CVM fund filing
        if doc.get("fund_name") or doc.get("admin"):
            lines = [doc.get("fund_name") or "", f"Administrator: {doc.get('admin') or ''}"]
            if doc.get("manager"):
                lines.append(f"Manager: {doc['manager']}")
            if doc.get("fund_class"):
                lines.append(f"Class: {doc['fund_class']}")
            return "\n".join(line for line in lines if line)
        # BCB autorizações / new-entrant entity
        if doc.get("name") or doc.get("cnpj"):
            lines = [
                doc.get("name") or "Unknown entity",
                f"CNPJ: {doc.get('cnpj') or ''}",
                f"Entity type: {doc.get('entity_type') or ''}",
            ]
            if doc.get("legal_nature"):
                lines.append(f"Legal nature: {doc['legal_nature']}")
            if doc.get("situation"):
                lines.append(f"Situation: {doc['situation']}")
            return "\n".join(lines)
        return json.dumps(
            {k: v for k, v in doc.items() if k != "raw"}, ensure_ascii=False
        )
    return json.dumps(doc, ensure_ascii=False)


def _metadata_attributes(doc: dict[str, Any]) -> dict[str, str]:
    attrs = {
        "source": doc.get("source"),
        "kind": doc.get("kind"),
        "doc_type": doc.get("doc_type")
        or doc.get("fund_class")
        or doc.get("entity_type"),
        "date": doc.get("date") or doc.get("registered"),
        "url": doc.get("url"),
        "cnpj": doc.get("cnpj"),
        "name": doc.get("name") or doc.get("fund_name") or doc.get("admin"),
    }
    return {k: v for k, v in attrs.items() if v}


def write_raw_documents(bucket: str, docs: list[dict[str, Any]]) -> list[str]:
    """Write each doc as a text object + Bedrock KB metadata sidecar.

    docs are expected to already be diffed (only genuinely new items) —
    this function doesn't re-filter. Returns the S3 keys written.
    """
    s3 = boto3.client("s3")
    written: list[str] = []
    for doc in docs:
        source = doc.get("source", "unknown")
        key = f"{source}/{doc['id']}.txt"
        s3.put_object(Bucket=bucket, Key=key, Body=_document_text(doc).encode("utf-8"))
        s3.put_object(
            Bucket=bucket,
            Key=f"{key}.metadata.json",
            Body=json.dumps({"metadataAttributes": _metadata_attributes(doc)}, ensure_ascii=False).encode("utf-8"),
        )
        written.append(key)
    return written
