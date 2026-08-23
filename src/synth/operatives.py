"""Wave 3 (ADR 003, Shift 3) — Operatives: the person layer, public-record-scoped.

Competitive intelligence is ultimately about **people** — the operatives who carry
capability and relationships *across* corporate boundaries ("the team that built a
rival's credit engine just left to found it"; "the controller behind this quietly
registered SCD"). This promotes person-name strings we already touch (QSA
controllers/sócios, named parties in DOU acts) into **person nodes** with typed,
time-bounded role edges to entities — and grounds otherwise-speculative A–B edges in
a *sourced* fact ("the same controller bridges them").

**LGPD & defamation — the hardest guardrail yet (ADR 003 Decision 6 + risks).**
Person narratives are about *named humans*, so:
- **Scope is strictly public professional roles from public records** (QSA, CVM, DOU,
  court dockets) — a **public figure acting in a corporate capacity**, never a private
  individual incidentally named, never private-life inference.
- **No CPF is stored or keyed on** (protected/masked, unlike the public CNPJ).
- Person resolution is **more conservative than entity resolution**: a node is only
  minted with **name + role + affiliating document** (all three), and **every person
  node is review-gated from day one** — nothing is ever auto-asserted. A false "person
  X moved to a competitor / is behind shell Y / is a litigation respondent" is
  defamatory, so operatives only ever **propose**, labeled and citing the record.

**Input-gated, honestly.** The current ingestion does not yet surface individual-person
names (QSA `items` are empty; the CVM `admin`/`manager`/`leader` fields are
*institutional* — DTVMs/asset managers, not people). So this emits `source_gated`
today; the resolution mechanism is built and tested, and activates with no code change
when person-bearing fields (QSA sócios, DOU parties) land — exactly the ADR's
"promote those strings into resolved nodes."
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import unicodedata
from collections import defaultdict
from typing import Any, Iterator

import boto3

from src.synth.synthesize import run_at_now, run_date_today

PERSONS_KEY = "graph/persons.json"
PERSON_PROPOSALS_KEY = "graph/person_proposals.json"

# Fields that carry *individual-person* names in public records. Deliberately EXCLUDES
# admin/manager/leader — in this pipeline those are institutional (fund administrators
# and asset managers, i.e. companies), not people.
PERSON_ROLE_FIELDS: dict[str, str] = {
    "controllers": "controlador",
    "controller": "controlador",
    "socios": "sócio",
    "socio": "sócio",
    "directors": "diretor",
    "board": "conselheiro",
    "respondents": "requerido",
    "parties": "parte",
    "counsel": "advogado",
}

# Tokens that mark a "person" string as actually a COMPANY (never mint a person node
# from these — the LGPD scope is humans in a corporate role, not the corporation).
_COMPANY_TOKENS = re.compile(
    r"\b(S/?A|S\.A\.?|LTDA|DTVM|CCTVM|EIRELI|HOLDING|ASSET|GESTORA|GESTAO|BANCO|"
    r"SEGUROS|PARTICIPACOES|FUNDO|CAPITAL|INVESTIMENTOS|DISTRIBUIDORA|CORRETORA)\b",
    re.IGNORECASE,
)


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _norm_name(name: str) -> str:
    """Normalized person key: accent-folded, upper, single-spaced. NOT a CPF."""
    return re.sub(r"\s+", " ", _strip_accents(str(name or "")).upper()).strip()


def _is_person_name(name: str) -> bool:
    """Conservative: >=2 name tokens, letters only, and no company marker."""
    n = str(name or "").strip()
    if _COMPANY_TOKENS.search(_strip_accents(n)):
        return False
    tokens = [t for t in re.split(r"\s+", n) if t]
    if len(tokens) < 2:
        return False
    return all(re.fullmatch(r"[A-Za-zÀ-ÿ.'\-]+", t) for t in tokens)


def _entity_of(signal: dict[str, Any]) -> str | None:
    """Which tracked entity this signal affiliates the person to (best-effort)."""
    for k in ("entity", "entity_id"):
        if signal.get(k):
            return signal[k]
    ents = signal.get("entities") or []
    return ents[0] if ents else None


def _document_of(signal: dict[str, Any]) -> str | None:
    """The affiliating public document (required — no doc, no person node)."""
    for k in ("id", "url", "protocol", "doc_id", "source"):
        if signal.get(k):
            return str(signal[k])
    return None


def iter_person_roles(signals: list[dict[str, Any]]) -> Iterator[tuple[str, str, str | None, str]]:
    """Yield (person_name, role, affiliating_entity, document) from public-record fields.

    Only fields in PERSON_ROLE_FIELDS, only strings that look like a human name, only
    with an affiliating document. This is the whole LGPD surface — kept narrow.
    """
    for sig in signals:
        if not isinstance(sig, dict):
            continue
        doc = _document_of(sig)
        if not doc:
            continue
        ent = _entity_of(sig)
        for field, role in PERSON_ROLE_FIELDS.items():
            val = sig.get(field)
            if not val:
                continue
            names = val if isinstance(val, list) else [val]
            for nm in names:
                if isinstance(nm, dict):
                    nm = nm.get("name") or nm.get("nome")
                if nm and _is_person_name(str(nm)):
                    yield str(nm).strip(), role, ent, doc


def resolve_persons(signals: list[dict[str, Any]], *, run_date: str) -> dict[str, Any]:
    """Pure: signals -> {persons, proposals, common_control}. All review-gated.

    A person node needs **name + role + document** (all three). Homonyms are flagged
    (same name, multiple distinct affiliating documents/entities) but never merged
    silently. A person affiliated (as controller/sócio) to >=2 tracked entities yields
    a **common_control** edge proposal that grounds a relational edge as a sourced fact.
    """
    # normalized-name -> aggregate
    acc: dict[str, dict[str, Any]] = {}
    for name, role, ent, doc in iter_person_roles(signals):
        key = _norm_name(name)
        if not key:
            continue
        p = acc.setdefault(key, {"key": key, "display": name.strip(), "roles": set(),
                                 "entities": set(), "documents": set()})
        p["roles"].add(role)
        if ent:
            p["entities"].add(ent)
        p["documents"].add(doc)

    persons: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    control_edges: list[dict[str, Any]] = []
    _controllers = {"controlador", "sócio"}

    for key, p in sorted(acc.items()):
        ents = sorted(p["entities"])
        roles = sorted(p["roles"])
        docs = sorted(p["documents"])
        ambiguous = len(docs) > 1 and len(ents) != 1  # possible homonym / needs care
        node = {
            "id": f"person:{key.lower().replace(' ', '_')}",
            "display": p["display"],
            "roles": roles,
            "entities": ents,
            "n_documents": len(docs),
            "ambiguous": ambiguous,
            "status": "pending",           # ALWAYS review-gated — never auto-asserted
            "lgpd_scope": "public_professional_role",
            "created": run_date,
        }
        persons.append(node)
        proposals.append({
            "id": f"person:{node['id']}",
            "kind": "person",
            "person": p["display"],
            "roles": roles,
            "entities": ents,
            "ambiguous": ambiguous,
            "text": (f"{p['display']} — {', '.join(roles)}"
                     + (f" de {', '.join(ents)}" if ents else "")
                     + ". Papel profissional público; requer curadoria (LGPD)."),
            "evidence_ids": docs[:12],
            "status": "pending",
            "created": run_date,
        })
        # person behind >=2 tracked entities in a control role -> common_control edge
        if len(ents) >= 2 and (p["roles"] & _controllers):
            control_edges.append({
                "id": f"common_control:{'-'.join(ents)}:{node['id']}",
                "kind": "common_control",
                "entities": ents,
                "via_person": p["display"],
                "roles": sorted(p["roles"] & _controllers),
                "text": (f"{p['display']} figura como {', '.join(sorted(p['roles'] & _controllers))} "
                         f"em {', '.join(ents)} — controle comum (fonte pública)."),
                "evidence_ids": docs[:12],
                "status": "pending",
                "created": run_date,
            })

    return {"persons": persons, "proposals": proposals, "common_control": control_edges}


def _collect_signals(digest: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a digest's signal slices into a flat list for person scanning."""
    out: list[dict[str, Any]] = []
    for v in (digest or {}).values():
        if isinstance(v, list):
            out.extend(x for x in v if isinstance(x, dict))
        elif isinstance(v, dict):
            items = v.get("items")
            if isinstance(items, list):
                out.extend(x for x in items if isinstance(x, dict))
    return out


