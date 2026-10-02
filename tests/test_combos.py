"""Curated segment combos (ADR 024 amendment 2026-10-02): one storefront subscription per combo,
resolved to modules from tenant_config.COMBOS — the single source of truth."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from src.dashboard import feed_api
from src.dashboard import tenant_config as tc
from src.dashboard import upgrade

SITE = Path(__file__).resolve().parents[1] / "src" / "dashboard" / "site"
REF = "entry-0a1b2c3d4e5f"


class _T:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        it = self.items.get(Key["tenant_id"])
        return {"Item": it} if it else {}

    def put_item(self, Item):
        self.items[Item["tenant_id"]] = dict(Item)


def _world():
    t = _T()
    t.items[REF] = {"tenant_id": REF, "tier": "entry", "modules": [], "billing_managed": True,
                    "trial_until": "2000-01-01T00:00:00+00:00", "trial_modules": ["crypto"]}
    return t


def _ev(**kw):
    base = {"product": "onca", "tenant_ref": REF, "tier": "saas_combo", "combo": "bancos-completo",
            "event": "checkout.session.completed", "status": "active", "stripe_event_id": "evt_c1"}
    base.update(kw)
    return base


def _cfg(t):
    return tc.get_tenant_config(REF, table=t)


def test_combo_table_is_the_owner_decision_and_only_sellable_saas_modules():
    assert {cid: (c["modules"], c["price_brl"], tc.combo_list_price(cid))
            for cid, c in tc.COMBOS.items()} == {
        "bancos-completo": (("banking", "investment-banking"), 14900, 17800),
        "pagamentos": (("fintech", "acquiring"), 6600, 7800),
        "patrimonio": (("wealth-management", "asset-management"), 8300, 9800),
        "ativos-digitais-apostas": (("crypto", "betting"), 4900, 5800),
        "fundos-imobiliario-agro": (("real-estate-funds", "agri-funds"), 6600, 7800),
    }
    from src.synth.entity_registry import INDUSTRIES
    for c in tc.COMBOS.values():
        for m in c["modules"]:
            assert m in INDUSTRIES and m not in tc.NOT_READY_INDUSTRIES and tc.band_of(m)


def test_combo_grant_unions_modules_and_lifts_the_tier_then_replay_is_a_noop():
    t = _world()
    assert upgrade.handle(_ev(), t) == {"ok": True, "action": "grant", "billing": "active"}
    cfg = _cfg(t)
    assert cfg["tier"] == "saas" and cfg["modules"] == ["banking", "investment-banking"]
    assert cfg["billing"]["combos"] == ["bancos-completo"]
    assert cfg["billing"]["paid_modules"] == ["banking", "investment-banking"]
    sub = t.items[REF]["subs"]["combo:bancos-completo"]
    assert sub["tier"] == "saas_combo" and sub["combo"] == "bancos-completo"
    assert t.items["BILLING#evt_c1"]["combo"] == "bancos-completo"
    assert upgrade.handle(_ev(), t) == {"ok": True, "replay": True}


def test_combo_cancel_revokes_and_a_replayed_purchase_does_not_regrant():
    t = _world()
    upgrade.handle(_ev(), t)
    assert upgrade.handle(_ev(event="customer.subscription.updated", status="past_due",
                              stripe_event_id="evt_c2"), t)["action"] is None
    assert _cfg(t)["modules"] == ["banking", "investment-banking"]   # grace
    upgrade.handle(_ev(event="customer.subscription.deleted", status="canceled",
                       stripe_event_id="evt_c3"), t)
    cfg = _cfg(t)
    assert cfg["modules"] == [] and cfg["tier"] == "entry"
    assert cfg["billing"]["state"] == "lapsed" and cfg["billing"]["lapsed_combos"] == ["bancos-completo"]
    assert "combo:bancos-completo" not in cfg["billing"]["lapsed_modules"]
    assert upgrade.handle(_ev(), t)["replay"] is True
    assert _cfg(t)["modules"] == []


def test_revoking_a_combo_keeps_a_module_still_paid_separately():
    t = _world()
    upgrade.handle(_ev(tier="saas_premium", combo=None, module="banking", stripe_event_id="evt_m1"), t)
    upgrade.handle(_ev(stripe_event_id="evt_c4"), t)
    assert _cfg(t)["modules"] == ["banking", "investment-banking"]
    upgrade.handle(_ev(event="customer.subscription.deleted", status="canceled",
                       stripe_event_id="evt_c5"), t)
    cfg = _cfg(t)
    assert cfg["modules"] == ["banking"] and cfg["tier"] == "saas"


def test_unknown_or_missing_combo_raises_for_the_alarm_and_stays_retryable():
    t = _world()
    with pytest.raises(upgrade.UnknownTenant):
        upgrade.handle(_ev(combo="tudo-de-graca", stripe_event_id="evt_c6"), t)
    with pytest.raises(upgrade.UnknownTenant):
        upgrade.handle(_ev(combo=None, module="banking", stripe_event_id="evt_c7"), t)
    with pytest.raises(upgrade.UnknownTenant):
        upgrade.handle(_ev(tenant_ref="ghost", stripe_event_id="evt_c8"), t)
    assert not [k for k in t.items if k.startswith("BILLING#")]
    with pytest.raises(ValueError):   # a per-module purchase can't smuggle the combo tier
        tc.apply_purchase(REF, "saas_combo", "banking", "grant", event_id="evt_x", table=t)


def test_upgrade_url_for_combos():
    u = upgrade.upgrade_url(REF, "saas_combo", combo="pagamentos")
    p = urlparse(u)
    assert (p.scheme, p.netloc) == ("https", "signals-llc.store")
    q = {k: v[0] for k, v in parse_qs(p.query).items()}
    assert q == {"product": "onca", "tier": "saas_combo", "combo": "pagamentos", "ref": REF,
                 "return": "https://onssa.org/exec?upgraded=1"}
    assert "module" not in q
    assert upgrade.upgrade_url(REF, "saas_combo", combo="nope") is None
    assert upgrade.upgrade_url(REF, "saas_combo") is None
    assert upgrade.upgrade_url(REF, "saas_combo", "banking", combo="bancos-completo") is None
    assert upgrade.upgrade_url(REF, "saas_premium", combo="bancos-completo") is None
    assert upgrade.upgrade_url("a@b.com", "saas_combo", combo="pagamentos") is None
    # per-module links are unchanged
    assert upgrade.upgrade_url(REF, "saas_mid", "insurance").startswith(
        "https://signals-llc.store/?product=onca&tier=saas_mid&module=insurance&ref=")


def test_feed_upgrade_payload_carries_the_combo_table():
    assert feed_api.upgrade_info()["combos"] == tc.combos_payload()
    assert tc.combos_payload()["patrimonio"] == {
        "name": "Patrimônio", "modules": ["wealth-management", "asset-management"],
        "price_brl": 8300, "list_price_brl": 9800}


def test_pricing_page_combos_match_python():
    html = (SITE / "pricing.html").read_text(encoding="utf-8")
    assert "15% de desconto" not in html
    found = {m.group(1): (tuple(m.group(3).split()), int(m.group(2))) for m in re.finditer(
        r'data-combo="([^"]+)" data-price="(\d+)" data-modules="([^"]+)"', html)}
    assert found == {cid: (c["modules"], c["price_brl"]) for cid, c in tc.COMBOS.items()}
    for cid in tc.COMBOS:
        assert f'href="/exec?assinar=combo:{cid}"' in html
        save = tc.combo_list_price(cid) - tc.COMBOS[cid]["price_brl"]
        assert f"economize R$ {save:,}/mês".replace(",", ".") in html


def test_context_js_saas_bands_match_python_minus_not_ready():
    js = (SITE / "v2" / "context.js").read_text(encoding="utf-8")
    body = re.search(r"const SAAS_BANDS = (\{.*?\});", js, re.S).group(1)
    body = re.sub(r"(\w+):", r'"\1":', body)
    body = re.sub(r",\s*([\]}])", r"\1", body)
    bands = json.loads(body)
    assert bands == {b: [m for m in mods if m not in tc.NOT_READY_INDUSTRIES]
                     for b, mods in tc.SAAS_BANDS.items()}
