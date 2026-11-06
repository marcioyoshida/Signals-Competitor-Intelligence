"""Ingest BCB institutions-in-operation registry — new-entrant early warning.

BCB does not publish a clean 'pending authorization process' feed. What it
does publish is the list of institutions currently in operation. A NEW
entity appearing in that registry is the observable event: 'a competitor
just cleared authorization to operate'.

Live API (verified 2026-07-19):
  Instituicoes_em_funcionamento v1 — working EntitySets
  https://olinda.bcb.gov.br/olinda/servico/Instituicoes_em_funcionamento/versao/v1

  EntitySets used:
    - SedesBancoComMultCE  (banks + foreign bank branches)
    - SedesSociedades      (non-bank societies, incl. payment institutions)
    - SedesCooperativas
    - SedesConsorcios

BcBase v2's EntidadesSupervisionadas is a FunctionImport(dataBase) that
currently returns HTTP 500 for known date formats — kept as a future
fallback once BCB fixes it, not used as the primary path.

Lambda port note: handler wraps fetch_authorized(); JsonState → DynamoDB.
"""
from __future__ import annotations

from typing import Any

import requests

FUNCIONAMENTO = (
    "https://olinda.bcb.gov.br/olinda/servico/Instituicoes_em_funcionamento/versao/v1/odata"
)

# Working EntitySets on Instituicoes_em_funcionamento (live-verified).
DEFAULT_RESOURCES: list[str] = [
    "SedesBancoComMultCE",
    "SedesSociedades",
    "SedesCooperativas",
    "SedesConsorcios",
]

# Optional segment filter (case-insensitive substring). Empty = keep all
# (classification below prioritizes fintech licenses without dropping the rest).
RELEVANT_TYPES: list[str] = []

# Normalize the registry's SEGMENTO/CLASSE into a compact license class. The
# fintech-entrant licenses (IP, SCD, SEP, SCFI, microcrédito) are what a
# "quiet registration" radar cares about; ordered so the first substring match
# wins. Values verified live against SedesSociedades/SedesBancoComMultCE.
_LICENSE_RULES: list[tuple[str, str]] = [
    ("instituição de pagamento", "Instituição de Pagamento"),
    ("crédito direto", "Crédito Direto (SCD)"),
    ("empréstimo entre pessoas", "Empréstimo P2P (SEP)"),
    ("financiamento e investimento", "Financeira (SCFI)"),
    ("microempreendedor", "Microcrédito (SCMEPP)"),
    ("banco", "Banco"),
    ("corretora", "Corretora/DTVM"),
    ("distribuidora", "Corretora/DTVM"),
    ("arrendamento", "Leasing"),
    ("fomento", "Agência de Fomento"),
    ("hipotecária", "Companhia Hipotecária"),
    ("cooperativa", "Cooperativa"),
    # SedesCooperativas rows classify as "Singular" / "Central".
    ("singular", "Cooperativa"),
    ("central", "Cooperativa"),
    ("consórcio", "Consórcio"),
    ("consorcio", "Consórcio"),
]

# License classes considered "fintech entrants" for the radar.
FINTECH_LICENSES = frozenset({
    "Instituição de Pagamento",
    "Crédito Direto (SCD)",
    "Empréstimo P2P (SEP)",
    "Financeira (SCFI)",
    "Microcrédito (SCMEPP)",
})


def classify_license(entity_type: str | None) -> str:
    """Map a raw SEGMENTO/CLASSE to a compact license class label."""
    text = (entity_type or "").lower()
    for needle, label in _LICENSE_RULES:
        if needle in text:
            return label
    return (entity_type or "Outro").strip() or "Outro"


def fetch_authorized(
    resources: list[str] | None = None, top: int = 10000
) -> list[dict[str, Any]]:
    """Fetch the current in-operation institutions registry (all resources)."""
    resources = resources or DEFAULT_RESOURCES
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for resource in resources:
        url = f"{FUNCIONAMENTO}/{resource}?$top={top}&$format=json"
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        for row in resp.json().get("value", []):
            if not _keep(row):
                continue
            rec = _normalize(row, resource)
            if rec["id"] in seen:
                continue
            seen.add(rec["id"])
            out.append(rec)
    return out


def _keep(row: dict[str, Any]) -> bool:
    if not RELEVANT_TYPES:
        return True
    etype = (row.get("SEGMENTO") or row.get("CLASSE") or "")
    return any(t.lower() in etype.lower() for t in RELEVANT_TYPES)


