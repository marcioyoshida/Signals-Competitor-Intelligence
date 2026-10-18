"""pt-BR number formatting for server-built display strings (#263).

/exec formats its own numbers in pt-BR (`nf()` in v3/index.html, d3186b9) but it cannot safely
rewrite text the server already wrote, so "ROE 19.4%" reached the page with an English decimal.
Display strings are formatted HERE, at the source: decimal comma, thousands dot.

Only display strings use this module. Machine-readable fields stay numeric.

- ``int`` values keep no decimals ("42", "1.234"), whatever ``nd`` is.
- ``float`` values get exactly ``nd`` decimals ("19,4", "1.234,5").
- ``None``, NaN, infinities and non-numbers render as "—".
- Negative numbers use the ASCII hyphen-minus, the same as the client's
  ``toLocaleString("pt-BR")`` ("-3,2"). Negative zero after rounding has no sign.
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

DASH = "—"


def _value(x: Any) -> int | float | None:
    if isinstance(x, Decimal):  # DynamoDB numbers
        if not x.is_finite():
            return None
        x = int(x) if x.as_tuple().exponent >= 0 else float(x)
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


def num(x: Any, nd: int = 1) -> str:
    """'19,4' · '1.234,5' · '1.234' (int) · '—' (None/NaN)."""
    v = _value(x)
    if v is None:
        return DASH
    if isinstance(v, int):
        s = f"{abs(v):,d}"
        neg = v < 0
    else:
        s = f"{abs(v):,.{nd}f}"
        neg = v < 0 and float(s.replace(",", "")) != 0.0
    s = s.replace(",", "\0").replace(".", ",").replace("\0", ".")
    return ("-" if neg else "") + s


def compact(x: Any, nd: int = 1) -> str:
    """Like ``num`` but a whole float drops its decimals: 3.0 -> '3', 3.5 -> '3,5' (the old ``:g``)."""
    v = _value(x)
    if isinstance(v, float) and float(round(v, nd)).is_integer():
        v = int(round(v, nd))
    return num(v, nd)


def pct(x: Any, nd: int = 1) -> str:
    """'19,4%' · '—' (no '%' on a missing value)."""
    s = num(x, nd)
    return s if s == DASH else s + "%"


def signed(x: Any, nd: int = 1) -> str:
    """'+25,0' · '-3,2' · '0,0' (zero after rounding carries no sign)."""
    s = num(x, nd)
    if s == DASH or s.startswith("-") or not any(ch in "123456789" for ch in s):
        return s
    return "+" + s


def signed_pct(x: Any, nd: int = 1) -> str:
    """'+6,9%' · '-1,2%'."""
    s = signed(x, nd)
    return s if s == DASH else s + "%"


def pp(x: Any, nd: int = 1) -> str:
    """Percentage points: '2,3 p.p.'."""
    s = num(x, nd)
    return s if s == DASH else s + " p.p."
