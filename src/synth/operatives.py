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
- **No full CPF is ever stored or keyed on.** The *public masked* CPF that Receita
  itself publishes in the QSA (`***XXXXXX**`, only the middle six digits) is used
  strictly as a **homonym-disambiguation / control-cohort key** — it separates two
  different "João Silva" and cohorts genuine same-person control across entities, which
  a name alone cannot. It is shown masked for vetting and never reconstructed into a
  full CPF (P1 ingestion force-masks defensively; see `ingest/watchlist_qsa.py`).
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


def _mask_digits(doc_mask: str | None) -> str:
    """Public middle-six digits of a masked CPF — the disambiguation key (never full)."""
    m = re.search(r"\*+\s*(\d{6})\s*\*+", str(doc_mask or ""))
    return m.group(1) if m else ""


def iter_person_roles(
    signals: list[dict[str, Any]],
) -> Iterator[tuple[str, str, str | None, str, str]]:
    """Yield (person_name, role, affiliating_entity, document, doc_mask) from
    public-record fields.

    Only fields in PERSON_ROLE_FIELDS, only strings that look like a human name, only
    with an affiliating document. A dict item may carry its own `role` (e.g. a QSA
    sócio's qualificação) and a masked `doc_mask` (public partial CPF); both override
    the field defaults. This is the whole LGPD surface — kept narrow.
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
                item_role, mask = role, ""
                if isinstance(nm, dict):
                    item_role = str(nm.get("role") or role)
                    mask = _mask_digits(nm.get("doc_mask"))
                    nm = nm.get("name") or nm.get("nome")
                if nm and _is_person_name(str(nm)):
                    yield str(nm).strip(), item_role, ent, doc, mask


def resolve_persons(signals: list[dict[str, Any]], *, run_date: str) -> dict[str, Any]:
    """Pure: signals -> {persons, proposals, common_control}. All review-gated.

    A person node needs **name + role + document** (all three). Homonyms are flagged
    (same name, multiple distinct affiliating documents/entities) but never merged
    silently. A person affiliated (as controller/sócio) to >=2 tracked entities yields
    a **common_control** edge proposal that grounds a relational edge as a sourced fact.
    """
    # (normalized-name, masked-CPF-digits) -> aggregate. The masked-CPF component
    # separates homonyms and cohorts genuine same-person control; when absent, the key
    # falls back to name alone (and such a node is flagged ambiguous below).
    acc: dict[tuple[str, str], dict[str, Any]] = {}
    for name, role, ent, doc, mask in iter_person_roles(signals):
        nkey = _norm_name(name)
        if not nkey:
            continue
        key = (nkey, mask)
        p = acc.setdefault(key, {"nkey": nkey, "mask": mask, "display": name.strip(),
                                 "roles": set(), "entities": set(), "documents": set()})
        p["roles"].add(role)
        if ent:
            p["entities"].add(ent)
        p["documents"].add(doc)

    persons: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    control_edges: list[dict[str, Any]] = []
    _controllers = {"controlador", "sócio"}

    for (nkey, mask), p in sorted(acc.items()):
        ents = sorted(p["entities"])
        roles = sorted(p["roles"])
        docs = sorted(p["documents"])
        # Ambiguous only when we LACK the disambiguator: no masked CPF, yet the same
        # name spans multiple documents/entities (possible homonym). A shared masked
        # CPF across entities is a resolved cohort, not an ambiguity.
        ambiguous = not mask and len(docs) > 1 and len(ents) != 1
        doc_mask = f"***{mask}**" if mask else None
        node = {
            "id": f"person:{nkey.lower().replace(' ', '_')}" + (f":{mask}" if mask else ""),
            "display": p["display"],
            "roles": roles,
            "entities": ents,
            "n_documents": len(docs),
            "doc_mask": doc_mask,          # public masked CPF (partial) — never full
            "ambiguous": ambiguous,
            "status": "pending",           # ALWAYS review-gated — never auto-asserted
            "lgpd_scope": "public_professional_role",
            "created": run_date,
        }
        persons.append(node)  # the full resolved graph knows everyone (cohort math)
        # Relevance gate for the REVIEW QUEUE: only surface control-cohort-relevant
        # people — someone in a control role (sócio/controlador) OR bridging >=2 tracked
        # entities. A lone statutory director of one entity is resolved but not queued,
        # so the analyst sees control signals, not every name on a big bank's board.
        control = bool(p["roles"] & _controllers)
        bridges = len(ents) >= 2
        if control or bridges:
            doc_hint = f" (doc {doc_mask})" if doc_mask else ""
            proposals.append({
                "id": f"person:{node['id']}",
                "kind": "person",
                "person": p["display"],
                "roles": roles,
                "entities": ents,
                "doc_mask": doc_mask,
                "ambiguous": ambiguous,
                "text": (f"{p['display']}{doc_hint} — {', '.join(roles)}"
                         + (f" de {', '.join(ents)}" if ents else "")
                         + ". Papel profissional público; requer curadoria (LGPD)."),
                "evidence_ids": docs[:12],
                "status": "pending",
                "created": run_date,
            })
        # person behind >=2 tracked entities in a control role -> common_control edge.
        # A shared masked CPF makes this a resolved control cohort, not a homonym guess.
        if len(ents) >= 2 and (p["roles"] & _controllers):
            ctrl = sorted(p["roles"] & _controllers)
            ground = (f"mesmo CPF (mascarado {doc_mask})" if mask
                      else "mesmo nome — sem CPF para confirmar; possível homônimo")
            control_edges.append({
                "id": f"common_control:{'-'.join(ents)}:{node['id']}",
                "kind": "common_control",
                "entities": ents,
                "via_person": p["display"],
                "roles": ctrl,
                "doc_mask": doc_mask,
                "grounded": bool(mask),
                "text": (f"{p['display']} figura como {', '.join(ctrl)} em {', '.join(ents)} "
                         f"— controle comum ({ground}; fonte pública)."),
                "evidence_ids": docs[:12],
                "status": "pending",
                "created": run_date,
            })

    return {"persons": persons, "proposals": proposals, "common_control": control_edges}


def _qsa_signals(bucket: str, s3: Any) -> list[dict[str, Any]]:
    """Turn the P1 watchlist-QSA slice into per-entity person signals for resolution.

    Each entity's sócios become one signal carrying the `socios` field (list of
    {name, role, doc_mask}); the affiliating document is the entity's Receita QSA URL.
    """
    try:
        from src.ingest import watchlist_qsa

        slice_ = _load_json(bucket, watchlist_qsa.WATCHLIST_QSA_KEY, s3)
    except Exception:  # pragma: no cover - absent before P1 first runs
        return []
    out: list[dict[str, Any]] = []
    for ent, rec in (slice_.get("entities") or {}).items():
        socios = rec.get("socios") or []
        if socios:
            out.append({"entity": ent, "id": f"qsa:{rec.get('cnpj') or ent}",
                        "url": rec.get("url"), "socios": socios})
    return out


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
    """Idempotent + self-healing. Freshly-generated proposals inherit any human status
    from a prior version; a prior proposal the generator NO LONGER produces is dropped
    if still pending (a tightened gate should clear stale machine noise) but KEPT if a
    human already decided it (audit trail)."""
    prior_by_id = {p["id"]: p for p in (existing or []) if p.get("id")}
    out: dict[str, dict[str, Any]] = {}
    for p in fresh:
        pid = p.get("id")
        if not pid:
            continue
        prior = prior_by_id.get(pid)
        out[pid] = ({**p, "status": prior.get("status", "pending"),
                     "created": prior.get("created", p.get("created"))} if prior else p)
    for pid, p in prior_by_id.items():
        if pid not in out and p.get("status") in ("approved", "rejected"):
            out[pid] = p  # decided-but-no-longer-generated: keep for audit
    return sorted(out.values(), key=lambda p: p.get("id", ""))


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

    signals = _collect_signals(digest) + _qsa_signals(digests_bucket, s3)
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