def _merge(existing: list[dict[str, Any]], fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {p["id"]: p for p in (existing or []) if p.get("id")}
    for p in fresh:
        prior = by_id.get(p["id"])
        by_id[p["id"]] = ({**p, "status": prior.get("status", "pending"),
                           "created": prior.get("created", p.get("created"))} if prior else p)
    return sorted(by_id.values(), key=lambda p: p.get("id", ""))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Resolve person nodes from the latest digest's public-record fields (review-gated)."""
    digests_bucket = os.environ.get("ONCA_DIGESTS_BUCKET")
    if not digests_bucket:
        return {"statusCode": 200, "body": json.dumps({"status": "no_digests_bucket"})}

    s3 = boto3.client("s3")
    run_date = run_date_today()
    try:
        from src.synth import digest_io

        digest = digest_io.load_latest_digest_from_s3() or {}
    except Exception as exc:  # pragma: no cover - best-effort
        print(f"Warning: operatives digest load failed: {exc}")
        digest = {}

    signals = _collect_signals(digest)
    resolved = resolve_persons(signals, run_date=run_date)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    published = None
    try:
        prev_props = _load_json(digests_bucket, PERSON_PROPOSALS_KEY, s3).get("proposals", [])
        merged = _merge(prev_props, resolved["proposals"] + resolved["common_control"])
        s3.put_object(
            Bucket=digests_bucket, Key=PERSONS_KEY,
            Body=json.dumps({"generated_at": now, "as_of": run_date,
                             "persons": resolved["persons"]}, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json", CacheControl="no-cache")
        s3.put_object(
            Bucket=digests_bucket, Key=PERSON_PROPOSALS_KEY,
            Body=json.dumps({"generated_at": now, "as_of": run_date, "proposals": merged},
                            ensure_ascii=False).encode("utf-8"),
            ContentType="application/json", CacheControl="no-cache")
        published = f"s3://{digests_bucket}/{PERSON_PROPOSALS_KEY}"
    except Exception as exc:  # pragma: no cover - publish best-effort
        print(f"Warning: operatives publish failed: {exc}")

    status = "ok" if resolved["persons"] else "source_gated"
    return {
        "statusCode": 200,
        "body": json.dumps({
            "status": status, "as_of": run_date, "signals_scanned": len(signals),
            "persons": len(resolved["persons"]),
            "common_control": len(resolved["common_control"]),
            "run_at": run_at_now(), "published": published,
        }),
    }


def _load_json(bucket: str, key: str, s3: Any) -> dict[str, Any]:
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8"))
    except Exception:  # pragma: no cover - absent
        return {}