def _normalize(row: dict[str, Any], resource: str) -> dict[str, Any]:
    """Map a live Instituicoes_em_funcionamento row to a signal record."""
    cnpj = row.get("CNPJ") or row.get("Cnpj")
    name = row.get("NOME_INSTITUICAO") or row.get("Nome")
    entity_type = (
        row.get("SEGMENTO")
        or row.get("CLASSE")
        or resource  # cooperatives/consortia lack SEGMENTO
    )
    ident = cnpj or name or "unknown"
    license_class = classify_license(entity_type)
    return {
        "id": f"bcb-auth:{ident}",
        "source": "BCB-Autorizacoes",
        "kind": "competitor",
        "cnpj": cnpj,
        "name": name,
        "entity_type": entity_type,
        "license_class": license_class,
        "is_fintech": license_class in FINTECH_LICENSES,
        "legal_nature": None,
        "situation": "em_funcionamento",
        "registry": resource,
        "uf": row.get("UF"),
        "municipio": row.get("MUNICIPIO"),
        "raw": row,
    }


# --- R2 / #74: register-VERIFIED certifications for tracked entities -------------------
# The in-operation registry is a real authorization fact per institution (CNPJ + SEGMENTO/CLASSE +
# "em funcionamento"). Joining it to the tracked entities by CNPJ root lifts `certifications` from
# 0% real to a register-verified value — the honest answer to the CPO decision "is competitor X
# authorized for segment Y?", which the industry-INFERRED derivation could only assume.
def certification_label(row: dict[str, Any]) -> str:
    """A verified certification string from an in-operation registry row:
    ``BCB · <license_class> · em funcionamento``."""
    lic = (row.get("license_class") or row.get("entity_type") or "").strip() or "instituição autorizada"
    return f"BCB · {lic} · em funcionamento"


