"""Enrich new BCB entrants with Receita Federal CNPJ data (brand + controllers).

The BCB registry gives only a legal name and the 8-digit CNPJ *root* (raiz),
so a quietly-registered fintech shows up as an anonymous LTDA. This resolves the
brand (nome_fantasia) and the quadro de sócios/administradores (QSA — who's
behind it) via BrasilAPI, a public wrapper over Receita's open CNPJ data.

Live-verified schema (2026-08-16), BrasilAPI /api/cnpj/v1/{cnpj14}:
  razao_social, nome_fantasia, capital_social, cnae_fiscal_descricao,
  data_inicio_atividade, qsa[].{nome_socio, qualificacao_socio}

Registry CNPJ is the 8-digit raiz → reconstruct the matriz (raiz + 0001 + DV).
Enrich only NEW entrants (bounded), cache by raiz, degrade gracefully.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Callable

import requests

BRASILAPI = "https://brasilapi.com.br/api/cnpj/v1/{cnpj}"


def _dv(base: str) -> str:
    """Two CNPJ check digits for a 12-digit base."""
    def digit(nums: str, weights: list[int]) -> str:
        total = sum(int(n) * w for n, w in zip(nums, weights))
        rem = total % 11
        return "0" if rem < 2 else str(11 - rem)

    w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    d1 = digit(base, w1)
    d2 = digit(base + d1, [6] + w1)
    return d1 + d2


def full_cnpj(value: str | None) -> str | None:
    """Normalize to a 14-digit CNPJ; reconstruct the matriz from an 8-digit raiz."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 14:
        return digits
    if len(digits) == 8:
        base = digits + "0001"
        return base + _dv(base)
    return None


def fetch_cnpj(
    cnpj: str | None,
    *,
    timeout: int = 8,
    session: requests.Session | None = None,
) -> dict[str, Any] | None:
    """Fetch full CNPJ data from BrasilAPI; None on any failure (best-effort)."""
    full = full_cnpj(cnpj)
    if not full:
        return None
    getter = session.get if session else requests.get
    try:
        resp = getter(
            BRASILAPI.format(cnpj=full),
            timeout=timeout,
            headers={"User-Agent": "Onca-CI/1.0 (competitive-intelligence)"},
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as exc:  # pragma: no cover - network best-effort
        print(f"Warning: Receita lookup failed for {cnpj}: {exc}")
        return None


def summarize(data: dict[str, Any]) -> dict[str, Any]:
    """Pull the enrichment fields from a BrasilAPI CNPJ payload."""
    qsa = data.get("qsa") or []
    partners = [
        {"name": q.get("nome_socio"), "role": q.get("qualificacao_socio")}
        for q in qsa
        if q.get("nome_socio")
    ]
    # Prefer an owning partner ("Sócio"/holding) over a mere administrator.
    owners = [p for p in partners if "administrador" not in str(p["role"]).lower()]
    primary = (owners or partners or [{}])[0].get("name")
    out = {
        "trade_name": data.get("nome_fantasia") or None,
        "legal_name": data.get("razao_social") or None,
        "controllers": [p["name"] for p in partners][:6],
        "controller": primary,
        "cnae": data.get("cnae_fiscal_descricao") or None,
        "founded": data.get("data_inicio_atividade") or None,
        "capital_social": data.get("capital_social"),
    }
    return {k: v for k, v in out.items() if v}


def enrich_entrants(
    entrants: list[dict[str, Any]],
    *,
    max_lookups: int | None = None,
    fetcher: Callable[[str | None], dict[str, Any] | None] | None = None,
    pause_sec: float = 0.2,
) -> list[dict[str, Any]]:
    """Attach Receita brand/QSA fields to new-entrant records (in place)."""
    if not entrants:
        return entrants
    max_lookups = max_lookups or int(os.environ.get("ONCA_RECEITA_MAX", "15"))
    fetch = fetcher or fetch_cnpj
    cache: dict[str, dict[str, Any]] = {}
    done = 0
    for e in entrants:
        if done >= max_lookups:
            break
        raiz = re.sub(r"\D", "", str(e.get("cnpj") or ""))[:8]
        if not raiz:
            continue
        data = cache.get(raiz)
        if data is None:
            data = fetch(e.get("cnpj"))
            done += 1
            if data:
                cache[raiz] = data
            if pause_sec:
                time.sleep(pause_sec)
        if data:
            e.update(summarize(data))
    return entrants
