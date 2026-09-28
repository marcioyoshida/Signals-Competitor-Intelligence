#!/usr/bin/env python3
"""Write Onça's discovery files (#180/#183/#186) from src/dashboard/discovery.py:
the site's /llms.txt, /ai.txt, /openapi.json, /.well-known/agent-card.json, and the MCP
Registry's server.json at the repo root. Re-run after changing discovery.py; the drift test
(tests/test_discovery.py) fails until you do."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.dashboard import discovery  # noqa: E402

SITE = ROOT / "src" / "dashboard" / "site"

for rel, content in discovery.files().items():
    out = SITE / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    print("wrote", out.relative_to(ROOT))
(ROOT / "server.json").write_text(json.dumps(discovery.render_server_json(), ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
print("wrote server.json")
