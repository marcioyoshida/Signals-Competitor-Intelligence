"""International sanctions lists for the CCO board (#381, epic #384) — ported from Griffon.

Four public bulk lists, no key (structure verified live 2026-11-12):

* **UN Security Council consolidated list** — legally binding in Brazil: Lei 13.810/2019
  requires an immediate asset freeze (BCB Res. 44/2020, CVM Res. 50/2021). ~1k rows, 2 MB.
* **OFAC SDN** (US Treasury) — not binding in Brazil but decisive for correspondent banking,
  card schemes and FX. ~19.5k rows, 30 MB. Brazilian entities carry their CNPJ
  (``idType`` "Tax ID No.", ``idCountry`` Brazil).
* **EU financial sanctions (FSF)** — ~6.3k rows, 26 MB.
* **UK Sanctions List (FCDO)** — ~6.4k rows, 22 MB. Griffon's OFSI ConList URL is dead (409).

What reaches the board:

1. **Brazil-linked entities** — nationality / address / place of registration / tax id in
   Brazil. A fact from the list, no matching involved.
2. **Roster matches** — a tracked Onça entity whose CNPJ root equals a listed entity's Brazilian
   tax id (``basis="cnpj"``), or whose folded full name equals a listed ENTITY name/alias
   (``basis="name"``, ≥2 significant tokens). Always worded "possible match — verify on the
   official list"; never "sanctioned".

**Individuals are counted, never stored by name** — same LGPD posture as the BCB PAS ingest
(bcb_supervision): Onça keeps no person's sanction record.

State in ``reference/sanctions.json`` (digests bucket). A list whose fetch fails KEEPS its
previous snapshot (``errors`` names it) — a failed download must never read as delistings.
New designations are the id diff against the previous snapshot; a list's first snapshot is a
baseline (``first_seen`` None), so the first run never floods the board.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import unicodedata
from typing import IO, Any, Callable, Iterator
from xml.etree import ElementTree as ET

STATE_KEY = "reference/sanctions.json"

LISTS: dict[str, dict[str, str]] = {
    "un": {"label": "ONU · Conselho de Segurança", "short": "ONU",
           "url": "https://scsanctions.un.org/resources/xml/en/consolidated.xml",
           "page": "https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list"},
    "ofac": {"label": "OFAC · SDN (EUA)", "short": "OFAC",
             "url": "https://www.treasury.gov/ofac/downloads/sdn.xml",
             "page": "https://sanctionssearch.ofac.treas.gov/"},
    "eu": {"label": "União Europeia · sanções financeiras", "short": "UE",
           "url": ("https://webgate.ec.europa.eu/fsd/fsf/public/files/"
                   "xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw"),
           "page": "https://www.sanctionsmap.eu/"},
    "uk": {"label": "Reino Unido · UK Sanctions List", "short": "UK",
           "url": "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml",
           "page": "https://www.gov.uk/government/publications/the-uk-sanctions-list"},
}
# Small first: a slow EU download never costs the UN list (the one binding in Brazil).
ORDER = ("un", "ofac", "uk", "eu")

_UA = {"User-Agent": "Mozilla/5.0 (compatible; OncaBot/1.0; +https://onssa.org)"}

# Who must screen against sanctions lists in Brazil (Lei 9.613 art. 9 obligated sectors) —
# an unbound Brazil-linked designation is relevant to all of them. Tenant scoping still
# applies (fail closed on no overlap).
AML_AUDIENCE = ["banking", "investment-banking", "insurance", "asset-management",
                "wealth-management", "private-markets", "fintech", "crypto", "consorcio",
                "betting", "real-estate-funds", "agri-funds", "acquiring", "closed-pension",
                "securitization"]

NEW_LOG_DAYS = 90

_BRAZIL = {"BRAZIL", "BRASIL", "BR", "FEDERATIVE REPUBLIC OF BRAZIL"}
_SUFFIX = {"SA", "S", "A", "LTDA", "LTD", "LIMITED", "EIRELI", "ME", "EPP", "INC", "LLC",
           "CORP", "CORPORATION", "CO", "COMPANY", "PLC", "GMBH", "AG", "THE", "DE", "DA",
           "DO", "DOS", "DAS", "E", "OF", "AND"}


# --- normalisation ------------------------------------------------------------------------
def _fold(s: Any) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    return re.sub(r"\s+", " ", "".join(c for c in s if not unicodedata.combining(c))).strip().upper()


def name_key(s: Any) -> str:
    """Matching key: folded, punctuation → space, legal suffixes / particles dropped. "" when
    fewer than 2 significant tokens or under 8 chars — too generic to match on safely."""
    toks = [t for t in re.split(r"[^A-Z0-9]+", _fold(s)) if t and t not in _SUFFIX]
    key = " ".join(toks)
    return key if len(toks) >= 2 and len(key) >= 8 else ""


def _digits(s: Any) -> str:
    return "".join(c for c in str(s or "") if c.isdigit())


def _is_brazil(*vals: Any) -> bool:
    return any(_fold(v) in _BRAZIL for v in vals if v)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _txt(e: ET.Element | None) -> str:
    return (e.text or "").strip() if e is not None and e.text else ""


def _rec(lst: str, rid: str, kind: str, name: str, aliases: list[str], programs: list[str],
         listed_on: str | None, brazil: bool, cnpjs: list[str]) -> dict[str, Any]:
    seen: set[str] = set()
    al = []
    for a in aliases:
        a = re.sub(r"\s+", " ", a).strip()
        if a and a.upper() != name.upper() and a.upper() not in seen:
            seen.add(a.upper())
            al.append(a)
    return {"list": lst, "id": f"{lst}:{rid}", "kind": kind, "name": name, "aliases": al[:12],
            "programs": [p for p in dict.fromkeys(programs) if p][:6],
            "listed_on": listed_on or None, "brazil": brazil,
            "cnpj_roots": sorted({c[:8] for c in cnpjs if len(c) == 14})}


# --- parsers (streaming; each yields normalised records) ----------------------------------
def parse_un(fp: IO[bytes]) -> Iterator[dict[str, Any]]:
    for _ev, el in ET.iterparse(fp, events=("end",)):
        tag = _local(el.tag)
        if tag not in ("INDIVIDUAL", "ENTITY"):
            continue
        parts = [_txt(el.find(t)) for t in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")]
        name = " ".join(p for p in parts if p)
        rid = _txt(el.find("REFERENCE_NUMBER")) or _txt(el.find("DATAID"))
        if not name or not rid:
            el.clear()
            continue
        aliases = [_txt(a.find("ALIAS_NAME")) for a in el.iter() if _local(a.tag).endswith("_ALIAS")]
        countries = [_txt(c) for c in el.iter() if _local(c.tag) in ("COUNTRY", "COUNTRY_OF_BIRTH")]
        countries += [_txt(v) for n in el.findall("NATIONALITY") for v in n.findall("VALUE")]
        yield _rec("un", rid, "individual" if tag == "INDIVIDUAL" else "entity", name, aliases,
                   [_txt(el.find("UN_LIST_TYPE"))], _txt(el.find("LISTED_ON"))[:10],
                   _is_brazil(*countries), [])
        el.clear()


def parse_ofac(fp: IO[bytes]) -> Iterator[dict[str, Any]]:
    for _ev, el in ET.iterparse(fp, events=("end",)):
        if _local(el.tag) != "sdnEntry":
            continue
        f = {_local(c.tag): c for c in el}
        uid = _txt(f.get("uid"))
        name = " ".join(p for p in (_txt(f.get("firstName")), _txt(f.get("lastName"))) if p)
        if not uid or not name:
            el.clear()
            continue
        kind = {"Entity": "entity", "Individual": "individual", "Vessel": "vessel",
                "Aircraft": "aircraft"}.get(_txt(f.get("sdnType")), "entity")
        aliases = []
        for aka in el.iter():
            if _local(aka.tag) == "aka":
                a = {_local(c.tag): _txt(c) for c in aka}
                aliases.append(" ".join(p for p in (a.get("firstName"), a.get("lastName")) if p))
        countries, cnpjs = [], []
        for c in el.iter():
            t = _local(c.tag)
            if t in ("country", "placeOfBirth", "vesselFlag"):
                countries.append(_txt(c))
            elif t == "id":
                d = {_local(x.tag): _txt(x) for x in c}
                if _is_brazil(d.get("idCountry")):
                    countries.append("Brazil")
                    if "TAX ID" in _fold(d.get("idType")) or "CNPJ" in _fold(d.get("idType")):
                        cnpjs.append(_digits(d.get("idNumber")))
        countries += [p.rsplit(",", 1)[-1].strip() for p in countries if "," in p]
        programs = [_txt(p) for p in el.iter() if _local(p.tag) == "program"]
        yield _rec("ofac", uid, kind, name, aliases, programs, None, _is_brazil(*countries), cnpjs)
        el.clear()


def parse_eu(fp: IO[bytes]) -> Iterator[dict[str, Any]]:
    for _ev, el in ET.iterparse(fp, events=("end",)):
        if _local(el.tag) != "sanctionEntity":
            continue
        rid = el.get("euReferenceNumber") or el.get("logicalId")
        st = next((c for c in el if _local(c.tag) == "subjectType"), None)
        kind = {"person": "individual", "enterprise": "entity"}.get(
            st.get("code") if st is not None else "", "entity")
        names = [c.get("wholeName") or "" for c in el if _local(c.tag) == "nameAlias"]
        names = [n for n in names if n.strip()]
        regs = [c for c in el if _local(c.tag) == "regulation"]
        if not rid or not names:
            el.clear()
            continue
        brazil = any((c.get("countryIso2Code") or "").upper() == "BR"
                     for c in el if _local(c.tag) in ("address", "citizenship", "birthdate", "identification"))
        cnpjs = [_digits(c.get("number")) for c in el if _local(c.tag) == "identification"
                 and (c.get("countryIso2Code") or "").upper() == "BR"]
        listed = min((r.get("publicationDate") or "" for r in regs), default="") or None
        yield _rec("eu", rid, kind, names[0].strip(), names[1:],
                   [r.get("programme") or "" for r in regs], listed, brazil, cnpjs)
        el.clear()


def _uk_date(s: str) -> str | None:
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def parse_uk(fp: IO[bytes]) -> Iterator[dict[str, Any]]:
    for _ev, el in ET.iterparse(fp, events=("end",)):
        if _local(el.tag) != "Designation":
            continue
        rid = _txt(el.find("UniqueID"))
        primary, aliases = "", []
        for n in el.iter("Name"):
            parts = [_txt(n.find(f"Name{i}")) for i in range(1, 7)]
            full = " ".join(p for p in parts if p)
            if not full:
                continue
            if _txt(n.find("NameType")).lower().startswith("primary") and not primary:
                primary = full
            else:
                aliases.append(full)
        name = primary or (aliases.pop(0) if aliases else "")
        if not rid or not name:
            el.clear()
            continue
        kind = {"Individual": "individual", "Entity": "entity", "Ship": "vessel"}.get(
            _txt(el.find("IndividualEntityShip")), "entity")
        countries = [_txt(c) for c in el.iter() if c.tag in ("AddressCountry", "Nationality",
                                                               "CountryOfBirth", "Location")]
        yield _rec("uk", rid, kind, name, aliases, [_txt(el.find("RegimeName"))],
                   _uk_date(_txt(el.find("DateDesignated"))), _is_brazil(*countries), [])
        el.clear()


PARSERS: dict[str, Callable[[IO[bytes]], Iterator[dict[str, Any]]]] = {
    "un": parse_un, "ofac": parse_ofac, "eu": parse_eu, "uk": parse_uk}


# --- snapshot + matching (pure) -----------------------------------------------------------
def snapshot(lst: str, records: Any, *, as_of: str) -> dict[str, Any]:
    """One list's compact snapshot: every id (for the diff), counts by kind, and the full record
    only for Brazil-linked ENTITIES. Individuals are counted, never kept by name (LGPD)."""
    ids: list[str] = []
    kinds: dict[str, int] = {}
    brazil_entities: list[dict[str, Any]] = []
    brazil_individuals = 0
    entity_index: list[dict[str, Any]] = []   # entities only, for roster matching
    for r in records:
        ids.append(r["id"])
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        if r["kind"] == "individual":
            brazil_individuals += 1 if r["brazil"] else 0
            continue
        if r["kind"] == "entity":
            entity_index.append({"id": r["id"], "name": r["name"], "aliases": r["aliases"],
                                 "cnpj_roots": r["cnpj_roots"]})
        if r["brazil"]:
            brazil_entities.append(r)
    return {"as_of": as_of, "total": len(ids), "kinds": kinds, "ids": sorted(set(ids)),
            "brazil_entities": brazil_entities, "brazil_individuals": brazil_individuals,
            "_entity_index": entity_index}


def roster_matches(entity_index: list[dict[str, Any]], records: list[dict[str, Any]], *,
                   cnpj_to_entity: dict[str, str], name_to_entity: dict[str, str]
                   ) -> list[dict[str, Any]]:
    """Listed entities that coincide with a tracked roster entity: CNPJ root first (strong),
    then exact name key (possible). One row per (listed id, roster entity)."""
    by_id = {r["id"]: r for r in records}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for e in entity_index:
        hit: tuple[str, str] | None = None
        for root in e.get("cnpj_roots") or []:
            if root in cnpj_to_entity:
                hit = (cnpj_to_entity[root], "cnpj")
                break
        if hit is None:
            for n in [e["name"], *(e.get("aliases") or [])]:
                k = name_key(n)
                if k and k in name_to_entity:
                    hit = (name_to_entity[k], "name")
                    break
        if hit:
            src = by_id.get(e["id"]) or e
            out[(e["id"], hit[0])] = {
                "list": e["id"].split(":", 1)[0], "id": e["id"], "name": e["name"],
                "programs": src.get("programs") or [], "listed_on": src.get("listed_on"),
                "entity": hit[0], "basis": hit[1]}
    return sorted(out.values(), key=lambda r: (r["basis"] != "cnpj", r["id"]))


# --- fetch + state ------------------------------------------------------------------------
def _fetch(lst: str, *, get: Callable[..., Any] | None = None) -> list[dict[str, Any]]:
    import requests

    get = get or requests.get
    resp = get(LISTS[lst]["url"], headers=_UA, timeout=(15, 120), stream=True)
    resp.raise_for_status()
    raw = getattr(resp, "raw", None)
    if raw is not None and hasattr(raw, "decode_content"):
        raw.decode_content = True
    else:  # test doubles / non-streaming clients
        import io

        raw = io.BytesIO(resp.content)
    records = list(PARSERS[lst](raw))
    if not records:
        raise ValueError(f"{lst}: parsed 0 records (format change?)")
    return records


def update_list(prev: dict[str, Any] | None, lst: str, records: list[dict[str, Any]], *,
                today: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fold one fresh list into its previous snapshot → (snapshot, diff counts)."""
    snap = snapshot(lst, records, as_of=today)
    prev_ids = set((prev or {}).get("ids") or [])
    baseline = not prev_ids
    new_ids = set() if baseline else set(snap["ids"]) - prev_ids
    removed = 0 if baseline else len(prev_ids - set(snap["ids"]))
    first_seen_prev = {r["id"]: r.get("first_seen") for r in (prev or {}).get("brazil_entities") or []}
    for r in snap["brazil_entities"]:
        r["first_seen"] = today if r["id"] in new_ids else first_seen_prev.get(r["id"])
    kinds_new: dict[str, int] = {}
    by_id = {r["id"]: r for r in records}
    for i in new_ids:
        k = by_id[i]["kind"]
        kinds_new[k] = kinds_new.get(k, 0) + 1
    return snap, {"date": today, "list": lst, "new": len(new_ids), "removed": removed,
                  "new_by_kind": kinds_new, "baseline": baseline,
                  "new_brazil": sum(1 for i in new_ids if by_id[i]["brazil"])}


