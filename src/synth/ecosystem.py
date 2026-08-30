"""Opportunistic (ADR 003) — Ecosystem / dependency. SOURCE-GATED by design.

"Who's exposed if hub Z breaks." The subject is an infrastructure **hub** (a core-
banking vendor, a BaaS/acquiring rail, a fund administrator) and the mechanism is
**contagion over directed dependency edges**: a threat/incident at a hub propagates
exposure to the entities that depend on it.

The ADR is explicit that this axis is **source-gated**, not effort-gated: "dependency
edges rarely in public filings; **ingestion is the blocker**" — "the work is finding a
dependency-data source, not the synthesis." So this ships the *synthesis* (a pure
contagion function + a dependency-graph builder) and reports `source_gated` until a
dependency source is wired.

Two edge sources are supported, both honest about their strength:
- an **external dependency source** (env `ONCA_ECOSYSTEM_SOURCE` → an S3 JSON key of
  `{hub, dependents:[...], kind}` edges) — the real answer when it exists;
- a **weak proxy already in the data**: fund-administration edges (a fund's
  `admin`/`manager` institution is a hub its funds depend on). Extracted but only ever
  used to compute contagion when a hub actually has an incident.

Contagion emits an **exposure card** only when a hub with dependents has a recent
threat/incident signal — labeled inference, grounded in the hub's incident. No hub
incident (the current state) ⇒ nothing to propagate ⇒ `source_gated`.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import boto3

from src.synth import feature_store
from src.synth.synthesize import run_at_now, run_date_today

AXIS = "ecosystem"
DEPENDENCY_GRAPH_KEY = "ecosystem/dependencies.json"

# Institutional hub fields in the current data (fund plumbing) — the weak proxy.
_HUB_FIELDS = ("admin", "manager", "custodian")
INCIDENT_THREAT = 0.6  # a hub "incident" is a high-threat signal on the hub


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except (TypeError, ValueError):
        return float(default)


def build_dependency_graph(
    signals: list[dict[str, Any]], external: list[dict[str, Any]] | None = None
) -> dict[str, dict[str, Any]]:
    """Pure: signals (+ optional external edges) -> {hub: {dependents, kind}}.

    Weak proxy: a fund/security's administrator/manager institution is a hub the
    fund depends on. External edges (a real dependency source) are merged verbatim.
    """
    hubs: dict[str, dict[str, Any]] = {}

    def _add(hub: str, dep: str, kind: str) -> None:
        hub = (hub or "").strip()
        dep = (dep or "").strip()
        if not hub or not dep or hub == dep:
            return
        h = hubs.setdefault(hub, {"hub": hub, "dependents": set(), "kind": kind})
        h["dependents"].add(dep)

    for sig in signals:
        if not isinstance(sig, dict):
            continue
        dep = sig.get("fund_name") or sig.get("security") or sig.get("name")
        for field in _HUB_FIELDS:
            if sig.get(field) and dep:
                _add(str(sig[field]), str(dep), f"fund_{field}")

    for e in external or []:
        hub = e.get("hub")
        kind = e.get("kind") or "declared"
        for dep in e.get("dependents") or []:
            _add(hub, dep, kind)

    for h in hubs.values():
        h["dependents"] = sorted(h["dependents"])
        h["n_dependents"] = len(h["dependents"])
    return hubs


# Dependency-kind → reader-facing noun (issue #37: "10 dependentes (fund_admin)" reads
# as jargon). Falls back to a generic label for unmapped kinds.
_KIND_LABEL = {
    "fund_admin": "fundos sob sua administração",
    "custodian": "fundos sob sua custódia",
    "controller": "empresas que controla",
    "cloud": "clientes de infraestrutura",
}


def contagion(
    hubs: dict[str, dict[str, Any]], incidents: dict[str, Any]
) -> list[dict[str, Any]]:
    """Pure: dependency graph + hub incidents -> exposure findings.

    ``incidents`` maps a hub → its incident: either a bare severity float, or a dict
    ``{severity, citations, source_ids, event}`` (the triggering high-threat signal —
    threaded through so the card can CITE it, issue #37). A hub with dependents AND an
    incident above the severity floor yields one exposure finding.
    """
    out: list[dict[str, Any]] = []
    min_sev = _f("ONCA_ECOSYSTEM_MIN_SEVERITY", 0.5)
    for hub, inc in incidents.items():
        sev = inc.get("severity") if isinstance(inc, dict) else inc
        h = hubs.get(hub)
        if not h or not h["dependents"] or float(sev) < min_sev:
            continue
        payload = inc if isinstance(inc, dict) else {}
        out.append({
            "hub": hub, "severity": round(float(sev), 3),
            "dependents": h["dependents"], "n_dependents": h["n_dependents"],
            "kind": h["kind"],
            "citations": payload.get("citations") or [],
            "source_ids": payload.get("source_ids") or [],
            "event": payload.get("event") or "",
        })
    out.sort(key=lambda x: (x["severity"], x["n_dependents"]), reverse=True)
    return out


def build_card(finding: dict[str, Any]) -> dict[str, Any]:
    hub, n = finding["hub"], finding["n_dependents"]
    kind = finding.get("kind") or ""
    label = _KIND_LABEL.get(kind) or (f"dependentes ({kind})" if kind else "dependentes")
    # Lead with the REAL, cited hub signal — not a vague "incidente" (issue #37):
    # reference the triggering event so the reader knows what the risk is.
    event = (finding.get("event") or "").strip()
    lead = f"{hub} está sob sinal de alta ameaça"
    if event:
        snippet = re.split(r"(?<=[.!?])\s", event)[0][:160].rstrip(" .")
        if snippet:
            lead += f" — {snippet}"
    head = (f"{lead}. {n} {label} podem ser afetados por contágio "
            f"(inferência, não fato confirmado).")
    return {
        "id": f"ecosystem-{hub}".replace(" ", "_")[:120],
        "kind": "ecosystem", "axis": AXIS, "subject_type": "hub",
        "entity": None, "entities": finding["dependents"][:20],
        "hub": hub, "n_dependents": n,
        "lenses": [], "is_alert": False, "is_inference": True,
        "threat_score": round(min(0.5, 0.2 + 0.02 * n), 3),
        "threat_factors": {"severity": finding.get("severity"), "n_dependents": n},
        "threat_score_note": "estimated_v1_ecosystem",
        "narrative": head,
        # Ground the inference in the hub's triggering signal (the whole product
        # promise is cited claims — an uncited inference card is a defect).
        "citations": finding.get("citations") or [],
        "source_ids": finding.get("source_ids") or [],
        "mode": "derived",
        "run_date": run_date_today(), "run_at": run_at_now(), "as_of": run_date_today(),
        "data_as_of": {},
    }


def _load_external_source(s3: Any) -> list[dict[str, Any]]:
    """Load a real dependency-edge source if ONCA_ECOSYSTEM_SOURCE is configured."""
    key = os.environ.get("ONCA_ECOSYSTEM_SOURCE")
    bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not key or not bucket:
        return []
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        data = json.loads(body.decode("utf-8"))
        return data.get("edges", data) if isinstance(data, (dict, list)) else []
    except Exception as exc:  # pragma: no cover - best-effort
        print(f"Warning: ecosystem source load failed: {exc}")
        return []


def _hub_incidents(narratives: list[dict[str, Any]], hubs: dict[str, dict[str, Any]]) -> dict[str, float]:
    """Best-effort: a hub named in a recent high-threat narrative is 'in incident'."""
    incidents: dict[str, Any] = {}
    hub_names = {h.lower(): h for h in hubs}
    for n in narratives:
        if not isinstance(n, dict) or not feature_store.is_activity_narrative(n):
            continue
        sev = feature_store._score(n.get("threat_score"))
        if sev < INCIDENT_THREAT:
            continue
        text = (n.get("narrative") or "").lower()
        for low, hub in hub_names.items():
            if low and low in text:
                cur = incidents.get(hub)
                # Keep the highest-threat triggering signal; carry its citations so
                # the exposure card is grounded (issue #37).
                if cur is None or sev > cur["severity"]:
                    incidents[hub] = {
                        "severity": sev,
                        "citations": n.get("citations") or [],
                        "source_ids": n.get("source_ids") or [],
                        "event": (n.get("narrative") or "").strip(),
                    }
    return incidents


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Build the dependency graph; emit contagion cards only when a hub has an incident."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}

    s3 = boto3.client("s3")
    run_date = run_date_today()
    window = int(_f("ONCA_ECOSYSTEM_WINDOW_DAYS", _f("ONCA_FEATURE_WINDOW_DAYS", 90)))

    signals: list[dict[str, Any]] = []
    try:
        from src.synth import digest_io, operatives

        signals = operatives._collect_signals(digest_io.load_latest_digest_from_s3() or {})
    except Exception as exc:  # pragma: no cover
        print(f"Warning: ecosystem signal load failed: {exc}")

    external = _load_external_source(s3)
    hubs = build_dependency_graph(signals, external)

    # publish the dependency graph (the substrate — useful even before contagion fires)
    try:
        s3.put_object(Bucket=digests_bucket, Key=DEPENDENCY_GRAPH_KEY,
                      Body=json.dumps({"as_of": run_date, "hubs": list(hubs.values())},
                                      ensure_ascii=False).encode("utf-8"),
                      ContentType="application/json", CacheControl="no-cache")
    except Exception as exc:  # pragma: no cover
        print(f"Warning: ecosystem graph publish failed: {exc}")

    narratives = feature_store.load_history(digests_bucket, window, s3=s3)
    incidents = _hub_incidents(narratives, hubs)
    findings = contagion(hubs, incidents)

    keys = []
    for fnd in findings:
        card = build_card(fnd)
        key = f"{feature_store.NARRATIVES_PREFIX}{run_date}/{card['id']}.json"
        try:
            s3.put_object(Bucket=digests_bucket, Key=key,
                          Body=json.dumps(card, ensure_ascii=False, indent=2).encode("utf-8"),
                          ContentType="application/json")
            keys.append(key)
        except Exception as exc:  # pragma: no cover
            print(f"Warning: write ecosystem card failed: {exc}")

    # No external source AND no hub incident to propagate ⇒ honestly source-gated.
    status = "ok" if keys else ("source_gated" if not external else "no_incidents")
    return {"statusCode": 200, "body": json.dumps({
        "status": status, "as_of": run_date, "hubs": len(hubs),
        "external_edges": len(external), "incidents": len(incidents),
        "emitted": len(keys), "run_at": run_at_now(),
    })}
