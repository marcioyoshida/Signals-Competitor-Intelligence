"""#291: Bedrock calls go through Onça's tagged application inference profiles."""
import json
import re
from pathlib import Path

from src.synth import bedrock_llm

ARN = "arn:aws:bedrock:us-east-1:668449743071:application-inference-profile/abc123def456"


def _env(monkeypatch, profiles, region="us-east-1"):
    monkeypatch.setenv("MODEL_PROFILES", json.dumps(profiles))
    monkeypatch.setenv("AWS_REGION", region)


def test_invoke_target_uses_the_profile_when_deployed(monkeypatch):
    _env(monkeypatch, {"amazon.nova-lite-v1:0": ARN})
    assert bedrock_llm.invoke_target("amazon.nova-lite-v1:0") == ARN
    assert bedrock_llm.invoke_target("amazon.nova-micro-v1:0") == "amazon.nova-micro-v1:0"


def test_invoke_target_falls_back_to_the_model_id(monkeypatch):
    monkeypatch.delenv("MODEL_PROFILES", raising=False)
    assert bedrock_llm.invoke_target("amazon.nova-lite-v1:0") == "amazon.nova-lite-v1:0"
    monkeypatch.setenv("MODEL_PROFILES", "not json")
    assert bedrock_llm.invoke_target("amazon.nova-lite-v1:0") == "amazon.nova-lite-v1:0"


def test_invoke_target_ignores_another_regions_profile(monkeypatch):
    _env(monkeypatch, {"amazon.nova-lite-v1:0": ARN.replace("us-east-1", "us-west-2")})
    assert bedrock_llm.invoke_target("amazon.nova-lite-v1:0") == "amazon.nova-lite-v1:0"


def test_converse_sends_the_profile_arn(monkeypatch):
    _env(monkeypatch, {"amazon.nova-lite-v1:0": ARN})
    sent = {}

    class _Client:
        def converse(self, **kw):
            sent.update(kw)
            return {"output": {"message": {"content": [{"text": "ok"}]}}}

    monkeypatch.setattr(bedrock_llm.boto3, "client", lambda *a, **k: _Client())
    assert bedrock_llm.converse("hi", model_id="amazon.nova-lite-v1:0") == "ok"
    assert sent["modelId"] == ARN


def test_every_bedrock_lambda_gets_the_profiles():
    """A Lambda granted bedrock:InvokeModel but missing from the MODEL_PROFILES loop would
    keep invoking the bare model id, untagged."""
    src = (Path(__file__).resolve().parents[1] / "infra" / "app.py").read_text()
    lines = src.splitlines()
    granted = set()
    for i, ln in enumerate(lines):
        if '"bedrock:InvokeModel"' in ln:
            for j in range(i, max(i - 8, 0), -1):
                m = re.search(r"(\w+)\.add_to_role_policy", lines[j])
                if m:
                    granted.add(m.group(1))
                    break
    loop = re.search(r"for fn in \(([^)]*)\):\s*\n\s*fn\.add_environment\(\"MODEL_PROFILES\"", src)
    assert loop, "MODEL_PROFILES wiring loop not found"
    wired = {v.strip() for v in loop.group(1).split(",") if v.strip()}
    granted.discard("fn")                        # the wiring loop's own grant
    assert granted - wired == set(), granted - wired
