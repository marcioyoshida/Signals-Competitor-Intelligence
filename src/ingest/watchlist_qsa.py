"""P1 — watchlist QSA enrichment: the person-layer input for OncaOperatives.

`operatives` (ADR 003 Wave 3) resolves person nodes + common-control edges but sits
`source_gated` because ingestion never fetches the *tracked* competitors' QSA — the
existing Receita enrichment (`receita_cnpj.enrich_entrants`) runs only over NEW BCB
entrants, and the watchlist entities are established, not entrants. This closes that
gap: for each tracked entity that has a CNPJ root in the registry, fetch its quadro de
sócios/administradores (QSA) from BrasilAPI and emit a per-entity slice
(`graph/watchlist_qsa.json`) that operatives consumes. The axis self-activates on
arrival — no synth change needed (input-gating discipline).

**LGPD — masked doc only, never a full CPF.** Receita publishes the QSA with the
sócio's CPF **masked** (`***XXXXXX**`, only the middle six digits public). We keep that
masked form as a *disambiguation / control-cohort key* — it separates homonyms (two
different "João Silva") and cohorts genuine same-person control across entities, which
name alone cannot. The full CPF is never fetched (BrasilAPI does not expose it),
reconstructable (6 of 11 digits), nor stored. `_safe_mask` additionally re-masks
defensively, so even a source that leaked an unmasked CPF could not persist one here.
Only PF sócios (`identificador_de_socio == 2`) and public professional roles are kept;
companies (PJ) are dropped — they are entities, not people.

The QSA is slow-changing, so refreshes are TTL-gated per entity and bounded per run
(`max_lookups`), so a cold start spreads gently across a few runs rather than hammering
the public API. The core (`extract_socios`, `refresh`) is pure — fetch/clock injected —
so tests need no network.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from typing import Any, Callable

import boto3

from src.ingest import receita_cnpj

WATCHLIST_QSA_KEY = "graph/watchlist_qsa.json"

# QSA qualificação (free text) -> canonical operatives role. Control-bearing roles
# (sócio/controlador) are what drive common-control cohorting; the rest still mint
# person nodes with their true role edge.
def _role_of(qual: str | None) -> str:
    q = str(qual or "").lower()
    if "conselh" in q:
        return "conselheiro"
    if "diretor" in q or "presidente" in q:
        return "diretor"
    if "administrador" in q or "sócio" in q or "socio" in q or "titular" in q:
        return "sócio"
    return "sócio"  # QSA membership defaults to a controlling/ownership role


# Control roles first (they carry the cohort-of-control signal), then board/direction.
_ROLE_PRIORITY = {"sócio": 0, "diretor": 1, "conselheiro": 2}


def _safe_mask(value: str | None) -> str | None:
    """Return a MASKED partial CPF. Never emits a full CPF.

    BrasilAPI already masks PF docs (`***XXXXXX**`). Defensively, if a value ever
    arrives with a long unmasked digit run, re-mask it to the middle six digits so a
    full CPF can never be persisted through this path.
    """
    s = str(value or "").strip()
    if not s:
        return None
    if "*" in s:
        return s  # already masked by the source
    digits = re.sub(r"\D", "", s)
    if len(digits) == 11:                       # an unmasked CPF slipped in — mask it
        return f"***{digits[3:9]}**"
    return s if len(digits) < 7 else None       # anything else non-CPF-shaped -> drop


def mask_digits(doc_mask: str | None) -> str:
    """The public middle-six digits of a masked CPF — the disambiguation key."""
    m = re.search(r"\*+\s*(\d{6})\s*\*+", str(doc_mask or ""))
    return m.group(1) if m else ""


def extract_socios(data: dict[str, Any], *, max_persons: int = 20) -> list[dict[str, Any]]:
    """Pure: a BrasilAPI CNPJ payload -> control-relevant PF sócios (masked doc)."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for q in data.get("qsa") or []:
        if not isinstance(q, dict):
            continue
        # 2 = natural person (PF). Drop PJ (companies) — they are entities, not people.
        if str(q.get("identificador_de_socio") or "") not in ("2",):
            continue
        name = str(q.get("nome_socio") or "").strip()
        if not name:
            continue
        role = _role_of(q.get("qualificacao_socio"))
        doc_mask = _safe_mask(q.get("cnpj_cpf_do_socio"))
        dedup = (name.upper(), mask_digits(doc_mask))
        if dedup in seen:
            continue
        seen.add(dedup)
        out.append({"name": name, "role": role, "doc_mask": doc_mask,
                    "qual": q.get("qualificacao_socio")})
    out.sort(key=lambda s: _ROLE_PRIORITY.get(s["role"], 9))
    return out[:max_persons]


