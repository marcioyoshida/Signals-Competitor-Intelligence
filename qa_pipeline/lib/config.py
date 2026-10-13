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
OPERATOR_SECRET_PARAM = "/onca/dashboard/operator-secret"
SITE_URL = os.environ.get("ONCA_QA_SITE_URL", "https://onssa.org")

# ADR 016 / ed24dcf (2026-10-01): an Entry-tier tenant's /exec is the Entry board — officer
# tabs, Ask and the sector selector are hidden ("Entry has no officer suite"). The `entry` QA
# persona (tier entry, agri-funds) therefore cannot drive an officer-suite journey; the
# sovereign `admin` persona (banking + fintech) can. This is why the smoke/routing/resilience
# gates failed every night from 2026-10-02: they were clicking controls Entry never shows.
OFFICER_SUITE_PERSONA = "admin"
ENTRY_TIER_PERSONAS = frozenset({"entry"})


def officer_suite_persona(event: dict) -> str:
    """The persona for a journey through /exec's officer suite. The state machine
    (infra/qa_pipeline.py) still sends persona="entry" for smoke/routing/resilience; an
    Entry-tier persona is swapped for the sovereign one instead of failing on hidden controls.
    Any other explicit persona is honored."""
    persona = event.get("persona") or OFFICER_SUITE_PERSONA
    return OFFICER_SUITE_PERSONA if persona in ENTRY_TIER_PERSONAS else persona


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


@lru_cache
def operator_secret() -> str:
    """The `?opkey=` value #122 requires on top of the shared basic-auth password to read
    the raw, unscoped `/feed.json` (`?admin=1&opkey=...`) — never the tenant credential."""
    import boto3

    ssm = boto3.client("ssm")
    return ssm.get_parameter(Name=OPERATOR_SECRET_PARAM, WithDecryption=True)["Parameter"]["Value"]