def update_state(bucket: str, *, cnpj_to_entity: dict[str, str] | None = None,
                 name_to_entity: dict[str, str] | None = None, s3: Any | None = None,
                 get: Callable[..., Any] | None = None, lists: tuple[str, ...] = ORDER,
                 budget: Callable[[str], Any] | None = None,
                 today: dt.date | None = None) -> dict[str, Any]:
    """Fetch each list, diff against the stored snapshot, rebuild roster matches and persist
    after EVERY list (a budget kill mid-run keeps what already landed). ``budget(label)`` is
    an optional context-manager factory bounding each list (lambda_port._source_budget)."""
    import contextlib

    if s3 is None:
        import boto3

        s3 = boto3.client("s3")
    try:
        prev = json.loads(s3.get_object(Bucket=bucket, Key=STATE_KEY)["Body"].read())
    except Exception:  # noqa: BLE001 - first run / unreadable → fresh
        prev = {}
    day = (today or dt.date.today()).isoformat()
    doc: dict[str, Any] = {"lists": dict(prev.get("lists") or {}), "errors": {},
                           "new_log": list(prev.get("new_log") or []),
                           "matches": list(prev.get("matches") or []),
                           "entity_map": "fresh"}
    cnpj_to_entity = dict(cnpj_to_entity or {})
    name_to_entity = {nk: v for k, v in (name_to_entity or {}).items() if (nk := name_key(k))}
    if not cnpj_to_entity and not name_to_entity:
        doc["entity_map"] = "carried"    # registry unavailable → keep the previous matches
    stats: dict[str, Any] = {}
    for lst in lists:
        ctx = budget(f"Sanções {LISTS[lst]['short']}") if budget else contextlib.nullcontext()
        try:
            with ctx:
                records = _fetch(lst, get=get)
                snap, diff = update_list(doc["lists"].get(lst), lst, records, today=day)
                index = snap.pop("_entity_index")
                if doc["entity_map"] == "fresh":
                    doc["matches"] = [m for m in doc["matches"] if m.get("list") != lst] + \
                        roster_matches(index, records, cnpj_to_entity=cnpj_to_entity,
                                       name_to_entity=name_to_entity)
                doc["lists"][lst] = snap
                doc["new_log"].append(diff)
                stats[lst] = {"total": snap["total"], "new": diff["new"],
                              "brazil_entities": len(snap["brazil_entities"])}
        except Exception as exc:  # noqa: BLE001 - keep the previous snapshot
            doc["errors"][lst] = str(exc)[:300]
            stats[lst] = {"error": str(exc)[:120]}
        cut = ((today or dt.date.today()) - dt.timedelta(days=NEW_LOG_DAYS)).isoformat()
        doc["new_log"] = [d for d in doc["new_log"] if str(d.get("date") or "") >= cut]
        doc["as_of"] = day
        s3.put_object(Bucket=bucket, Key=STATE_KEY, ContentType="application/json",
                      Body=json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    return {"lists": stats, "matches": len(doc["matches"]), "entity_map": doc["entity_map"],
            "errors": sorted(doc["errors"])}


# --- feed projection + tenant scoping (pure) ----------------------------------------------
def for_feed(doc: dict[str, Any] | None, *, entity_attrs: dict[str, Any] | None = None,
             today: dt.date | None = None) -> dict[str, Any]:
    """The feed-facing ``sanctions`` block. Ids are dropped (diff state only); ``errors`` stays
    out (operator logs). Brazil-linked rows bound to a roster entity take its label/industries;
    unbound ones are relevant to every AML-obligated sector."""
    if not doc:
        return {}
    attrs = entity_attrs or {}
    today = today or dt.date.today()
    cut30 = (today - dt.timedelta(days=30)).isoformat()
    bound: dict[str, str] = {m["id"]: m["entity"] for m in doc.get("matches") or []
                             if m.get("entity") in attrs}

    def label(eid: str) -> str:
        return (attrs.get(eid) or {}).get("label") or eid

    def inds(eid: str | None) -> list[str]:
        return list((attrs.get(eid) or {}).get("industries") or []) if eid else list(AML_AUDIENCE)

    lists_out, brazil, ind_counts = [], [], {}
    for lst in ORDER:
        s = (doc.get("lists") or {}).get(lst)
        if not s:
            continue
        log = [d for d in doc.get("new_log") or [] if d.get("list") == lst
               and str(d.get("date") or "") >= cut30 and not d.get("baseline")]
        lists_out.append({"list": lst, "label": LISTS[lst]["label"], "short": LISTS[lst]["short"],
                          "as_of": s.get("as_of"), "total": s.get("total"),
                          "entities": (s.get("kinds") or {}).get("entity", 0),
                          "individuals": (s.get("kinds") or {}).get("individual", 0),
                          "new_30d": sum(int(d.get("new") or 0) for d in log),
                          "binding_in_brazil": lst == "un", "url": LISTS[lst]["page"]})
        ind_counts[lst] = int(s.get("brazil_individuals") or 0)
        for r in s.get("brazil_entities") or []:
            eid = bound.get(r["id"])
            brazil.append({"id": r["id"], "list": lst, "list_short": LISTS[lst]["short"],
                           "name": r["name"], "aliases": (r.get("aliases") or [])[:4],
                           "programs": r.get("programs") or [], "listed_on": r.get("listed_on"),
                           "first_seen": r.get("first_seen"), "entity": eid,
                           "label": label(eid) if eid else r["name"],
                           "industries": inds(eid), "url": LISTS[lst]["page"]})
    brazil.sort(key=lambda r: (str(r.get("first_seen") or ""), str(r.get("listed_on") or "")),
                reverse=True)
    matches = [{**m, "label": label(m["entity"]), "industries": inds(m["entity"]),
                "list_short": LISTS.get(m["list"], {}).get("short", m["list"]),
                "url": LISTS.get(m["list"], {}).get("page")}
               for m in doc.get("matches") or [] if m.get("entity") in attrs]
    return {"as_of": doc.get("as_of"), "lists": lists_out, "brazil_entities": brazil,
            "brazil_individuals": ind_counts, "matches": matches}


def scope(block: dict[str, Any] | None, industries: Any,
          row_ok: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    """Tenant scoping (#193 rule): an entity-bound row follows its entity; an unbound one needs
    a licensed industry. Fail closed. List totals are public context and stay."""
    if not block:
        return {}
    keep = {str(i).strip().lower() for i in (industries or [])}

    def ok(r: dict[str, Any]) -> bool:
        if r.get("entity"):
            return bool(row_ok(r))
        return bool({str(i).lower() for i in r.get("industries") or []} & keep)

    out = dict(block)
    for key in ("brazil_entities", "matches"):
        out[key] = [r for r in block.get(key) or [] if ok(r)]
    if not ({i for i in AML_AUDIENCE} & keep):
        out["brazil_individuals"] = {}
    return out
