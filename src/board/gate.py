"""Spend gate and per-run cost cap (#329).

A board run is 15–25 large-context model calls. It runs only when someone is paying for it:

- the tenant has connected its **own model** (BYOK, #345): the calls bill to the tenant;
- the tenant holds an **active paid subscription** (storefront billing, #187);
- an operator set the tenant's ``board_owner_ok`` flag (owner-approved runs: evals, pilots).

A trial, a lapsed plan or an unprovisioned tenant is refused before any model call.

The cap is in tokens (input + output, across every call of the run). The runner checks it
between phases (``Meter.stop``); a run that crosses it ends there with partial results and a
visible gap. A tenant row may lower or raise it with ``board_token_cap``.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

DEFAULT_TOKEN_CAP = 400_000
MAX_TOKEN_CAP = 2_000_000

ALLOW_CLIENT_MODEL, ALLOW_PAYING, ALLOW_OWNER = "client_model", "paying", "owner_flag"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str            # client_model | paying | owner_flag | a refusal code
    client_billed: bool = False
    token_cap: int = DEFAULT_TOKEN_CAP


def _cap(row: dict[str, Any] | None) -> int:
    try:
        v = int((row or {}).get("board_token_cap") or DEFAULT_TOKEN_CAP)
    except (TypeError, ValueError):
        v = DEFAULT_TOKEN_CAP
    return max(1, min(v, MAX_TOKEN_CAP))


def decide(tenant: str | None, *, config: Callable[[str], dict[str, Any] | None],
           raw_row: Callable[[str], dict[str, Any] | None],
           client_model: Callable[[str], Any]) -> Decision:
    """Pure decision over injected lookups (tenant_config, its raw row, model_keys.connection).
    A lookup that raises is a refusal: fail closed."""
    if not tenant:
        return Decision(False, "no_tenant")
    try:
        cfg = config(tenant)
        row = raw_row(tenant) or {}
    except Exception:
        return Decision(False, "tenant_lookup_failed")
    if not cfg:
        return Decision(False, "tenant_not_provisioned")
    cap = _cap(row)
    try:
        conn = client_model(tenant)
    except Exception:
        conn = None  # a broken model-key lookup never grants anything; other routes still may
    if conn is not None:
        return Decision(True, ALLOW_CLIENT_MODEL, client_billed=True, token_cap=cap)
    if ((cfg.get("billing") or {}).get("state")) == "active":
        return Decision(True, ALLOW_PAYING, token_cap=cap)
    if row.get("board_owner_ok") is True:
        return Decision(True, ALLOW_OWNER, token_cap=cap)
    state = (cfg.get("billing") or {}).get("state") or "unbilled"
    return Decision(False, f"not_paying:{state}")


@dataclass
class Meter:
    """Thread-safe token meter shared by every call of one run."""
    cap: int
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0
    cache_read: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, usage: dict[str, Any] | None) -> None:
        u = usage or {}
        with self._lock:
            self.calls += 1
            self.tokens_in += int(u.get("input_tokens") or 0)
            self.tokens_out += int(u.get("output_tokens") or 0)
            self.cache_read += int(u.get("cache_read") or 0)

    @property
    def total(self) -> int:
        return self.tokens_in + self.tokens_out

    def stop(self) -> bool:
        return self.total >= self.cap

    def summary(self) -> dict[str, int]:
        return {"calls": self.calls, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "cache_read": self.cache_read, "token_cap": self.cap}
