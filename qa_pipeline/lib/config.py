"""Runtime config for the ADR 027 QA pipeline — credentials pulled live from AWS on every
run, never checked into the repo (#126). Mirrors `infra/app.py`'s `dashboard_credentials()`
SSM-lookup pattern, but for the test-runner side rather than CDK synth time.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

QA_SECRET_NAME = "signalscompetitor/onca/qa-test-credentials"
BASIC_AUTH_USER_PARAM = "/onca/dashboard/basic-auth-user"
BASIC_AUTH_PASS_PARAM = "/onca/dashboard/basic-auth-pass"
SITE_URL = os.environ.get("ONCA_QA_SITE_URL", "https://onssa.org")


@lru_cache
def qa_credentials() -> dict[str, Any]:
    """{'user_pool_id', 'client_id', 'hosted_ui_domain', 'personas': {'entry': {...}, 'admin': {...}}}"""
    import boto3

    client = boto3.client("secretsmanager")
    return json.loads(client.get_secret_value(SecretId=QA_SECRET_NAME)["SecretString"])


@lru_cache
def basic_auth() -> tuple[str, str]:
    import boto3

    ssm = boto3.client("ssm")
    user = ssm.get_parameter(Name=BASIC_AUTH_USER_PARAM)["Parameter"]["Value"]
    pw = ssm.get_parameter(
        Name=BASIC_AUTH_PASS_PARAM, WithDecryption=True
    )["Parameter"]["Value"]
    return user, pw
