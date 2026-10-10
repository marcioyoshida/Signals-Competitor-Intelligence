"""Portuguese count phrases for pipeline-generated copy (#225).

`"(s)"` forms ("1 novo(s) entrante(s)", "8 sanção(ões) vigente(s)") read as unfinished text to a
senior reader. Pass the whole singular and plural phrase so adjectives agree with the noun:
`pt_count(1, "novo entrante", "novos entrantes")` -> "1 novo entrante".
"""
from __future__ import annotations


def pt_count(n: int | float | None, one: str, many: str) -> str:
    n = int(n or 0)
    return f"{n} {one if n == 1 else many}"
