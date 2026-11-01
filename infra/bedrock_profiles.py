"""Tagged Bedrock application inference profiles (#291), shared by the app and QA stacks.

Bedrock spend on a bare foundation-model id carries no cost-allocation tag, so 2026-10's Onça
Bedrock bill showed up untagged in Cost Explorer. A call through a profile tagged
``project=onca`` is attributed. The Lambdas pick the profile up from ``MODEL_PROFILES``
(``src/synth/bedrock_llm.invoke_target``).
"""
from __future__ import annotations

import re

from aws_cdk import CfnTag, Stack
from aws_cdk import aws_bedrock as bedrock


def model_profiles(scope, surface: str, model_ids) -> dict[str, str]:
    """One application inference profile per model, tagged ``project=onca`` and
    ``surface=<surface>``, COPIED FROM the in-region foundation model (never a cross-region
    profile). Returns model id -> profile ARN for ``MODEL_PROFILES`` and the IAM grants.
    Same shape as Signals-Legal-Intelligence ``infra/app_stack.py``."""
    stack = Stack.of(scope)
    out: dict[str, str] = {}
    for mid in dict.fromkeys(model_ids):
        slug = re.sub(r"[^a-z0-9]+", "-", mid.split(".", 1)[-1].lower()).strip("-")
        p = bedrock.CfnApplicationInferenceProfile(
            scope, "ModelProfile" + "".join(w.capitalize() for w in slug.split("-")),
            inference_profile_name=f"onca-{surface}-{slug}",
            # The schema allows letters, digits, ":" "." and single " _-" separators only.
            description=f"Onca {surface} cost attribution for {mid}",
            model_source=bedrock.CfnApplicationInferenceProfile.InferenceProfileModelSourceProperty(
                copy_from=f"arn:{stack.partition}:bedrock:{stack.region}::foundation-model/{mid}"),
            tags=[CfnTag(key="project", value="onca"), CfnTag(key="surface", value=surface)])
        out[mid] = p.attr_inference_profile_arn
    return out
