"""Connected apps (D6): what an operator lists and revokes for a user. Revoking an app deletes
its consent and revokes every refresh family it holds — and because every access
token names its family (``fid``), its access tokens stop working at /mcp/pro within
the resource server's cache window (T14), not at their 1-hour expiry."""
from __future__ import annotations

from typing import Any


def _n(v: Any) -> int | None:
    """DynamoDB returns numbers as Decimal, which json.dumps refuses (found live)."""
    return int(v) if v is not None else None


def list_apps(store: Any, sub: str) -> list[dict[str, Any]]:
    fams = [f for f in store.query("usr#" + sub, "fam#") if not f.get("revoked")]
    consents = {c["client_id"]: c for c in store.query("usr#" + sub, "consent#")}
    apps: dict[str, dict[str, Any]] = {}
    for c in consents.values():
        apps[c["client_id"]] = {"client_id": c["client_id"], "client_name": c.get("client_name"),
                                "redirect_host": c.get("redirect_host"),
                                "connected": _n(c.get("created")), "last_used": None, "sessions": 0}
    for f in fams:
        a = apps.setdefault(f["client_id"], {"client_id": f["client_id"],
                                             "client_name": f.get("client_name"),
                                             "redirect_host": None, "connected": _n(f.get("created")),
                                             "last_used": None, "sessions": 0})
        a["sessions"] += 1
        a["last_used"] = max(a["last_used"] or 0, int(f.get("last_used") or 0)) or None
    return sorted(apps.values(), key=lambda a: -(a["last_used"] or a["connected"] or 0))


def revoke_app(store: Any, sub: str, client_id: str | None = None) -> int:
    """Revoke one app (``client_id``) or ALL apps (``None``). Returns families revoked."""
    n = 0
    for f in store.query("usr#" + sub, "fam#"):
        if (client_id is None or f.get("client_id") == client_id) and not f.get("revoked"):
            store.update("usr#" + sub, f["sk"], {"revoked": True})
            n += 1
    for c in store.query("usr#" + sub, "consent#"):
        if client_id is None or c.get("client_id") == client_id:
            store.delete("usr#" + sub, c["sk"])
    return n
