"""Owner, 2026-10-25: hosted model calls run on Nova Lite (no paying customers yet). Nova Pro may
not come back as a default anywhere: not in code, not in a Lambda's environment in infra."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRO = re.compile(r"nova-pro", re.I)


def test_framework_default_is_lite(monkeypatch):
    import importlib
    monkeypatch.delenv("ONCA_FRAMEWORK_MODEL_ID", raising=False)
    from src.synth import bedrock_llm
    importlib.reload(bedrock_llm)
    assert bedrock_llm.FRAMEWORK_MODEL == "amazon.nova-lite-v1:0"
    assert bedrock_llm.DEFAULT_SYNTH_MODEL == "amazon.nova-lite-v1:0"


def test_qa_vision_default_is_lite(monkeypatch):
    import importlib
    monkeypatch.delenv("ONCA_QA_VISION_MODEL_ID", raising=False)
    from qa_pipeline.lib import vision
    importlib.reload(vision)
    assert vision.VISION_MODEL == "amazon.nova-lite-v1:0"


def test_no_nova_pro_in_code_or_infra():
    files = subprocess.run(["git", "ls-files", "src", "infra", "config", "scripts", "qa_pipeline"], cwd=ROOT,
                           capture_output=True, text=True, check=True).stdout.split()
    hits = []
    for f in files:
        if not f.endswith((".py", ".json", ".yaml", ".yml", ".js")) or "/site/" in f:
            continue
        for i, line in enumerate((ROOT / f).read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if PRO.search(line):
                hits.append(f"{f}:{i}")
    assert not hits, f"Nova Pro named as a model: {hits}"
