"""ADR 004 belief maintenance — drift & staleness re-review (open question #4).

Phase C promotes analyst-approved claims into durable curated bullets that survive
every belief rebuild — pinned at a high, stable confidence with NO decay. That is
correct for a *fresh* human decision, but dangerous over time: a Strength vetted six
months ago is asserted forever even if the world moved on. ADR 004's open question #4
(drift & staleness) asks for exactly this guard, and this producer supplies it, reusing
the propose→vet pattern rather than auto-deciding:

    a curated bullet that has gone `ONCA_MAINT_STALE_DAYS` without any affirmation —
    no human re-affirmation, no news corroboration reaching it — emits a **`stale`
    re-review proposal**. The analyst then decides in the same war-room panel:
      - **approve** ("aposentar") -> the bullet is retired (a curated retirement),
      - **reject**  ("manter")   -> the bullet is re-affirmed, resetting its clock.

Only **curated** bullets need this: the derived axis bullets self-maintain (they decay
and vanish when their axis stops firing — silence even narrates the absence). Curated
bullets are the only ones pinned regardless of fresh evidence, so they are the only
drift surface.

The store is idempotent by *affirmation epoch*: a bullet's stale proposal id encodes
the date it was last affirmed, so within one staleness cycle the same proposal recurs
(no duplicates), and after a re-affirmation the next cycle earns a fresh id. The core
`find_stale()` is pure (no I/O) for deterministic tests.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

import boto3

from src.synth import feature_store, swot_reconcile, swot_store
from src.synth.synthesize import ENTITY_LABELS, run_at_now, run_date_today

MAINTENANCE_PROPOSALS_KEY = "swot/maintenance_proposals.json"

ENABLED = os.environ.get("ONCA_MAINT_ENABLED", "1") not in ("0", "false", "False")
STALE_DAYS = int(os.environ.get("ONCA_MAINT_STALE_DAYS", "90"))  # a quarter — belief half-life


def _entity_label(entity: str) -> str:
    return ENTITY_LABELS.get(entity) or str(entity).replace("_", " ").title()


def _days_between(a: str, b: str) -> int | None:
    try:
        return (feature_store._parse(a) - feature_store._parse(b)).days
    except Exception:
        return None


def last_affirmed(bullet: dict[str, Any], reinf_dates: list[str]) -> str:
    """Most recent date this curated bullet was affirmed — by a human decision
    (approved/re-affirmed) or by a news corroboration reaching it. '' if unknown."""
    dates = [
        str(bullet.get("reaffirmed_at") or "")[:10],
        str(bullet.get("approved_at") or "")[:10],
        str(bullet.get("date") or "")[:10],
        *[str(d or "")[:10] for d in reinf_dates],
    ]
    return max([d for d in dates if d], default="")


def find_stale(
    curated: dict[str, Any],
    reinforcements: list[dict[str, Any]],
    *,
    as_of: str,
    stale_days: int = STALE_DAYS,
) -> list[dict[str, Any]]:
    """Pure: curated store + reinforcements -> `stale` re-review proposals. No I/O.

    A curated, still-active (not already retired) bullet whose last affirmation is
    >= stale_days old earns one proposal, idempotent by (bullet, affirmation epoch).
    """
    retired = {r.get("target_bullet_id") for r in curated.get("retirements", [])
               if r.get("target_bullet_id")}
    reinf_by_bullet: dict[str, list[str]] = {}
    for r in reinforcements or []:
        bid = r.get("bullet_id")
        if bid:
            reinf_by_bullet.setdefault(bid, []).append(str(r.get("date") or "")[:10])

    out: list[dict[str, Any]] = []
    for b in curated.get("bullets", []):
        bid = b.get("id")
        if not bid or bid in retired:
            continue
        affirmed = last_affirmed(b, reinf_by_bullet.get(bid, []))
        days = _days_between(as_of, affirmed) if affirmed else None
        if days is None or days < stale_days:
            continue
        ent = b.get("entity")
        label = b.get("label") or _entity_label(ent)
        out.append({
            "id": f"stale:{bid}:{affirmed}",   # one per staleness cycle
            "kind": "stale",
            "entity": ent,
            "label": label,
            "dimension": b.get("dimension"),
            "target_bullet_id": bid,
            "target_text": b.get("text"),
            "text": f"Crença sem corroboração há {days} dias — ainda é válida?",
            "days_stale": days,
            "date": as_of,
            "status": "pending",
            "created": as_of,
        })
    return out


def publish(
    proposals: list[dict[str, Any]], bucket: str, *, s3: Any, as_of: str
) -> dict[str, int]:
    """Merge into the durable maintenance store (idempotent, human status preserved)."""
    try:
        body = s3.get_object(Bucket=bucket, Key=MAINTENANCE_PROPOSALS_KEY)["Body"].read()
        existing = json.loads(body.decode("utf-8")).get("proposals", [])
    except Exception:  # pragma: no cover - absent until first stale bullet
        existing = []
    merged = swot_reconcile.merge_proposals(existing, proposals)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    s3.put_object(
        Bucket=bucket, Key=MAINTENANCE_PROPOSALS_KEY,
        Body=json.dumps({"generated_at": now, "as_of": as_of,
                         "proposals": merged}, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json", CacheControl="no-cache",
    )
    return {
        "proposals": len(merged),
        "proposals_new": len(merged) - len(existing),
        "proposals_pending": sum(1 for p in merged if p.get("status") == "pending"),
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Emit stale re-review proposals for curated bullets that have gone quiet."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}
    if not ENABLED:
        return {"statusCode": 200, "body": json.dumps({"status": "disabled"})}

    s3 = boto3.client("s3")
    run_date = run_date_today()
    stale_days = int(os.environ.get("ONCA_MAINT_STALE_DAYS", str(STALE_DAYS)))

    curated = swot_store._load_curated(digests_bucket, s3=s3)
    reinforcements = swot_store._load_reinforcements(digests_bucket, s3=s3)
    proposals = find_stale(curated, reinforcements, as_of=run_date, stale_days=stale_days)

    counts = {"proposals": 0, "proposals_new": 0, "proposals_pending": 0}
    try:
        counts = publish(proposals, digests_bucket, s3=s3, as_of=run_date)
    except Exception as exc:  # pragma: no cover - publish best-effort
        print(f"Warning: maintenance publish failed: {exc}")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "status": "ok",
            "as_of": run_date,
            "stale_days": stale_days,
            "curated_bullets": len(curated.get("bullets", [])),
            "stale_found": len(proposals),
            "run_at": run_at_now(),
            **counts,
        }),
    }
