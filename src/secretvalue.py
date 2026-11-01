"""Read a secret value by name, from SSM Parameter Store or Secrets Manager.

A name starting with ``/`` is an SSM **SecureString** parameter (decrypted with the
AWS-managed ``aws/ssm`` key); anything else is a Secrets Manager secret id. The static
values (``/onca/api-key``, ``/onca/directory-reviewer``, ``/onca/qa-test-credentials``)
moved to SSM on 2026-11-01 (#292): it is free, and nothing rotates them. The Secrets
Manager branch stays for what can't move (the Cognito IdP client secrets, which
CloudFormation resolves inside ``ProviderDetails``).

Raises on failure; callers already treat a load failure as "not configured". The value is
never printed or logged.
"""
from __future__ import annotations

from typing import Any


def read(name: str, *, client: Any = None) -> str:
    import boto3
    if name.startswith("/"):
        ssm = client or boto3.client("ssm")
        return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    sm = client or boto3.client("secretsmanager")
    return sm.get_secret_value(SecretId=name)["SecretString"]
