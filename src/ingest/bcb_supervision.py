"""BCB supervision sources for the CRO board (#378 part 2) — all official Banco Central data.

Three public BCB sources, none of them in the normativos search index:

* **PAS register** — first/second-instance decisions of Processos Administrativos
  Sancionadores, Olinda open-data OData (``Gepad_QuadroPenalidades``), updated daily.
  Only LEGAL ENTITIES are kept: rows about individuals (CPF, masked by the BCB) are dropped
  at ingest (LGPD — Onça never stores a person's sanction record). Of the companies, only
  supervisory decisions reach the board: the COPAS committee, or a BCB-supervised
  institution. The DERAD/GEPAG capital-register fines on non-financial firms (most of the
  register) are noise for a bank CRO.
* **Pix data-incident register** — "Registro de incidentes com dados pessoais" (BCB LGPD
  page), DICT rows only: institution, incident period, number of Pix keys affected.
* **Public consultations** — the BCB "Sistema Consultas Públicas" (www3 audpub, a stateful
  server-rendered app, no JSON): the ACTIVE list carries the comment deadline. Two requests.

BCB terms (Direitos Autorais): reproduction permitted with the information kept intact and
the source cited — every row carries ``source`` and a BCB ``url``.

Written once per structured-ingest run to ``reference/bcb_supervision.json`` (digests
bucket). A section whose fetch fails KEEPS its previous rows (``errors`` says which).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
import unicodedata
from typing import Any, Callable

STATE_KEY = "reference/bcb_supervision.json"
SOURCE = "Banco Central do Brasil"

PAS_URL = ("https://olinda.bcb.gov.br/olinda/servico/Gepad_QuadroPenalidades/versao/v1/odata/"
           "QuadroGeralProcessoAdministrativoSancionador")
PAS_PAGE = "https://www.bcb.gov.br/estabilidadefinanceira/processossfn"
PIX_URL = "https://www.bcb.gov.br/api/servico/sitebcb/registro_de_incidentes_com_dados_pessoais"
PIX_PAGE = "https://www.bcb.gov.br/acessoinformacao/lgpd"
AUDPUB = "https://www3.bcb.gov.br/audpub/"
CONSULTAS_PAGE = "https://www.bcb.gov.br/estabilidadefinanceira/consultaspublicas"

PAS_LOOKBACK_DAYS = 365
_UA = {"User-Agent": "Mozilla/5.0 (compatible; OncaBot/1.0; +https://onssa.org)",
       "Accept": "application/json, text/html;q=0.9"}

_MONTHS = {"janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
           "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12}


def _fold(s: Any) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    return re.sub(r"\s+", " ", "".join(c for c in s if not unicodedata.combining(c))).strip().upper()


def _digits(s: Any) -> str:
    return "".join(c for c in str(s or "") if c.isdigit())


def _clean(s: Any) -> str:
    """HTML → text, invisible characters (zero-width spaces the BCB lists carry) removed."""
    t = html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))
    t = re.sub(r"[​‌‍﻿\xa0]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# --- industry inference for institutions that are not tracked entities --------------------
# Order matters: the first match wins. Every row the BCB supervises lands in at least one
# licensed module, so tenant scoping (fail closed on no industry) keeps it.
_IND_RULES: list[tuple[str, list[str]]] = [
    (r"ADMINISTRADORA DE CONSORCIO|\bCONSORCIOS?\b", ["consorcio"]),
    (r"COOPERATIVA DE CREDITO|\bCOOPERATIVA\b|\bSICOOB\b|\bSICREDI\b|\bCRESOL\b", ["banking"]),
    (r"INSTITUICAO DE PAGAMENTO|\bPAGAMENTOS?\b|\bPAY\b", ["acquiring", "fintech"]),
    (r"SOCIEDADE DE CREDITO DIRETO|\bSCD\b|SOCIEDADE DE EMPRESTIMO ENTRE PESSOAS|\bSEP\b",
     ["fintech"]),
    (r"CORRETORA DE CAMBIO|CORRETORA DE TITULOS|DISTRIBUIDORA DE TITULOS|\bDTVM\b|\bCCTVM\b",
     ["investment-banking"]),
    (r"\bBANCO\b|\bFINANCEIRA\b|CREDITO,? FINANCIAMENTO|SOCIEDADE DE CREDITO|ARRENDAMENTO MERCANTIL"
     r"|\bLEASING\b|MICROEMPREENDEDOR", ["banking"]),
]
_NOT_SFN = re.compile(r"\bFUNDO\b|\bETF\b|\bCLASSE\b|RESSEGURO|SEGURADORA|PREVIDENCIA")


def infer_industries(name: Any, default: list[str] | None = None) -> list[str]:
    n = _fold(name)
    for pat, inds in _IND_RULES:
        if re.search(pat, n):
            return list(inds)
    return list(default or [])


def is_sfn_institution(name: Any) -> bool:
    """A BCB-supervised institution by its legal name (bank, cooperative, IP, SCD, DTVM…)."""
    n = _fold(name)
    return bool(infer_industries(n)) and not _NOT_SFN.search(n)


# --- PAS register ------------------------------------------------------------------------
def pas_url(since: str, top: int = 5000) -> str:
    # Olinda rejects '+'-encoded spaces ("not-allowed value 'x+desc'") and quoted Edm.Date
    # literals, so the query string is built by hand with %20 and a bare date.
    return (f"{PAS_URL}?$filter=Data_da_decisao_1_instancia%20ge%20{since}"
            f"&$orderby=Data_da_decisao_1_instancia%20desc&$top={int(top)}&$format=json")


def committee(decision_no: Any) -> str | None:
    m = re.match(r"\s*([A-Z]+)", str(decision_no or ""))
    return m.group(1) if m else None


def normalize_pas(row: dict[str, Any]) -> dict[str, Any] | None:
    """One register row → a board row, or None (individual, or not supervisory)."""
    doc = _digits(row.get("CPF_CNPJ"))
    if len(doc) != 14 or "*" in str(row.get("CPF_CNPJ") or ""):
        return None                                   # LGPD: never keep a person's record
    name = _clean(row.get("Nome"))
    com = committee(row.get("Numero_decisao_1_instancia"))
    if not (com == "COPAS" or is_sfn_institution(name)):
        return None
    if _NOT_SFN.search(_fold(name)) and com != "COPAS":
        return None
    pas = str(row.get("PAS") or "").strip()
    d1 = str(row.get("Data_da_decisao_1_instancia") or "")[:10] or None
    if not pas or not d1:
        return None
    fine = row.get("Valor_da_multa_1_instancia")
    return {
        "id": f"bcb-pas:{pas}:{_digits(row.get('Numero_decisao_1_instancia'))}",
        "pas": pas, "name": name, "cnpj_root": doc[:8],
        "committee": com, "decision_no": _clean(row.get("Numero_decisao_1_instancia")),
        "date": d1, "cited": str(row.get("Data_da_citacao") or "")[:10] or None,
        "penalty": _clean(row.get("Tipo_penalidade_1_instancia")) or None,
        "fine": float(fine) if isinstance(fine, (int, float)) else None,
        "duration": _clean(row.get("Duracao_da_pena_1_instancia")) or None,
        "appealed": _fold(row.get("Apresentou_recurso")) == "SIM",
        "decision2_date": str(row.get("Data_da_decisao_2_instancia") or "")[:10] or None,
        "penalty2": _clean(row.get("Tipo_penalidade_2_instancia")) or None,
        "status": _clean(row.get("Situacao")) or None,
        # a COPAS target whose legal name says nothing (e.g. "CREDZ LTDA") is still a
        # BCB-supervised institution: show it to every BCB-supervised module
        "industries": infer_industries(name, ["banking", "fintech", "acquiring"]),
        "source": SOURCE, "url": PAS_PAGE,
    }


def fetch_pas(get: Callable[[str], Any], *, today: dt.date) -> list[dict[str, Any]]:
    since = (today - dt.timedelta(days=PAS_LOOKBACK_DAYS)).isoformat()
    payload = get(pas_url(since))
    rows = [r for r in (normalize_pas(x) for x in (payload or {}).get("value") or []) if r]
    rows.sort(key=lambda r: (r["date"], r["id"]), reverse=True)
    return rows


# --- Pix data-incident register -----------------------------------------------------------
_DATE = r"(\d{1,2})(?:º|o)?\s+de\s+([a-zç]+)(?:\s+de\s+(\d{4}))?"


def parse_period(text: Any) -> tuple[str | None, str | None]:
    """'26 a 28 de abril de 2026' / '30 de agosto de 2025 a 27 de fevereiro de 2026' /
    '15 de março de 2024' → (start ISO, end ISO). Missing parts borrow from the end date."""
    t = _clean(text).lower()
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    parts = re.split(r"\s+a\s+", t, maxsplit=1)
    end_m = re.search(_DATE, parts[-1])
    if not end_m or end_m.group(2) not in _MONTHS or not end_m.group(3):
        return None, None
    ey, em, ed = int(end_m.group(3)), _MONTHS[end_m.group(2)], int(end_m.group(1))
    try:
        end = dt.date(ey, em, ed)
    except ValueError:
        return None, None
    start = end
    if len(parts) == 2:
        sm = re.search(_DATE, parts[0])
        if sm:
            smon = _MONTHS.get(sm.group(2), em) if sm.group(2) in _MONTHS else em
            sy = int(sm.group(3)) if sm.group(3) else ey
            try:
                start = dt.date(sy, smon, int(sm.group(1)))
            except ValueError:
                start = end
        else:
            d0 = re.match(r"\s*(\d{1,2})\b", parts[0])
            if d0:
                try:
                    start = dt.date(ey, em, int(d0.group(1)))
                except ValueError:
                    start = end
    return start.isoformat(), end.isoformat()


def parse_keys(text: Any) -> int | None:
    m = re.search(r"(\d{1,3}(?:\.\d{3})+|\d+)\s+chaves", _clean(text), re.I)
    return int(m.group(1).replace(".", "")) if m else None


def normalize_pix(row: dict[str, Any]) -> dict[str, Any] | None:
    if _fold(_clean(row.get("BancoDados"))) != "DICT":
        return None
    inst = _clean(row.get("Instituicao_incidente"))
    period = _clean(row.get("Periodo_incidente"))
    start, end = parse_period(period)
    if not inst or not end:
        return None
    return {
        "id": "bcb-pix:" + hashlib.sha1(f"{_fold(inst)}|{period}".encode()).hexdigest()[:12],
        "name": inst, "period": period, "start": start, "date": end,
        "keys": parse_keys(row.get("NaturezaDados")),
        # a Pix-data incident touches the Pix ecosystem, not one module
        "industries": infer_industries(inst, ["banking", "fintech", "acquiring"]),
        "source": SOURCE, "url": PIX_PAGE,
    }


def fetch_pix(get: Callable[[str], Any]) -> list[dict[str, Any]]:
    rows = [r for r in (normalize_pix(x) for x in (get(PIX_URL) or {}).get("conteudo") or []) if r]
    rows.sort(key=lambda r: (r["date"], r["id"]), reverse=True)
    return rows


# --- public consultations (active list) ---------------------------------------------------
def parse_consultas(page_html: str, *, today: dt.date) -> list[dict[str, Any]]:
    """The audpub 'Consultas e outras participações ativas' page → [{number, deadline, subject}]."""
    t = _clean(re.sub(r"<(?:script|style)[^>]*>.*?</(?:script|style)>", " ", page_html or "",
                      flags=re.S | re.I))
    out: list[dict[str, Any]] = []
    # The number is anchored to the "Detalhar consulta" link that follows it — the page header
    # carries today's date ("10/11/2026"), whose tail "11/2026" looks like a number too.
    nxt = r"(?<![\d/])\d{1,4}/\d{4}[\s|]+Detalhar consulta"
    for m in re.finditer(r"(?<![\d/])(\d{1,4}/\d{4})[\s|]+Detalhar consulta[\s|]+Data final da consulta:[\s|]*"
                         r"(\d{2})/(\d{2})/(\d{4})\s*(.*?)(?=" + nxt + r"|$)", t, flags=re.S):
        try:
            deadline = dt.date(int(m.group(4)), int(m.group(3)), int(m.group(2)))
        except ValueError:
            continue
        subject = re.sub(r"\s+", " ", m.group(5)).strip()
        cut = re.search(r"(?<=[.;])\s", subject)          # the summary is one sentence
        subject = (subject[:cut.start()] if cut else subject)[:400]
        number = m.group(1)
        out.append({
            "id": f"bcb-cp:{number}", "number": number, "deadline": deadline.isoformat(),
            "days_to_deadline": (deadline - today).days, "subject": subject,
            "industries": infer_industries(subject, ["banking", "fintech"]),
            "source": SOURCE, "url": CONSULTAS_PAGE,
        })
    return out


def fetch_consultas(*, today: dt.date, session: Any = None) -> list[dict[str, Any]]:
    import requests

    s = session or requests.Session()
    s.headers.update(_UA)
    home = s.get(AUDPUB + "HomePage", timeout=20)
    home.raise_for_status()
    link = re.search(r'href="([^"]*linkAudienciasAtivas[^"]*)"', home.text)
    if not link:
        raise RuntimeError("audpub: active-consultations link not found")
    page = s.get(requests.compat.urljoin(home.url, html.unescape(link.group(1))), timeout=20)
    page.raise_for_status()
    return parse_consultas(page.text, today=today)


# --- state --------------------------------------------------------------------------------
def _http_get_json(url: str) -> Any:
    import requests

    last: Exception | None = None
    for _ in range(2):                                 # Olinda answers 502 now and then
        try:
            r = requests.get(url, timeout=20, headers=_UA)
            r.raise_for_status()
            txt = r.text.strip()
            if txt.startswith("/*"):                   # Olinda wraps errors in a comment
                raise RuntimeError(txt[:200])
            return json.loads(txt)
        except Exception as exc:  # noqa: BLE001 - retried, then raised
            last = exc
    raise RuntimeError(f"GET failed: {url[:80]}… ({last})")


_SUFFIX = re.compile(r"\b(?:S\s*/?\s*A|SOCIEDADE ANONIMA|LTDA|EIRELI|ME|EPP)\b\.?$")


def name_key(name: Any) -> str:
    """A conservative join key for registers that carry no CNPJ (the Pix register): the folded
    name before any ' - ' qualifier, punctuation and corporate suffixes dropped, and a leading
    'BANCO' removed — "Banco Exemplo S.A." and "Exemplo" meet; nothing fuzzier than that."""
    n = re.split(r"\s[-–—]\s", _fold(name), maxsplit=1)[0]
    n = re.sub(r"[.,;:()]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    for _ in range(2):
        n = _SUFFIX.sub("", n).strip()
    n = re.sub(r"^BANCO\s+", "", n)
    return n if len(n) >= 3 else ""


def match_entities(rows: list[dict[str, Any]], cnpj_to_entity: dict[str, str] | None,
                   name_to_entity: dict[str, str] | None = None) -> None:
    """Bind a row to a tracked entity: CNPJ root first, then the exact folded legal name, then
    the conservative ``name_key`` (an ambiguous key — two entities — never binds)."""
    names = name_to_entity or {}
    keyed: dict[str, str | None] = {}
    for k, eid in names.items():
        nk = name_key(k)
        if nk:
            keyed[nk] = eid if keyed.get(nk, eid) == eid else None
    for r in rows:
        # the BCB Pix register closes a name with its short form: "Banco X S.A. (X)"
        paren = re.search(r"\(([^()]{2,40})\)\s*$", str(r.get("name") or ""))
        alias = paren.group(1) if paren else None
        ent = (cnpj_to_entity or {}).get(r.get("cnpj_root") or "") \
            or names.get(_fold(r.get("name"))) or keyed.get(name_key(r.get("name"))) \
            or (alias and (names.get(_fold(alias)) or keyed.get(name_key(alias))))
        if ent:
            r["entity"] = ent


def carried_bindings(prev: dict[str, Any] | None) -> tuple[dict[str, str], dict[str, str]]:
    """(cnpj_root -> entity, folded name -> entity) from a previous state doc's bound rows — the
    fallback when this run's registry scan is unavailable."""
    cnpj: dict[str, str] = {}
    names: dict[str, str] = {}
    for key in ("pas", "pix_incidents"):
        for r in (prev or {}).get(key) or []:
            eid = r.get("entity")
            if not eid:
                continue
            if r.get("cnpj_root"):
                cnpj.setdefault(str(r["cnpj_root"])[:8], eid)
            if r.get("name"):
                names.setdefault(_fold(r["name"]), eid)
    return cnpj, names


