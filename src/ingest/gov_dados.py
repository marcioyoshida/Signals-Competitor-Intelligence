"""dados.gov.br catalog client — the federated cross-industry open-data route.

dados.gov.br is the federal open-data catalog (CKAN-backed) that federates datasets from
every organ. This thin client searches it and resolves a dataset's downloadable resource
URLs, so an ingester can pull a source (e.g. consumidor.gov.br complaints, #63) without
hard-coding a fragile per-site scrape.

API shape (verified against the OpenAPI at https://dados.gov.br/v3/api-docs, 2026-08-31):
  base   https://dados.gov.br/dados/api/publico
  auth   header ``chave-api-dados-abertos: <GOV_DADOS_TOKEN>`` (the whole API is token-gated)
  search GET /conjuntos-dados?nomeConjuntoDados=<q>  -> [ {id, title, nome, nomeOrganizacao} ]
  detail GET /conjuntos-dados/{id}                   -> { recursos: [ {link, nomeArquivo,
                                                        formato, titulo, tamanho} ] }

Token: read from ``GOV_DADOS_TOKEN`` env or the ``signalscompetitor/onca/api-key`` secret.
NB (2026-08-31) the stored token is currently REJECTED (401 on every route) — it must be
regenerated at dados.gov.br → "Minha Conta". With no valid token this client returns
``[]``/``None`` (fail-closed), so any ingester built on it is simply inert until the token
is fixed. Best-effort throughout.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

BASE = "https://dados.gov.br/dados/api/publico"
AUTH_HEADER = "chave-api-dados-abertos"
_SECRET_ID = "signalscompetitor/onca/api-key"
_TOKEN_CACHE: dict[str, str | None] = {}


def token() -> str | None:
    """GOV_DADOS_TOKEN from env, else the api-key secret (JSON). Cached; None if absent."""
    if "v" in _TOKEN_CACHE:
        return _TOKEN_CACHE["v"]
    tok = os.environ.get("GOV_DADOS_TOKEN")
    if not tok:
        try:
            import boto3
            raw = boto3.client("secretsmanager").get_secret_value(SecretId=_SECRET_ID)["SecretString"]
            tok = (json.loads(raw) or {}).get("GOV_DADOS_TOKEN")
        except Exception as exc:  # pragma: no cover - secret unavailable locally
            print(f"Warning: GOV_DADOS_TOKEN unavailable: {exc}")
            tok = None
    _TOKEN_CACHE["v"] = tok
    return tok


def _get(path: str, params: dict[str, Any] | None,
         fetcher: Callable[[str, dict[str, Any] | None, dict[str, str]], Any] | None) -> Any:
    tok = token()
    if not tok:
        return None
    if fetcher is not None:
        return fetcher(f"{BASE}{path}", params, {AUTH_HEADER: tok})
    import requests
    try:
        resp = requests.get(f"{BASE}{path}", params=params, timeout=30,
                            headers={AUTH_HEADER: tok, "Accept": "application/json",
                                     "User-Agent": "Onca-CI/1.0 (competitive-intelligence)"})
    except requests.RequestException as exc:  # pragma: no cover - network best-effort
        print(f"Warning: dados.gov.br GET {path} failed: {exc}")
        return None
    if resp.status_code != 200:
        print(f"Warning: dados.gov.br GET {path} -> HTTP {resp.status_code} "
              "(token rejected? regenerate GOV_DADOS_TOKEN)")
        return None
    try:
        return resp.json()
    except ValueError:  # pragma: no cover
        return None


def search_datasets(query: str, *, fetcher=None) -> list[dict[str, Any]]:
    """Datasets whose name matches ``query`` (empty list if none / no token)."""
    data = _get("/conjuntos-dados", {"nomeConjuntoDados": query}, fetcher)
    rows = data if isinstance(data, list) else (data or {}).get("content") or []
    return [r for r in rows if isinstance(r, dict) and r.get("id")]


def dataset_resources(dataset_id: str, *, fetcher=None) -> list[dict[str, Any]]:
    """Downloadable resources of a dataset: [{link, nomeArquivo, formato, titulo}]."""
    data = _get(f"/conjuntos-dados/{dataset_id}", None, fetcher)
    recs = (data or {}).get("recursos") if isinstance(data, dict) else None
    out: list[dict[str, Any]] = []
    for r in recs or []:
        link = r.get("link") or r.get("url")
        if link:
            out.append({"link": link, "nomeArquivo": r.get("nomeArquivo"),
                        "formato": (r.get("formato") or r.get("format") or "").upper() or None,
                        "titulo": r.get("titulo") or r.get("descricao"),
                        "atualizado": r.get("dataUltimaAtualizacaoArquivo")})
    return out


def find_resource(query: str, *, formato: str | None = None, name_contains: str | None = None,
                  fetcher=None) -> dict[str, Any] | None:
    """First resource across the matching datasets that fits ``formato``/``name_contains``.

    Returns the resource dict (with ``link``) or None. ``formato`` e.g. "CSV"/"ZIP";
    ``name_contains`` filters by ``nomeArquivo``/``titulo`` (case-insensitive)."""
    want_fmt = (formato or "").upper() or None
    needle = (name_contains or "").lower() or None
    for ds in search_datasets(query, fetcher=fetcher):
        for r in dataset_resources(ds["id"], fetcher=fetcher):
            if want_fmt and (r.get("formato") or "") != want_fmt:
                continue
            if needle and needle not in f"{r.get('nomeArquivo') or ''} {r.get('titulo') or ''}".lower():
                continue
            return {**r, "dataset_id": ds["id"], "dataset_title": ds.get("title") or ds.get("nome")}
    return None


def clear_cache() -> None:
    _TOKEN_CACHE.clear()