def certifications_by_cnpj(authorized: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Map CNPJ 8-digit root → the set of verified BCB certification labels. Rows without a CNPJ
    are skipped (they cannot be joined to a tracked entity)."""
    out: dict[str, set[str]] = {}
    for r in authorized:
        root = "".join(ch for ch in str(r.get("cnpj") or "") if ch.isdigit())[:8]
        if not root:
            continue
        out.setdefault(root, set()).add(certification_label(r))
    return out


def apply_verified_certifications(
    authorized: list[dict[str, Any]], *, source: str = "structured", table: Any | None = None
) -> list[str]:
    """#74/R2: stamp register-verified ``certifications`` onto every tracked entity that resolves to
    a BCB in-operation row (by CNPJ root). ``structured`` provenance — wins over the industry-
    inferred derivation but (ADR-018) never demotes a curated list. Returns the entity_ids updated.
    Best-effort/pure-local (no network): iterates the already-fetched ``authorized`` rows."""
    from src.synth import entity_registry as er

    changed: list[str] = []
    seen: set[str] = set()
    for root, labels in certifications_by_cnpj(authorized).items():
        eid = er.resolve_by_cnpj(root, table=table)
        if not eid or eid in seen:
            continue
        seen.add(eid)
        cur = set((er.get_entity(eid, table=table) or {}).get("certifications") or [])
        if er.set_certifications(eid, cur | labels, source=source, table=table):
            changed.append(eid)
    return changed


# --- #358 register EXITS ---------------------------------------------------------------------
# This module only ever saw ENTRANTS. An institution that leaves "em funcionamento" (cancelled
# authorisation, liquidação extrajudicial, merger) produced no event at all. Each run now keeps a
# compact snapshot (cnpj root → name/segment/resource) and diffs it against the previous one.
SNAPSHOT_KEY = "reference/bcb_institutions.json"
EXITS_KEY = "distress/registry_exits.json"
# A resource whose row count fell by more than this share is treated as a PARTIAL fetch (BCB
# pagination/outage), and the diff for it is skipped — a false "exit" would be a false claim.
MAX_SHRINK = 0.05
EXITS_KEEP_DAYS = 120


def _root(v: Any) -> str | None:
    d = "".join(ch for ch in str(v or "") if ch.isdigit())
    return d[:8] if len(d) >= 8 else None


def snapshot(records: list[dict[str, Any]], as_of: str) -> dict[str, Any]:
    """Compact register snapshot: ``{"as_of", "counts": {resource: n}, "institutions": {root: row}}``."""
    inst: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for r in records or []:
        root = _root(r.get("cnpj"))
        res = str(r.get("registry") or "?")
        counts[res] = counts.get(res, 0) + 1
        if not root or root in inst:
            continue
        inst[root] = {"cnpj": root, "name": r.get("name"), "license_class": r.get("license_class"),
                      "entity_type": r.get("entity_type"), "registry": res,
                      "uf": r.get("uf"), "municipio": r.get("municipio")}
    return {"as_of": as_of, "counts": counts, "institutions": inst}


def diff_exits(prev: dict[str, Any] | None, cur: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """``(exits, skipped_resources)``: institutions in ``prev`` but not in ``cur``, dated
    ``cur["as_of"]``. A resource that shrank by more than ``MAX_SHRINK`` (or vanished) is skipped
    whole. No previous snapshot → no exits (first run seeds)."""
    if not prev or not prev.get("institutions"):
        return [], []
    pc, cc = prev.get("counts") or {}, cur.get("counts") or {}
    skipped = [res for res, n in pc.items()
               if n and (cc.get(res, 0) < n * (1 - MAX_SHRINK))]
    now = cur.get("institutions") or {}
    exits = []
    for root, row in (prev.get("institutions") or {}).items():
        if root in now or row.get("registry") in skipped:
            continue
        exits.append({**row, "date": cur.get("as_of"), "last_seen_in_register": prev.get("as_of")})
    return exits, skipped


def merge_exits(doc: dict[str, Any] | None, exits: list[dict[str, Any]], as_of: str) -> dict[str, Any]:
    """Durable exits log (dedup by cnpj root, first exit date kept, pruned after
    ``EXITS_KEEP_DAYS``). An institution that RE-appears is dropped from the log by the caller."""
    import datetime as dt

    by = {x["cnpj"]: x for x in ((doc or {}).get("exits") or []) if x.get("cnpj")}
    for x in exits:
        by.setdefault(x["cnpj"], x)
    floor = (dt.date.fromisoformat(as_of[:10]) - dt.timedelta(days=EXITS_KEEP_DAYS)).isoformat()
    kept = sorted((x for x in by.values() if str(x.get("date") or "") >= floor),
                  key=lambda x: str(x.get("date") or ""), reverse=True)
    return {"as_of": as_of, "count": len(kept), "exits": kept}


def update_register_state(records: list[dict[str, Any]], bucket: str, *, s3: Any = None,
                          as_of: str | None = None) -> dict[str, Any]:
    """Persist the snapshot, diff it against the previous one, fold exits into the durable log.
    Best-effort at the call site; never runs on an empty fetch."""
    import datetime as dt
    import json

    import boto3

    if not records:
        return {"skipped": "empty fetch"}
    s3 = s3 or boto3.client("s3")
    as_of = as_of or dt.date.today().isoformat()

    def _get(key: str) -> dict[str, Any]:
        try:
            return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        except Exception:
            return {}

    def _put(key: str, doc: dict[str, Any]) -> None:
        s3.put_object(Bucket=bucket, Key=key, ContentType="application/json",
                      Body=json.dumps(doc, ensure_ascii=False).encode("utf-8"))

    prev = _get(SNAPSHOT_KEY)
    cur = snapshot(records, as_of)
    exits, skipped = diff_exits(prev, cur)
    log = _get(EXITS_KEY)
    # an institution back in the register is no longer an exit
    log["exits"] = [x for x in (log.get("exits") or []) if x.get("cnpj") not in cur["institutions"]]
    log = merge_exits(log, exits, as_of)
    if skipped:  # keep the previous rows of a partially-fetched resource in the snapshot
        for root, row in (prev.get("institutions") or {}).items():
            if row.get("registry") in skipped:
                cur["institutions"].setdefault(root, row)
                cur["counts"][row["registry"]] = max(cur["counts"].get(row["registry"], 0),
                                                     (prev.get("counts") or {}).get(row["registry"], 0))
    _put(SNAPSHOT_KEY, cur)
    _put(EXITS_KEY, log)
    return {"institutions": len(cur["institutions"]), "new_exits": len(exits),
            "exits_logged": log["count"], "skipped_resources": skipped}


def inspect(resource: str | None = None) -> None:
    """One-shot schema check against a live EntitySet."""
    resource = resource or DEFAULT_RESOURCES[0]
    url = f"{FUNCIONAMENTO}/{resource}?$top=1&$format=json"
    resp = requests.get(url, timeout=60)
    print(f"HTTP {resp.status_code}  {url}")
    resp.raise_for_status()
    rows = resp.json().get("value", [])
    if not rows:
        print("No rows — list EntitySets at:")
        print(f"  {FUNCIONAMENTO}/")
        return
    print("Keys:", list(rows[0].keys()))
    print("Sample row:", rows[0])


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "inspect":
        inspect(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        rows = fetch_authorized()
        print(f"{len(rows)} institutions in operation")
        for r in rows[:15]:
            print(
                f"  {(r['entity_type'] or '?')[:30]:30}  "
                f"{(r['name'] or r['cnpj'] or '?')[:50]}"
            )
