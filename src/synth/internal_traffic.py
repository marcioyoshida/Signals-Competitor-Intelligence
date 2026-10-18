"""Our own traffic is not client evidence (#165, engagement fix 2026-10-18).

One test, shared by every usage rollup (mobile usage, the ETS Engagement component, the "interesse
executivo" attention lists), so they can't disagree about what counts as a client:

- the QA personas' tenants (`qa-internal-*`): the OncaQaPipeline and nav tests sign in on every run;
- the operator (`operator` tenant, and the opkey operator mode, which writes `actor="operator"`
  with no tenant at all: 2,273 automated "Hoje" opens in W41 alone);
- the owner's own all-sectors tenant: the owner using the product is not demand.

Live 2026-10-18: 7,974 engagement events since 09-06, 0 from a client — the Engagement score sat at
10/10 on automation alone.
"""
from __future__ import annotations

from typing import Any

INTERNAL_TENANT_PREFIXES = ("qa-internal-",)
INTERNAL_TENANTS = {"operator", "owner-all-sectors"}
INTERNAL_ACTORS = {"operator"}


def is_internal(e: dict[str, Any]) -> bool:
    t = str(e.get("tenant") or "")
    return (t in INTERNAL_TENANTS or t.startswith(INTERNAL_TENANT_PREFIXES)
            or str(e.get("actor") or "") in INTERNAL_ACTORS)