def refresh(
    entities: list[dict[str, Any]],
    prev: dict[str, Any],
    *,
    fetch: Callable[[str | None], dict[str, Any] | None],
    now: dt.datetime,
    ttl_days: int = 30,
    max_lookups: int = 10,
    max_persons: int = 20,
) -> dict[str, Any]:
    """Pure-ish: refresh the per-entity QSA slice, TTL-gated and bounded per run.

    `entities` = [{entity, cnpj}] (cnpj = 8-digit root or full). `prev` = the prior
    slice. Re-fetches only entities whose cache is older than ttl_days, up to
    max_lookups per run, so a cold start spreads across runs. Returns the new slice.
    """
    prev_ents = (prev or {}).get("entities", {})
    out: dict[str, Any] = dict(prev_ents)  # keep fresh cache entries as-is
    done = 0
    for e in entities:
        ent = e.get("entity")
        cnpj = e.get("cnpj")
        if not ent or not cnpj:
            continue
        cached = prev_ents.get(ent)
        if cached and not _stale(cached.get("fetched_at"), now, ttl_days):
            continue
        if done >= max_lookups:
            continue  # leave stale ones for a later run (bounded API use)
        done += 1
        full = receita_cnpj.full_cnpj(cnpj)
        data = fetch(cnpj)
        if not data:
            # keep any prior entry; record the attempt so we don't spin on it forever
            if cached:
                out[ent] = {**cached, "fetched_at": now.isoformat(timespec="seconds")}
            continue
        socios = extract_socios(data, max_persons=max_persons)
        out[ent] = {
            "cnpj": full,
            "url": f"https://brasilapi.com.br/api/cnpj/v1/{full}" if full else None,
            "fetched_at": now.isoformat(timespec="seconds"),
            "socios": socios,
        }
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "refreshed": done,
        "entities": out,
    }


def _stale(fetched_at: str | None, now: dt.datetime, ttl_days: int) -> bool:
    if not fetched_at:
        return True
    try:
        ts = dt.datetime.fromisoformat(str(fetched_at))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        return (now - ts).days >= ttl_days
    except Exception:
        return True


# --- registry + S3 plumbing -------------------------------------------------
def _watchlist_entities() -> list[dict[str, Any]]:
    """Tracked entities that carry a CNPJ root in the registry: [{entity, cnpj}]."""
    try:
        from src.synth import entity_registry

        out: list[dict[str, Any]] = []
        for e in entity_registry.list_entities():
            roots = e.get("cnpj_roots") or []
            if roots and e.get("entity_id"):
                out.append({"entity": e["entity_id"], "cnpj": roots[0]})
        return out
    except Exception as exc:  # pragma: no cover - registry best-effort
        print(f"Warning: watchlist QSA registry load failed: {exc}")
        return []


def _load(bucket: str, key: str, s3: Any) -> dict[str, Any]:
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8"))
    except Exception:  # pragma: no cover - absent on first run
        return {}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Refresh graph/watchlist_qsa.json from the tracked entities' Receita QSA."""
    bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}
    if os.environ.get("ONCA_QSA_ENABLED", "1") not in ("1", "true", "True"):
        return {"statusCode": 200, "body": json.dumps({"status": "disabled"})}

    s3 = boto3.client("s3")
    ttl = int(os.environ.get("ONCA_QSA_TTL_DAYS", "30"))
    max_lookups = int(os.environ.get("ONCA_QSA_MAX_LOOKUPS", "10"))
    max_persons = int(os.environ.get("ONCA_QSA_MAX_PERSONS", "20"))

    entities = _watchlist_entities()
    prev = _load(bucket, WATCHLIST_QSA_KEY, s3)
    slice_ = refresh(entities, prev, fetch=receita_cnpj.fetch_cnpj,
                     now=dt.datetime.now(dt.timezone.utc), ttl_days=ttl,
                     max_lookups=max_lookups, max_persons=max_persons)

    published = None
    try:
        s3.put_object(
            Bucket=bucket, Key=WATCHLIST_QSA_KEY,
            Body=json.dumps(slice_, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json", CacheControl="no-cache")
        published = f"s3://{bucket}/{WATCHLIST_QSA_KEY}"
    except Exception as exc:  # pragma: no cover - publish best-effort
        print(f"Warning: watchlist QSA publish failed: {exc}")

    covered = sum(1 for r in slice_["entities"].values() if r.get("socios"))
    persons = sum(len(r.get("socios") or []) for r in slice_["entities"].values())
    return {
        "statusCode": 200,
        "body": json.dumps({
            "status": "ok",
            "watchlist": len(entities),
            "entities_with_qsa": covered,
            "socios_total": persons,
            "refreshed_this_run": slice_["refreshed"],
            "published": published,
        }),
    }