def update_state(bucket: str, *, s3: Any = None, get: Callable[[str], Any] | None = None,
                 consultas_fn: Callable[..., list[dict[str, Any]]] | None = None,
                 cnpj_to_entity: dict[str, str] | None = None,
                 name_to_entity: dict[str, str] | None = None,
                 today: dt.date | None = None) -> dict[str, Any]:
    """Fetch the three sources and write ``STATE_KEY``. A failed section keeps its previous
    rows. Returns a one-line summary dict for the log."""
    import boto3

    s3 = s3 or boto3.client("s3")
    get = get or _http_get_json
    today = today or dt.date.today()
    try:
        prev = json.loads(s3.get_object(Bucket=bucket, Key=STATE_KEY)["Body"].read())
    except Exception:
        prev = {}
    doc: dict[str, Any] = {"as_of": today.isoformat(), "source": SOURCE, "errors": {}}
    if not cnpj_to_entity and not name_to_entity:
        # registry scan failed or timed out: reuse the previous run's bindings, never unbind
        cnpj_to_entity, name_to_entity = carried_bindings(prev)
        if cnpj_to_entity or name_to_entity:
            doc["entity_map"] = "carried"
    for key, fn in (("pas", lambda: fetch_pas(get, today=today)),
                    ("pix_incidents", lambda: fetch_pix(get)),
                    ("consultas", lambda: (consultas_fn or fetch_consultas)(today=today))):
        try:
            rows = fn()
            if key != "consultas":
                match_entities(rows, cnpj_to_entity, name_to_entity)
            doc[key] = rows
            doc[f"{key}_as_of"] = today.isoformat()
        except Exception as exc:  # noqa: BLE001 - a dead source keeps yesterday's rows
            doc["errors"][key] = str(exc)[:300]
            doc[key] = prev.get(key) or []
            doc[f"{key}_as_of"] = prev.get(f"{key}_as_of")
    s3.put_object(Bucket=bucket, Key=STATE_KEY, ContentType="application/json",
                  Body=json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    return {"pas": len(doc["pas"]), "pix_incidents": len(doc["pix_incidents"]),
            "consultas": len(doc["consultas"]),
            "matched": sum(1 for k in ("pas", "pix_incidents") for r in doc[k] if r.get("entity")),
            "errors": sorted(doc["errors"])}


# --- feed projection + tenant scoping (pure) ----------------------------------------------
PAS_FEED_DAYS = 180
PIX_FEED_DAYS = 730


def for_feed(doc: dict[str, Any] | None, *, entity_attrs: dict[str, Any] | None = None,
             today: dt.date | None = None) -> dict[str, Any]:
    """The feed-facing ``bcb_supervision`` block. A row bound to an entity of THIS feed's roster
    takes that entity's label and industries; an unbound row keeps its inferred industries.
    Consultations recount ``days_to_deadline`` and drop once closed. ``errors`` stays out
    (operator logs only)."""
    today = today or dt.date.today()
    attrs = entity_attrs or {}

    def bind(r: dict[str, Any]) -> dict[str, Any]:
        r = dict(r)
        ea = attrs.get(r.get("entity") or "")
        if ea is None:
            r.pop("entity", None)
        else:
            r["label"] = ea.get("label") or r.get("name")
            r["industries"] = list(ea.get("industries") or r.get("industries") or [])
        r.pop("cnpj_root", None)        # join key only; the CNPJ is public but not a UI field
        return r

    if not doc:
        return {}
    pas_cut = (today - dt.timedelta(days=PAS_FEED_DAYS)).isoformat()
    pix_cut = (today - dt.timedelta(days=PIX_FEED_DAYS)).isoformat()
    consultas = []
    for c in doc.get("consultas") or []:
        try:
            left = (dt.date.fromisoformat(c["deadline"]) - today).days
        except (KeyError, ValueError):
            continue
        if left >= 0:
            consultas.append({**c, "days_to_deadline": left})
    consultas.sort(key=lambda c: c["days_to_deadline"])
    return {
        "as_of": doc.get("as_of"), "source": SOURCE,
        "pas_as_of": doc.get("pas_as_of"), "pix_as_of": doc.get("pix_incidents_as_of"),
        "consultas_as_of": doc.get("consultas_as_of"),
        "pas": [bind(r) for r in doc.get("pas") or [] if str(r.get("date") or "") >= pas_cut],
        "pix_incidents": [bind(r) for r in doc.get("pix_incidents") or []
                          if str(r.get("date") or "") >= pix_cut],
        "consultas": consultas,
    }


def scope(block: dict[str, Any] | None, industries: Any,
          row_ok: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    """Tenant / entry scoping, same rule as the #193 enforcement register: an entity-bound row
    follows its entity (``row_ok``); an unbound one needs a licensed industry. Fail closed —
    an unbound row with no industry is dropped."""
    if not block:
        return {}
    keep = {str(i).strip().lower() for i in (industries or [])}

    def ok(r: dict[str, Any]) -> bool:
        if r.get("entity"):
            return bool(row_ok(r))
        return bool({str(i).lower() for i in r.get("industries") or []} & keep)

    out = dict(block)
    for key in ("pas", "pix_incidents", "consultas"):
        out[key] = [r for r in block.get(key) or [] if ok(r)]
    return out
