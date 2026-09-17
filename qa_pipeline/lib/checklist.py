"""A run-all-checks-then-report accumulator, shared by the #129/#130 check modules.

Deliberately does NOT raise on the first failed assertion — a Playwright session that
bails out after the first mismatch throws away every OTHER signal that same page load
could have given (exactly the failure mode a real navigation-regression run should avoid:
one broken selector shouldn't hide three other real breaks). Callers run every check they
can, then call `raise_if_failed()` once at the end.
"""
from __future__ import annotations

from typing import Any


class Checklist:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.items.append({"name": name, "ok": bool(ok), "detail": str(detail)})
        return bool(ok)

    @property
    def all_ok(self) -> bool:
        return all(i["ok"] for i in self.items)

    def summary(self) -> str:
        failed = [i for i in self.items if not i["ok"]]
        base = f"{len(self.items) - len(failed)}/{len(self.items)} checks passed"
        if not failed:
            return base
        return base + "; FAILED: " + "; ".join(f"{i['name']} ({i['detail']})" for i in failed)

    def raise_if_failed(self) -> None:
        if not self.all_ok:
            raise AssertionError(self.summary())
