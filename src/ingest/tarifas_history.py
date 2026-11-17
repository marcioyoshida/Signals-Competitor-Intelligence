"""BCB bank-fee tables, kept as dated snapshots (#335, ADR 031 Board M1).

Olinda ``Informes_ListaTarifasPorInstituicaoFinanceira`` (probed 2026-11-16) serves only the
fees **in force today**: per institution (8-digit CNPJ root) and client type (F/J), each
service with its maximum value, unit, periodicity and ``DataVigencia`` (the date the current
value took effect). There is no history endpoint. So fee history is:

* the past, partly: ``DataVigencia`` dates the last change of each current fee (Itaú's 33
  personal-account fees took effect between 2012 and 2025);
* the future, fully: one dated snapshot per run under ``board_history/tarifas/{date}.json.gz``,
  diffed by ``changes``.

Scope per snapshot: the BCB institution groups for banks (03: private, public and Caixa,
154 institutions), credit-finance companies (07, 103), payment institutions (12, 199) and
direct-credit companies (13, 141): about 600 institutions × F/J. Credit cooperatives (735) are
left out. ``Informes_ListaValoresDeServicoBancario`` adds the min / average / max of every
service per group, the market reference. Its maximum is sometimes the filler 99,999.99,
which inflates the average; such rows carry ``placeholder_max``.

Attribution: each institution root through the #330 map at the snapshot date. It makes no
model calls.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote

from src.ingest import bcb_entity_history as eh

BASE = "https://olinda.bcb.gov.br/olinda/servico/Informes_ListaTarifasPorInstituicaoFinanceira/versao/v1/odata"
BASE_VALORES = "https://olinda.bcb.gov.br/olinda/servico/Informes_ListaValoresDeServicoBancario/versao/v1/odata"
PREFIX = "board_history/tarifas/"
SCHEMA = "onca/tarifas-snapshot/v1"
GROUPS = {"03": "Bancos privados + públicos + Caixa", "07": "Sociedades de Crédito, Financiamento e Investimento",
          "12": "Instituição de Pagamento", "13": "Sociedade de Crédito Direto"}
CLIENT_TYPES = ("F", "J")
PLACEHOLDER = 99_999.0


def snapshot_key(day: str) -> str:
    return f"{PREFIX}{day}.json.gz"


def _url(base: str, fn: str, params: Mapping[str, str]) -> str:
    sig = ",".join(f"{k}=@{k}" for k in params)
    args = "&".join(f"@{k}={quote(repr_odata(v))}" for k, v in params.items())
    return f"{base}/{fn}({sig})?{args}&$format=json&$top=100000"


def repr_odata(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def _get(url: str, tries: int = 4) -> Any:
    import requests
    last: Exception | None = None
    for i in range(tries):
        try:
            r = requests.get(url, timeout=90)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


def institutions(group: str, get: Callable[[str], Any] = _get) -> list[dict[str, str]]:
    url = _url(BASE, "ListaInstituicoesDeGrupoConsolidado", {"CodigoGrupoConsolidado": group})
    return [{"cnpj8": str(r["Cnpj"]).zfill(8)[:8], "name": r.get("Nome")} for r in get(url).get("value", [])]


def fees(cnpj8: str, client: str, get: Callable[[str], Any] = _get) -> list[dict[str, Any]]:
    url = _url(BASE, "ListaTarifasPorInstituicaoFinanceira", {"PessoaFisicaOuJuridica": client, "CNPJ": cnpj8})
    return [{"code": r.get("CodigoServico"), "service": r.get("Servico"), "unit": r.get("Unidade"),
             "since": r.get("DataVigencia"), "max": r.get("ValorMaximo"), "value_type": r.get("TipoValor"),
             "periodicity": r.get("Periodicidade")} for r in get(url).get("value", [])]


def market(group: str, client: str, get: Callable[[str], Any] = _get) -> list[dict[str, Any]]:
    url = _url(BASE_VALORES, "ListaValoresServicoBancario",
               {"PessoaFisicaOuJuridica": client, "CodigoGrupoConsolidado": group})
    return [{"code": r.get("CodigoServico"), "service": r.get("NomeServico"), "min": r.get("ValorMinimo"),
             "avg": r.get("ValorMedio"), "max": r.get("ValorMaximo"),
             "placeholder_max": is_placeholder(r.get("ValorMaximo"))} for r in get(url).get("value", [])]


def is_placeholder(v: Any) -> bool:
    """BCB's group reference reports 99,999.99 as a maximum: a filler, not a price. It drags
    the group average (account opening averaged R$2,915 in the bank group, 2026-11-16), so
    a consumer must not read ``avg`` as a typical fee when this is set."""
    return isinstance(v, (int, float)) and v >= PLACEHOLDER


def snapshot(day: str, emap: Mapping[str, Any], *, groups: Iterable[str] = tuple(GROUPS),
             get: Callable[[str], Any] = _get, pause: float = 0.25,
             log: Callable[[str], None] = lambda _m: None) -> dict[str, Any]:
    """Every in-scope institution's F/J fee table and each group's market reference."""
    insts: dict[str, dict[str, Any]] = {}
    for g in groups:
        for inst in institutions(g, get):
            insts.setdefault(inst["cnpj8"], {**inst, "groups": []})["groups"].append(g)
        time.sleep(pause)
    failed = 0
    for i, (cnpj8, inst) in enumerate(sorted(insts.items())):
        inst["entity_id"] = eh.entity_at(emap, cnpj8, day)
        for c in CLIENT_TYPES:
            try:
                inst[c] = fees(cnpj8, c, get)
            except Exception:  # noqa: BLE001 - one institution must not stop the snapshot
                inst[c] = None
                failed += 1
            time.sleep(pause)
        if i % 100 == 99:
            log(f"  {i + 1}/{len(insts)} institutions")
    ref = {g: {c: market(g, c, get) for c in CLIENT_TYPES} for g in groups}
    return {"schema": SCHEMA, "date": day, "source": BASE, "groups": {g: GROUPS.get(g) for g in groups},
            "institutions": insts, "market": ref, "failed": failed}


def changes(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Fee moves between two snapshots: (institution, client type, service) whose maximum or
    effective date changed, plus fees that appeared or were withdrawn."""
    out = []
    oi, ni = old.get("institutions") or {}, new.get("institutions") or {}
    for cnpj8 in sorted(set(oi) | set(ni)):
        for c in CLIENT_TYPES:
            a = {f["code"]: f for f in ((oi.get(cnpj8) or {}).get(c) or [])}
            b = {f["code"]: f for f in ((ni.get(cnpj8) or {}).get(c) or [])}
            for code in sorted(set(a) | set(b)):
                fa, fb = a.get(code), b.get(code)
                if fa and fb and (fa.get("max"), fa.get("since")) == (fb.get("max"), fb.get("since")):
                    continue
                out.append({"cnpj8": cnpj8, "entity_id": (ni.get(cnpj8) or oi.get(cnpj8) or {}).get("entity_id"),
                            "client": c, "code": code, "service": (fb or fa).get("service"),
                            "from": fa.get("max") if fa else None, "to": fb.get("max") if fb else None,
                            "since": fb.get("since") if fb else None,
                            "kind": "new" if not fa else "withdrawn" if not fb else "changed"})
    return out


def put_json(s3: Any, bucket: str, key: str, doc: Any) -> None:
    s3.put_object(Bucket=bucket, Key=key, ContentType="application/json", ContentEncoding="gzip",
                  Body=gzip.compress(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()))


def get_json(s3: Any, bucket: str, key: str) -> Any:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception:  # noqa: BLE001
        return None
    try:
        body = gzip.decompress(body)
    except OSError:
        pass
    return json.loads(body)


def latest_snapshot_key(s3: Any, bucket: str, before: str | None = None) -> str | None:
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=PREFIX):
        keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".json.gz")]
    keys = sorted(k for k in keys if before is None or k < snapshot_key(before))
    return keys[-1] if keys else None


def today() -> str:
    return dt.date.today().isoformat()
