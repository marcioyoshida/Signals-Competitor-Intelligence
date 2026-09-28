"""#187: the storefront dispatch contract (upgrade Lambda) for Onça's per-module purchases."""
from __future__ import annotations

import pytest

from src.dashboard import tenant_config as tc
from src.dashboard import upgrade


class _T:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        it = self.items.get(Key["tenant_id"])
        return {"Item": it} if it else {}

    def put_item(self, Item):
        self.items[Item["tenant_id"]] = dict(Item)


def _ev(**kw):
    base = {"product": "onca", "tenant_ref": "entry-ana-1a2b", "tier": "entry", "module": "crypto",
            "event": "checkout.session.completed", "status": "active", "stripe_event_id": "evt_1"}
    base.update(kw)
    return base


def _world():
    t = _T()
    t.items["entry-ana-1a2b"] = {"tenant_id": "entry-ana-1a2b", "tier": "entry", "modules": [],
                                 "billing_managed": True, "trial_until": "2000-01-01T00:00:00+00:00",
                                 "trial_modules": ["crypto"]}
    return t


def test_purchase_grants_and_a_replay_is_a_noop():
    t = _world()
    assert tc.get_tenant_config("entry-ana-1a2b", table=t)["billing"]["state"] == "trial_expired"
    r = upgrade.handle(_ev(), t)
    assert r == {"ok": True, "action": "grant", "billing": "active"}
    assert tc.get_tenant_config("entry-ana-1a2b", table=t)["modules"] == ["crypto"]
    assert upgrade.handle(_ev(), t) == {"ok": True, "replay": True}


def test_cancel_then_a_replayed_purchase_does_not_regrant():
    t = _world()
    upgrade.handle(_ev(), t)
    upgrade.handle(_ev(event="customer.subscription.deleted", status="canceled",
                       stripe_event_id="evt_2"), t)
    assert tc.get_tenant_config("entry-ana-1a2b", table=t)["billing"]["state"] == "lapsed"
    assert upgrade.handle(_ev(), t)["replay"] is True
    assert tc.get_tenant_config("entry-ana-1a2b", table=t)["modules"] == []


def test_past_due_is_grace_and_saas_upgrade_lands_on_the_same_tenant():
    t = _world()
    upgrade.handle(_ev(), t)
    assert upgrade.handle(_ev(event="customer.subscription.updated", status="past_due",
                              stripe_event_id="evt_3"), t)["action"] is None
    upgrade.handle(_ev(tier="saas_premium", module="banking", stripe_event_id="evt_4"), t)
    cfg = tc.get_tenant_config("entry-ana-1a2b", table=t)
    assert cfg["tier"] == "saas" and cfg["modules"] == ["banking", "crypto"]
    assert len([k for k in t.items if not k.startswith("BILLING#")]) == 1  # no second tenant


def test_unknown_tenant_or_uncovered_module_raises_for_the_alarm():
    t = _world()
    with pytest.raises(upgrade.UnknownTenant):
        upgrade.handle(_ev(tenant_ref="ghost"), t)
    with pytest.raises(upgrade.UnknownTenant):
        upgrade.handle(_ev(tier="saas_entry", module="banking", stripe_event_id="evt_9"), t)
    assert "BILLING#evt_9" not in t.items  # nothing recorded: stays retryable


def test_other_products_and_bad_event_ids_are_ignored():
    t = _world()
    assert upgrade.handle(_ev(product="tarantula"), t)["reason"] == "product"
    assert upgrade.handle(_ev(stripe_event_id="x"), t)["reason"] == "event_id"


def test_upgrade_url_never_links_what_cannot_be_fulfilled():
    u = upgrade.upgrade_url("entry-ana-1a2b", "saas_mid", "insurance")
    assert u.startswith("https://signals-llc.store/?product=onca&tier=saas_mid&module=insurance&ref=")
    assert upgrade.upgrade_url("entry-ana-1a2b", "saas_entry", "banking") is None
    assert upgrade.upgrade_url("a@b.com", "entry", "crypto") is None
    assert upgrade.upgrade_url("", "entry", "crypto") is None
