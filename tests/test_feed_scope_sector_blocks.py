"""#203: sector-tagged top-level blocks (silence, market_structure, pricing, ifdata_market) are
scoped to the licence on every projection — SaaS /api/feed, the Entry portal slice and the
public sample — while market-wide context (macro, source_health) stays shared."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard import feed_builder as fb


def _feed():
    return {
        "run_date": "2026-10-01",
        "entity_attrs": {
            "itau": {"industries": ["banking"]},
            "fii_a": {"industries": ["real-estate-funds"]},
            "bet_a": {"industries": ["betting"]},
        },
        "groups": {},
        "entities": [{"entity": "itau"}, {"entity": "fii_a"}, {"entity": "bet_a"}],
        "feed": [{"id": "f1", "entity": "fii_a", "industries": ["real-estate-funds"],
                  "date": "2026-10-01", "citations": [{"url": "https://cvm.gov.br/x"}]}],
        "silence": [
            {"entity": "itau", "label": "Itaú", "industries": ["banking"]},
            {"entity": "fii_a", "label": "FII A", "industries": ["real-estate-funds"]},
            {"entity": "bet_a", "label": "Bet A", "industries": ["betting"]},
        ],
        "market_structure": {"banking": {"leader": {"entity": "itau"}},
                             "real-estate-funds": {"leader": {"entity": "fii_a"}}},
        "pricing": {"banking": {"pressure": "alta"}, "real-estate-funds": {"pressure": "baixa"}},
        "ifdata_market": {"metric": "Ativo Total", "top": [{"entity": "itau"}]},
        "macro": {"selic": {"current": 13.75}},
        "source_health": [{"lens": "news", "band": "ok"}],
    }


def _silenced(out):
    return {r["entity"] for r in out["silence"]}


def test_saas_projection_scopes_sector_blocks():
    out = fb.scope_feed_to_modules(_feed(), ["real-estate-funds"])
    assert _silenced(out) == {"fii_a"}
    assert set(out["market_structure"]) == {"real-estate-funds"}
    assert set(out["pricing"]) == {"real-estate-funds"}
    assert out["ifdata_market"] == {}          # banking-system ranking: banking licences only
    assert out["macro"] == _feed()["macro"]     # market-wide context stays shared
    assert out["source_health"] == _feed()["source_health"]


def test_banking_licence_keeps_ifdata_market():
    out = fb.scope_feed_to_modules(_feed(), ["banking"])
    assert _silenced(out) == {"itau"}
    assert out["ifdata_market"]["metric"] == "Ativo Total"
    assert set(out["market_structure"]) == {"banking"}


def test_empty_licence_fails_closed():
    out = fb.scope_feed_to_modules(_feed(), [])
    assert out["silence"] == [] and out["market_structure"] == {} and out["pricing"] == {}
    assert out["ifdata_market"] == {}


def test_entry_portal_slice_scopes_sector_blocks():
    out = fb.derive_entry_feed(_feed())        # all entry industries
    assert _silenced(out) == {"fii_a", "bet_a"}
    assert "banking" not in out["market_structure"] and "banking" not in out["pricing"]
    assert out["ifdata_market"] == {}


def test_public_sample_never_carries_other_sectors():
    out = fb.derive_sample_feed(_feed(), industry="real-estate-funds")
    assert _silenced(out) <= {"fii_a"}
    assert set(out.get("market_structure") or {}) <= {"real-estate-funds"}
    assert set(out.get("pricing") or {}) <= {"real-estate-funds"}
    assert not out.get("ifdata_market")


def test_executive_copy_is_rebuilt_from_the_scoped_blocks():
    """executive.build_executive copies market_structure / ifdata_market / pricing into the
    officer payload; it is rebuilt from the scoped feed, so the fix must reach it too."""
    out = fb.scope_feed_to_modules(_feed(), ["real-estate-funds"])
    blob = repr(out.get("executive") or {})
    assert "'banking'" not in blob and "Ativo Total" not in blob
