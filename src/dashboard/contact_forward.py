"""E3 (#155): forward inbound mail to `contato@onssa.org` to a real inbox.

Onça publishes a role address (`contato@onssa.org`) as its public contact channel —
the pricing/sample pages must never show a personal Gmail as the company's commercial
front door. But `onssa.org` was, until this, a SEND-only SES identity (its SPF record
locks it to `amazonses.com`); there is no mailbox behind that address to actually read.

This is the receiving half. SES's receipt rule (`infra/app.py`, rule "onca-contact" in
the shared account-wide rule set — SES allows only ONE active rule set per region, so
this is a RULE added to it, not a rule set of its own) writes each inbound message to
S3 as a raw MIME object; this Lambda fires on that S3 write and re-sends the message
to the real recipient via `ses:SendRawEmail`.

**Why forward rather than just make `contato@` an alias in the SES config:** SES has
no alias/forwarding primitive — a receipt rule can only STORE mail (S3/SNS) or hand it
to a Lambda; getting it into an actual inbox requires this step regardless of where
that inbox lives.

**Why re-send rather than relay the raw bytes verbatim:** the original `From` almost
certainly fails SPF/DKIM/DMARC against the recipient's domain (it was never sent BY
us), so a byte-for-byte relay risks silent spam-filtering at the recipient. Sending a
fresh message FROM our own DKIM-verified domain, with the original sender preserved in
`Reply-To` and the original body quoted, keeps the reply path intact without inheriting
someone else's authentication failure.

The forwarding address is operator configuration, not a wired-in address. It lives in the
SSM parameter named by `ONCA_CONTACT_FORWARD_PARAM`, read at send time, so the operator's
inbox is never in the repo and no deploy can blank it. The earlier synth-time
`ONCA_CONTACT_FORWARD_TO` env var did exactly that: every deploy from a shell without it
reset the live value to empty, and contato@ silently forwarded nowhere. The env var still
wins when set (tests, local runs).
"""
from __future__ import annotations

import email
import os
from email import policy
from typing import Any

FROM_ADDRESS = "contato@onssa.org"


def _client(client: Any | None = None) -> Any:
    if client is not None:
        return client
    import boto3

    return boto3.client("ses")


def _s3_client(client: Any | None = None) -> Any:
    if client is not None:
        return client
    import boto3

    return boto3.client("s3")


def _forward_to(ssm: Any | None = None) -> str:
    direct = os.environ.get("ONCA_CONTACT_FORWARD_TO")
    if direct:
        return direct
    name = os.environ.get("ONCA_CONTACT_FORWARD_PARAM")
    if not name:
        return ""
    if ssm is None:
        import boto3

        ssm = boto3.client("ssm")
    return (ssm.get_parameter(Name=name)["Parameter"]["Value"] or "").strip()


def build_forward(raw_mime: bytes, forward_to: str) -> dict[str, str]:
    """Parse the stored inbound message and build the forwarded message's parts.

    Kept separate from the AWS calls so it is trivially testable with no mock server:
    feed it bytes, get back what would be sent.
    """
    msg = email.message_from_bytes(raw_mime, policy=policy.default)
    original_from = str(msg.get("From") or "unknown sender")
    original_subject = str(msg.get("Subject") or "(sem assunto)")
    original_date = str(msg.get("Date") or "")

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                body = part.get_content()
                break
    else:
        body = msg.get_content() if msg.get_content_type() == "text/plain" else ""
    body = (body or "(mensagem sem corpo em texto simples)").strip()

    subject = f"[Onça contato] {original_subject}"
    text = (
        f"Nova mensagem recebida em {FROM_ADDRESS}\n"
        f"De: {original_from}\n"
        f"Data: {original_date}\n"
        f"Assunto original: {original_subject}\n"
        f"{'-' * 40}\n\n"
        f"{body}"
    )
    return {
        "subject": subject,
        "text": text,
        "reply_to": original_from,
        "to": forward_to,
    }


def forward_object(bucket: str, key: str, *, s3: Any | None = None,
                   ses: Any | None = None, ssm: Any | None = None) -> dict[str, Any]:
    """Fetch the stored inbound message and forward it. Returns a small report."""
    forward_to = _forward_to(ssm)
    if not forward_to:
        # Fail closed: forwarding nowhere silently drops a real prospect's message
        # with no trace. Refuse instead, so a misconfiguration is loud, not lossy.
        raise RuntimeError("contact forward address is not configured "
                           "(ONCA_CONTACT_FORWARD_PARAM / ONCA_CONTACT_FORWARD_TO)")

    raw = _s3_client(s3).get_object(Bucket=bucket, Key=key)["Body"].read()
    parts = build_forward(raw, forward_to)

    _client(ses).send_email(
        Source=FROM_ADDRESS,
        Destination={"ToAddresses": [forward_to]},
        Message={
            "Subject": {"Data": parts["subject"], "Charset": "UTF-8"},
            "Body": {"Text": {"Data": parts["text"], "Charset": "UTF-8"}},
        },
        ReplyToAddresses=[parts["reply_to"]] if "@" in parts["reply_to"] else [],
    )
    return {"status": "forwarded", "to": forward_to, "subject": parts["subject"]}


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    results = []
    for record in event.get("Records") or []:
        s3_info = (record.get("s3") or {})
        bucket = (s3_info.get("bucket") or {}).get("name")
        key = (s3_info.get("object") or {}).get("key")
        if not bucket or not key:
            continue
        # URL-decode the key: S3 event notifications percent-encode it (e.g. spaces
        # as '+'), and SES's own object keys never contain characters that need it,
        # so skipping this would only ever break on values S3 itself introduced.
        import urllib.parse

        key = urllib.parse.unquote_plus(key)
        try:
            results.append(forward_object(bucket, key))
        except Exception as exc:  # pragma: no cover - defensive, one bad message
            # must not block any others in the same batch.
            print(f"Warning: could not forward s3://{bucket}/{key}: {exc}")
            results.append({"status": "error", "key": key, "error": str(exc)})
    return {"forwarded": results}
