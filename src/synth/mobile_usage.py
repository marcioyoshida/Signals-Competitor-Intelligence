"""#165 — how much Onça is used on phones, and for which jobs. The evidence input for the
Mobile M2 (store app) gate, whose threshold lives in epic #172: at least 30% of officer
sessions on phones for 4 consecutive weeks (plus one customer asking for an app).

Built from what the officer suite already records — no new personal data:

* engagement events (``OncaEngagementLog``): ``kind="session"`` on each /exec open, ``"install"``
  when the PWA is installed, ``"ask"``/``"share"``/``"hoje"``… for jobs; each carries only a
  device CLASS (phone/tablet/desktop, from viewport width + pointer type) and ``standalone``.
* decisions (``OncaDecisionLog``) carry the same ``device`` class (#168).

Operator-only: ``feed["mobile_usage"]`` is blanked in every tenant/entry/sample derivation.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from typing import Any

GATE_PHONE_SHARE = 0.30
GATE_WEEKS = 4
DEVICES = ("phone", "tablet", "desktop")
# jobs worth counting on a phone (engagement kinds) — plus decisions from the decision log
_JOB_KINDS = {"session": "abrir painel", "hoje": "abrir Hoje", "ask": "perguntar",
              "share": "compartilhar", "headline": "ler manchete", "install": "instalar"}


# Our own sessions are not demand evidence: the OncaQaPipeline phone gate (#163) signs in as the
# QA personas at 390/412px on every run, and the operator is us. Live 2026-09-29: 134 of 135
# sessions since 09-21 were qa-internal-* — the gate read 43% phone in W39 from automation alone.
INTERNAL_TENANT_PREFIXES = ("qa-internal-",)
INTERNAL_TENANTS = {"operator"}


def is_internal(e: dict[str, Any]) -> bool:
    t = str(e.get("tenant") or "")
    return t in INTERNAL_TENANTS or t.startswith(INTERNAL_TENANT_PREFIXES)


def _week(ts: str) -> str | None:
    try:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def build(events: list[dict[str, Any]], decisions: list[dict[str, Any]] | None = None, *,
          weeks: int = 8) -> dict[str, Any]:
    """Weekly sessions by device class (overall and per officer), installs, installed-app
    sessions, the top jobs done on a phone, and the M2 gate reading."""
    events = [e for e in (events or []) if isinstance(e, dict)]
    internal = sum(1 for e in events if is_internal(e) and e.get("kind") == "session")
    events = [e for e in events if not is_internal(e)]
    decisions = [d for d in (decisions or []) if isinstance(d, dict) and not is_internal(d)]
    sess: dict[str, Counter] = defaultdict(Counter)
    sess_off: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    standalone: Counter = Counter()
    installs: Counter = Counter()
    phone_jobs: Counter = Counter()
    for e in events:
        wk = _week(e.get("created_at"))
        if not wk:
            continue
        dev = e.get("device") if e.get("device") in DEVICES else "unknown"
        kind = str(e.get("kind") or "")
        if kind == "session":
            sess[wk][dev] += 1
            sess_off[wk][str(e.get("officer") or "—")][dev] += 1
            if e.get("standalone"):
                standalone[wk] += 1
        elif kind == "install":
            installs[wk] += 1
        if dev == "phone" and kind in _JOB_KINDS:
            phone_jobs[_JOB_KINDS[kind]] += 1
    for d in decisions or []:
        if isinstance(d, dict) and d.get("device") == "phone":
            phone_jobs["aprovar/rejeitar"] += 1

    rows = []
    for wk in sorted(sess, reverse=True)[:weeks]:
        c = sess[wk]
        total = sum(c.values())
        known = sum(c[d] for d in DEVICES)
        rows.append({
            "week": wk, "sessions": total,
            "by_device": {d: c[d] for d in (*DEVICES, "unknown") if c[d]},
            "phone_share": round(c["phone"] / known, 3) if known else None,
            "standalone": standalone[wk], "installs": installs[wk],
            "by_officer": {o: dict(v) for o, v in sorted(sess_off[wk].items())},
        })

    streak, prev = 0, None  # CONSECUTIVE most-recent ISO weeks at/above the gate share
    for r in rows:
        monday = dt.date.fromisocalendar(int(r["week"][:4]), int(r["week"][6:]), 1)
        if prev is not None and prev - monday != dt.timedelta(days=7):
            break                       # a week with no sessions breaks the run
        if r["phone_share"] is None or r["phone_share"] < GATE_PHONE_SHARE:
            break
        streak, prev = streak + 1, monday
    return {
        "weeks": rows,
        "installs_total": sum(installs.values()),
        "internal_sessions_excluded": internal,
        "phone_jobs": [{"job": k, "count": v} for k, v in phone_jobs.most_common(8)],
        "gate": {"phone_share_min": GATE_PHONE_SHARE, "weeks_required": GATE_WEEKS,
                 "weeks_met": streak, "usage_condition_met": streak >= GATE_WEEKS,
                 "note": "o gate M2 também exige ≥1 cliente pedindo app (épico #172)"},
    }
