"""Registry CRUD API — the operator control plane for entity curation.

The `onca-entities` registry is the single source of truth for per-entity
curation (aliases, industries, trust, display_name, news_term, ambiguous_tokens).
This is the REST surface over the `ENT#` records so curation is managed as an
API-served product, no code deploy. It is an INTERNAL/operator API — the registry
is never shipped to tenants; tenants consume the derived feed, not the registry.

Auth mirrors the review endpoint: fronted by CloudFront under `/api/registry/*`,
gated by the same basic-auth CloudFront Function, and the Function URL (AuthType
NONE) is protected by a shared origin secret CloudFront injects — so the edge
can't be bypassed. Read verbs are safe; writes mutate the table.

Routes (under /api/registry):
  GET    /entities                 list curation records (?include_inactive=1)
  POST   /entities                 create an entity (entity_id, display_name, aliases[])
  GET    /entities/{id}            fetch one
  PATCH  /entities/{id}            partial update (whitelisted curation fields)
  DELETE /entities/{id}            soft-delete (active=False)
  POST   /entities/{id}/aliases    add data-derived alias forms (reindexes ALIAS#)
  GET    /industries               the industry taxonomy
  GET    /reviews                  pending review queue (ADR step 5)
  POST   /reviews/{id}             resolve a review ({decision, industries?})
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any


def _resp(status: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False, default=str),
    }


def _method(event: dict[str, Any]) -> str:
    rc = (event.get("requestContext") or {}).get("http") or {}
    return str(rc.get("method") or event.get("httpMethod") or "GET").upper()


def _path(event: dict[str, Any]) -> str:
    return str(event.get("rawPath") or event.get("path") or "")


def _segments(path: str) -> list[str]:
    """Path parts after the /api/registry prefix (e.g. ['entities', 'c6'])."""
    p = path.split("/api/registry", 1)[-1]
    return [s for s in p.split("/") if s]


def _body(event: dict[str, Any]) -> dict[str, Any] | None:
    raw = event.get("body")
    if not raw:
        return {}
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        return None


def _truthy(event: dict[str, Any], key: str) -> bool:
    qs = event.get("queryStringParameters") or {}
    if isinstance(qs, dict) and str(qs.get(key, "")).lower() in ("1", "true", "yes"):
        return True
    return f"{key}=1" in str(event.get("rawQueryString") or "").lower()


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    # Origin secret: present only when the request came through CloudFront.
    secret = os.environ.get("ONCA_ORIGIN_SECRET")
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    if secret and headers.get("x-onca-origin") != secret:
        return _resp(403, {"error": "forbidden"})

    from src.synth import entity_registry as reg

    method = _method(event)
    segs = _segments(_path(event))
    if not segs:
        return _resp(200, {"service": "onca-registry", "resources": ["entities", "industries", "reviews"]})

    resource = segs[0]
    try:
        if resource == "entities":
            return _entities(method, segs[1:], event, reg)
        if resource == "industries" and method == "GET":
            return _resp(200, {"industries": reg.industry_rollup()})
        if resource == "reviews":
            return _reviews(method, segs[1:], event, reg)
    except Exception as exc:  # pragma: no cover - defensive; never leak a stack
        print(f"registry_api error: {exc}")
        return _resp(500, {"error": "internal error"})
    return _resp(404, {"error": "not found"})


def _entities(method: str, rest: list[str], event: dict[str, Any], reg: Any) -> dict[str, Any]:
    # Collection: /entities
    if not rest:
        if method == "GET":
            items = reg.list_entities(include_inactive=_truthy(event, "include_inactive"))
            return _resp(200, {"entities": items, "count": len(items)})
        if method == "POST":
            body = _body(event)
            if body is None:
                return _resp(400, {"error": "invalid JSON body"})
            eid = str(body.get("entity_id") or "").strip()
            name = str(body.get("display_name") or "").strip()
            aliases = body.get("aliases") or []
            if not eid or not name or not isinstance(aliases, list) or not aliases:
                return _resp(400, {"error": "entity_id, display_name and aliases[] required"})
            if reg.get_entity(eid) is not None:
                return _resp(409, {"error": "entity exists", "entity_id": eid})
            ent = reg.put_entity(
                eid, name, [str(a) for a in aliases],
                industries=body.get("industries") or (),
                confidence=str(body.get("confidence") or "curated"),
                news_term=body.get("news_term"),
                ambiguous_tokens=body.get("ambiguous_tokens"),
                controllers=body.get("controllers"),
            )
            return _resp(201, {"entity": ent})
        return _resp(405, {"error": "method not allowed"})

    # Member: /entities/{id}[/aliases]
    eid = rest[0]
    sub = rest[1] if len(rest) > 1 else None

    if sub == "aliases" and method == "POST":
        body = _body(event) or {}
        forms = body.get("aliases") or body.get("forms") or []
        if not isinstance(forms, list) or not forms:
            return _resp(400, {"error": "aliases[] required"})
        added = reg.accumulate_aliases(eid, [str(f) for f in forms])
        return _resp(200, {"entity_id": eid, "added": added})
    if sub is not None:
        return _resp(404, {"error": "not found"})

    if method == "GET":
        ent = reg.get_entity(eid)
        return _resp(200, {"entity": ent}) if ent else _resp(404, {"error": "not found"})
    if method in ("PATCH", "PUT"):
        body = _body(event)
        if body is None:
            return _resp(400, {"error": "invalid JSON body"})
        ent = reg.update_entity(eid, body)
        return _resp(200, {"entity": ent}) if ent else _resp(404, {"error": "not found"})
    if method == "DELETE":
        ent = reg.deactivate_entity(eid)
        return _resp(200, {"entity": ent}) if ent else _resp(404, {"error": "not found"})
    return _resp(405, {"error": "method not allowed"})


def _reviews(method: str, rest: list[str], event: dict[str, Any], reg: Any) -> dict[str, Any]:
    if not rest and method == "GET":
        items = reg.list_reviews(status="pending")
        return _resp(200, {"reviews": items, "count": len(items)})
    if rest and method == "POST":
        body = _body(event) or {}
        decision = body.get("decision")
        if decision not in ("approved", "rejected"):
            return _resp(400, {"error": "decision (approved|rejected) required"})
        extra: dict[str, Any] = {}
        inds = body.get("industries")
        if isinstance(inds, list):
            extra["industries"] = [str(i) for i in inds if str(i).strip()]
        item = reg.resolve_review(rest[0], decision, payload=extra or None)
        if item is None:
            return _resp(409, {"status": "noop", "detail": "missing or already decided"})
        return _resp(200, {"status": decision, "review_id": rest[0]})
    return _resp(405, {"error": "method not allowed"})
