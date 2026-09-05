"""Product Officer (CPO) ingestion enrichment — R1–R6 (docs/2026-09-05-product-officer-
ingestion-requirements.md).

Every field here is GROUNDED in data the registry/feed already carries; a field that is
*derived* (not a primary ingested fact) is labelled an inference on the board:

- R1 ESG        — already ingested (`esg_ise_b3` → `entity_attrs.esg`, ISE B3 membership); this
                  module only summarizes standing. Broader ESG needs a licensed source (#30).
- R2 Certifications — DERIVED from registry facts (B3 listing, the sector's regulator, ISE,
                  parent) → `entity_attrs.certifications`. Grounded (each cites its basis).
- R3 Market structure — per-sector size + share from the CVM `financials` store (real revenue/
                  assets) (+ BCB IF.data `market_share` where present) → `feed.market_structure`.
- R4 Pricing    — per-sector price-pressure PROXY from the juros/ofertas/pix lenses (labelled
                  inference) → `feed.pricing`.
- R5 Source-health — per-source freshness/volume/staleness derived from the feed cards →
                  `feed.source_health`.
- R6 Firmographics — coarse public/private + size band from registry facts + signal volume
                  (labelled inference) → `entity_attrs.firmographics`.
"""
from __future__ import annotations

from typing import Any

# R2 — the sector's primary regulator (a licence/authorization basis).
_REGULATOR_BY_INDUSTRY = {
    "banking": "BCB", "fintech": "BCB", "acquiring": "BCB", "consorcio": "BCB", "crypto": "BCB",
    "investment-banking": "CVM", "asset-management": "CVM", "wealth-management": "CVM",
    "agri-funds": "CVM", "real-estate-funds": "CVM", "securitization": "CVM", "advisory": "CVM",
    "private-markets": "CVM", "financial-data-analytics": "CVM", "brokerage": "CVM",
    "insurance": "SUSEP", "closed-pension": "PREVIC", "betting": "SPA/MF",
}
_PRICE_LENSES = {"juros", "ofertas", "pix", "inf_diario"}


# --- R2 certifications ----------------------------------------------------------------
def derive_certifications(eid: str, a: dict[str, Any]) -> list[dict[str, Any]]:
    """Certifications/licences GROUNDED in registry facts — each carries its basis."""
    out: list[dict[str, Any]] = []
    inds = a.get("industries") or []
    regs = {_REGULATOR_BY_INDUSTRY[i] for i in inds if i in _REGULATOR_BY_INDUSTRY}
    for reg in sorted(regs):
        out.append({"label": f"Autorização {reg}", "source": reg, "basis": "industry"})
    if a.get("ticker"):
        out.append({"label": f"Listada B3 ({a['ticker']})", "source": "B3", "basis": "ticker"})
    if (a.get("esg") or {}).get("ise_b3"):
        out.append({"label": "ISE B3 (sustentabilidade)", "source": "B3", "basis": "esg"})
    if a.get("parent"):
        out.append({"label": f"Subsidiária de {a['parent']}", "source": "registry", "basis": "parent"})
    return out


# --- R6 firmographics (coarse, labelled inference) ------------------------------------
def derive_firmographics(eid: str, a: dict[str, Any], signal_volume: int) -> dict[str, Any]:
    """Coarse public/private + a signal-volume size band. INFERENCE (not a primary fact)."""
    own = a.get("ownership")
    own_status = own.get("status") if isinstance(own, dict) else own
    public = bool(a.get("ticker")) or own_status == "public"
    band = ("grande" if signal_volume >= 15 else "média" if signal_volume >= 5
            else "pequena" if signal_volume >= 1 else "—")
    return {"listing": "pública" if public else "privada", "size_band": band,
            "signal_volume": signal_volume, "is_inference": True}


# --- R3 market structure (real: CVM financials + optional IF.data share) ---------------
def market_structure(financials: list[dict[str, Any]], entity_attrs: dict[str, Any],
                     market_share: dict[str, float] | None = None) -> dict[str, Any]:
    """Per-sector market size + leader share from the CVM `financials` store (revenue), keyed by
    the entity's industry. Real data where an issuer files; sectors without listed issuers report
    `covered=False` (honest — no fabricated size)."""
    by_sector: dict[str, list[dict[str, Any]]] = {}
    for f in financials or []:
        eid = f.get("entity_id")
        rev = f.get("revenue")
        if not eid or not rev:
            continue
        inds = (entity_attrs.get(eid) or {}).get("industries") or []
        # attribute to the issuer's OPERATING industries (skip leaf fund lines)
        for ind in inds:
            by_sector.setdefault(ind, []).append(
                {"entity": eid, "label": f.get("name") or eid, "revenue": rev,
                 "share_pct": (market_share or {}).get(eid)})
    out: dict[str, Any] = {}
    for ind, rows in by_sector.items():
        total = sum(r["revenue"] for r in rows)
        rows.sort(key=lambda r: r["revenue"], reverse=True)
        for r in rows:
            r["rev_share"] = round(r["revenue"] / total, 3) if total else None
        top = rows[0]
        out[ind] = {
            "covered": True, "issuers": len(rows),
            "size_revenue": total, "currency": "BRL",
            "leader": {"entity": top["entity"], "label": top["label"],
                       "rev_share": top["rev_share"], "share_pct": top.get("share_pct")},
            "hhi": round(sum((r["rev_share"] or 0) ** 2 for r in rows), 3),  # concentration
            "constituents": rows[:8],
        }
    return out


