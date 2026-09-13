"""Shared wall-clock-budget signal for the ingest layer.

Lives in its own module (rather than in ``lambda_port``) so individual source
modules can re-raise it without importing the handler that imports THEM.

Why it matters: the per-source budget is enforced with a SIGALRM that raises
this exception from whatever line the source happens to be on. Any broad
``except Exception`` in a best-effort loop will therefore swallow the kill
signal. Source code that has such a loop must re-raise ``SourceBudgetExceeded``
explicitly (see ``bcb_ifdata.map_to_entities``).
"""
from __future__ import annotations


class SourceBudgetExceeded(Exception):
    """A single source ran past its wall-clock budget (or the ingest deadline)."""
