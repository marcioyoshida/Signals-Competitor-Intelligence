"""Settings for the Onça MCP authorization server. One place for every constant, so a reviewer
can check them against the (Tarantula) threat model in one read."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

READ = "onca:read"                    # /mcp, /a2a: the caller's licensed, read-only data
WRITE = "onca:write"                  # /mcp/ops: the /api/act catalog — operators only (#182)
# resource path → the one scope a token for it may carry (audience binding, RFC 8707)
RESOURCES: dict[str, str] = {"/mcp": READ, "/mcp/ops": WRITE, "/a2a": READ}
ACCESS_TTL_S = 3600                   # 1 h access tokens (T7)
CODE_TTL_S = 60                       # single-use, 60 s codes (T1)
REQUEST_TTL_S = 600                   # pending authorization lifetime (T17)
REFRESH_ABSOLUTE_S = 30 * 86400       # 30 d absolute (same as the /exec session cap, #164)
REFRESH_IDLE_S = 7 * 86400            # 7 d idle
CIMD_CACHE_MAX_S = 86400              # T8: cache fetched client metadata ≤ 24 h
LEEWAY_S = 30                         # T20: clock skew tolerated on exp/iat


@dataclass(frozen=True)
class Settings:
    issuer: str = "https://onssa.org/oauth"
    base: str = "https://onssa.org"
    # D6: hosts that get a "verified" badge — EMPTY until the owner names them.
    verified_hosts: frozenset[str] = field(default_factory=frozenset)

    @property
    def callback(self) -> str:
        return self.issuer + "/callback"

    def resource_uri(self, path: str) -> str:
        return self.base + path

    def prm_url(self, path: str) -> str:
        """Protected Resource Metadata URL (RFC 9728, path form)."""
        return self.base + "/.well-known/oauth-protected-resource" + path

    def resource_path(self, resource: str | None) -> str | None:
        """The resource path for a client-supplied resource URI, or None if it's not ours."""
        r = (resource or "").rstrip("/")
        for path in RESOURCES:
            if r == self.base + path:
                return path
        return None


def settings() -> Settings:
    issuer = os.environ.get("ONCA_OAUTH_ISSUER", "https://onssa.org/oauth").rstrip("/")
    base = issuer.rsplit("/oauth", 1)[0]
    hosts = frozenset(h.strip().lower() for h in
                      os.environ.get("ONCA_OAUTH_VERIFIED_HOSTS", "").split(",") if h.strip())
    return Settings(issuer=issuer, base=base, verified_hosts=hosts)