# --- R4 pricing pressure (proxy from rate/offer lenses; labelled inference) ------------
def pricing_signals(feed_cards: list[dict[str, Any]], recent_dates: set[str]) -> dict[str, Any]:
    """Per-sector price-pressure PROXY: the volume + recency of rate/fee/offer signals. Not a
    price index — an attention proxy, surfaced as an inference until R4's structured source lands."""
    out: dict[str, dict[str, Any]] = {}
    for c in feed_cards:
        if not (set(c.get("lenses") or []) & _PRICE_LENSES):
            continue
        recent = str(c.get("date") or "") in recent_dates
        for ind in (c.get("industries") or []):
            s = out.setdefault(ind, {"signals": 0, "recent": 0, "is_inference": True})
            s["signals"] += 1
            if recent:
                s["recent"] += 1
    for s in out.values():
        s["pressure"] = ("alta" if s["recent"] >= 5 else "média" if s["recent"] >= 1 else "baixa")
    return out


# --- R5 source health (feed-derived freshness/volume/staleness) ------------------------
def source_health(feed_cards: list[dict[str, Any]], as_of: str | None) -> list[dict[str, Any]]:
    """Per-source (lens) freshness/volume/staleness from the feed — a data-quality readout with
    NO change to any ingester."""
    from datetime import date

    def _days(latest: str) -> int | None:
        try:
            ya, ma, da = (int(x) for x in str(as_of)[:10].split("-"))
            yb, mb, db = (int(x) for x in str(latest)[:10].split("-"))
            return abs((date(ya, ma, da) - date(yb, mb, db)).days)
        except Exception:
            return None

    agg: dict[str, dict[str, Any]] = {}
    for c in feed_cards:
        d = str(c.get("date") or "")
        for lens in (c.get("lenses") or []):
            s = agg.setdefault(lens, {"lens": lens, "docs": 0, "latest": ""})
            s["docs"] += 1
            if d > s["latest"]:
                s["latest"] = d
    out = []
    for lens, s in agg.items():
        staleness = _days(s["latest"]) if s["latest"] else None
        band = ("ok" if staleness is not None and staleness <= 2
                else "warn" if staleness is not None and staleness <= 7 else "stale")
        out.append({**s, "staleness_days": staleness, "band": band})
    out.sort(key=lambda s: (s["band"] != "stale", -(s["docs"])))
    return out


# --- top-level enrichment (called by feed_builder) ------------------------------------
def enrich_feed(feed: dict[str, Any]) -> None:
    """Attach R2–R6 Product enrichment to the feed in place (best-effort). ESG (R1) is already
    on entity_attrs from the ingest pipeline; this adds certifications + firmographics per entity
    and the source-health / market-structure / pricing blocks."""
    cards = [c for c in (feed.get("feed") or []) if isinstance(c, dict)]
    as_of = feed.get("as_of")
    dates = sorted({str(c.get("date")) for c in cards if c.get("date")})
    recent = set(dates[-7:])

    # per-entity signal volume (for firmographics size band)
    vol: dict[str, int] = {}
    for c in cards:
        if c.get("entity"):
            vol[c["entity"]] = vol.get(c["entity"], 0) + 1

    ea = feed.get("entity_attrs") or {}
    for eid, a in ea.items():
        a = a or {}
        if not a.get("certifications"):
            a["certifications"] = derive_certifications(eid, a)  # R2
        a["firmographics"] = derive_firmographics(eid, a, vol.get(eid, 0))  # R6
        ea[eid] = a
    feed["entity_attrs"] = ea

    feed["source_health"] = source_health(cards, as_of)  # R5
    feed["market_structure"] = market_structure(  # R3
        feed.get("financials") or [], ea, feed.get("market_share") or {})
    feed["pricing"] = pricing_signals(cards, recent)  # R4
