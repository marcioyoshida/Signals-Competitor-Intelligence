import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from src.synth import resolver  # noqa: E402


class FakeCacheTable:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.put_calls = 0
        self.get_calls = 0

    def get_item(self, Key):
        self.get_calls += 1
        it = self.items.get(Key["pk"])
        return {"Item": it} if it else {}

    def put_item(self, Item):
        self.put_calls += 1
        self.items[Item["pk"]] = Item


class FakeRegistryTable:
    """Minimal stand-in exercised through entity_registry's own _table() so the
    registry-mode branch is tested against real entity_registry code, not a
    re-implementation of it."""

    def __init__(self, cnpj_map, entities):
        self.cnpj_map = cnpj_map  # cnpj_root -> entity_id
        self.entities = entities  # entity_id -> {display_name, canonical_id, industries}

    def get_item(self, Key):
        pk = Key["pk"]
        if pk.startswith("CNPJ#"):
            eid = self.cnpj_map.get(pk[len("CNPJ#"):])
            return {"Item": {"entity_id": eid}} if eid else {}
        if pk.startswith("ENT#"):
            eid = pk[len("ENT#"):]
            ent = self.entities.get(eid)
            return {"Item": {"entity_id": eid, **ent}} if ent else {}
        return {}


# --- mode() -------------------------------------------------------------------

def test_mode_defaults_to_registry(monkeypatch):
    monkeypatch.delenv("ONCA_RESOLUTION_MODE", raising=False)
    assert resolver.mode() == resolver.REGISTRY


def test_mode_reads_remote(monkeypatch):
    monkeypatch.setenv("ONCA_RESOLUTION_MODE", "remote")
    assert resolver.mode() == resolver.REMOTE


def test_an_unrecognised_mode_falls_back_to_registry_not_remote(monkeypatch):
    # Fail toward the unchanged, no-outbound-calls behavior on a typo/misconfig —
    # never toward suddenly making network calls a SaaS tenant never expected.
    monkeypatch.setenv("ONCA_RESOLUTION_MODE", "typo-mode")
    assert resolver.mode() == resolver.REGISTRY


# --- resolve_known_id: registry mode -------------------------------------------

def test_registry_mode_resolves_by_cnpj_through_entity_registry(monkeypatch):
    monkeypatch.delenv("ONCA_RESOLUTION_MODE", raising=False)
    t = FakeRegistryTable(
        cnpj_map={"12345678": "acme"},
        entities={"acme": {"display_name": "Acme SA", "canonical_id": "acme",
                           "industries": ["banking"]}},
    )
    result = resolver.resolve_known_id(cnpj_root="12345678", table=t)
    assert result == {
        "entity_id": "acme", "display_name": "Acme SA", "canonical_id": "acme",
        "industries": ["banking"], "confidence": 1.0,
    }


def test_registry_mode_returns_none_for_an_unknown_cnpj(monkeypatch):
    monkeypatch.delenv("ONCA_RESOLUTION_MODE", raising=False)
    t = FakeRegistryTable(cnpj_map={}, entities={})
    assert resolver.resolve_known_id(cnpj_root="99999999", table=t) is None


# --- resolve_known_id: remote mode ---------------------------------------------

def test_remote_mode_calls_the_api_on_a_cache_miss_and_then_caches(monkeypatch):
    monkeypatch.setenv("ONCA_RESOLUTION_MODE", "remote")
    monkeypatch.setenv("ONCA_RESOLVE_API_URL", "https://vendor.example/resolve")
    cache = FakeCacheTable()
    calls = []

    def fake_post(url, payload):
        calls.append((url, payload))
        return {"entity_id": "acme", "display_name": "Acme SA",
               "canonical_id": "acme", "industries": ["banking"], "confidence": 0.95}

    monkeypatch.setattr(resolver, "_sign_and_post", fake_post)
    result = resolver.resolve_known_id(cnpj_root="12345678", cache_table=cache)

    assert result["entity_id"] == "acme"
    assert calls == [("https://vendor.example/resolve", {"cnpj_root": "12345678"})]
    assert cache.put_calls == 1
    assert cache.items["RESOLVE#cnpj#12345678"]["entity_id"] == "acme"
    assert "ttl" in cache.items["RESOLVE#cnpj#12345678"]


def test_remote_mode_serves_a_cache_hit_without_calling_the_api(monkeypatch):
    monkeypatch.setenv("ONCA_RESOLUTION_MODE", "remote")
    monkeypatch.setenv("ONCA_RESOLVE_API_URL", "https://vendor.example/resolve")
    cache = FakeCacheTable()
    cache.items["RESOLVE#cnpj#12345678"] = {
        "pk": "RESOLVE#cnpj#12345678", "entity_id": "acme",
        "display_name": "Acme SA", "canonical_id": "acme",
        "industries": ["banking"], "ttl": 9999999999,
    }
    calls = []
    monkeypatch.setattr(resolver, "_sign_and_post", lambda *a: calls.append(a) or {})

    result = resolver.resolve_known_id(cnpj_root="12345678", cache_table=cache)
    assert result["entity_id"] == "acme"
    assert calls == []  # never touched the network


def test_remote_mode_a_miss_is_not_cached(monkeypatch):
    # A newly-registered entity must resolve on the tenant's very next lookup,
    # not wait out a stale negative TTL.
    monkeypatch.setenv("ONCA_RESOLUTION_MODE", "remote")
    monkeypatch.setenv("ONCA_RESOLVE_API_URL", "https://vendor.example/resolve")
    cache = FakeCacheTable()
    monkeypatch.setattr(resolver, "_sign_and_post", lambda *a: None)

    result = resolver.resolve_known_id(cnpj_root="00000000", cache_table=cache)
    assert result is None
    assert cache.put_calls == 0


def test_remote_mode_fails_closed_with_no_configured_endpoint(monkeypatch):
    monkeypatch.setenv("ONCA_RESOLUTION_MODE", "remote")
    monkeypatch.delenv("ONCA_RESOLVE_API_URL", raising=False)
    cache = FakeCacheTable()
    with pytest.raises(RuntimeError):
        resolver.resolve_known_id(cnpj_root="12345678", cache_table=cache)


def test_resolve_known_id_requires_at_least_one_identifier(monkeypatch):
    monkeypatch.setenv("ONCA_RESOLUTION_MODE", "remote")
    with pytest.raises(ValueError):
        resolver.resolve_known_id(cache_table=FakeCacheTable())


# --- entities.resolve_by_cnpj: the wired seam ----------------------------------

def test_entities_resolve_by_cnpj_stays_unchanged_in_registry_mode(monkeypatch):
    monkeypatch.delenv("ONCA_RESOLUTION_MODE", raising=False)
    monkeypatch.delenv("ONCA_ENTITIES_TABLE", raising=False)
    from src.synth import entities

    # No ONCA_ENTITIES_TABLE configured => unchanged pre-existing behavior: None.
    assert entities.resolve_by_cnpj("12.345.678/0001-99") is None


def test_entities_resolve_by_cnpj_routes_through_remote_mode(monkeypatch):
    monkeypatch.setenv("ONCA_RESOLUTION_MODE", "remote")
    monkeypatch.setenv("ONCA_RESOLVE_API_URL", "https://vendor.example/resolve")
    from src.synth import entities

    monkeypatch.setattr(
        resolver, "resolve_known_id",
        lambda **kw: {"entity_id": "acme"} if kw.get("cnpj_root") == "12345678" else None,
    )
    assert entities.resolve_by_cnpj("12.345.678/0001-99") == "acme"


# --- entities.resolve_entities: the documented non-seam ------------------------

def test_resolve_entities_degrades_to_empty_in_remote_mode(monkeypatch, capsys):
    monkeypatch.setenv("ONCA_RESOLUTION_MODE", "remote")
    from src.synth import entities

    resolver._warned_free_text = False  # reset the once-only warning flag
    out = entities.resolve_entities({"source": "trade_press", "subject": "Itau announces something"})
    assert out == []
    assert "no Sovereign-plane equivalent" in capsys.readouterr().out


def test_resolve_entities_warns_exactly_once(monkeypatch, capsys):
    monkeypatch.setenv("ONCA_RESOLUTION_MODE", "remote")
    from src.synth import entities

    resolver._warned_free_text = False
    entities.resolve_entities({"source": "trade_press", "subject": "a"})
    entities.resolve_entities({"source": "trade_press", "subject": "b"})
    warnings = capsys.readouterr().out.count("no Sovereign-plane equivalent")
    assert warnings == 1


def test_resolve_entities_is_unaffected_in_registry_mode(monkeypatch):
    monkeypatch.delenv("ONCA_RESOLUTION_MODE", raising=False)
    from src.synth import entities

    resolver._warned_free_text = False
    out = entities.resolve_entities({
        "source": "trade_press", "subject": "Nubank anuncia nova funcionalidade",
    })
    assert "nubank" in out


# --- guard_free_text_resolution_unavailable ------------------------------------

def test_guard_is_false_in_registry_mode(monkeypatch):
    monkeypatch.delenv("ONCA_RESOLUTION_MODE", raising=False)
    resolver._warned_free_text = False
    assert resolver.guard_free_text_resolution_unavailable() is False


# --- Decision 4 step 4: the resolve-contract version pin -----------------------

def test_sign_and_post_sends_the_contract_version_header_inside_the_signed_request(monkeypatch):
    import boto3
    from botocore.credentials import Credentials

    monkeypatch.setattr(
        boto3, "Session",
        lambda: type("S", (), {"get_credentials": staticmethod(
            lambda: Credentials("AKIAFAKE", "secretfake")
        )})(),
    )

    captured = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"entity_id": "acme"}'

    def fake_urlopen(req, timeout=10):
        captured["headers"] = dict(req.headers)
        return _FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = resolver._sign_and_post("https://vendor.example/resolve", {"cnpj_root": "12345678"})

    assert result == {"entity_id": "acme"}
    # urllib.request.Request lower-cases header names on the way in.
    assert captured["headers"]["X-onca-resolve-contract-version"] == resolver.RESOLVE_CONTRACT_VERSION
    # Covered by the SigV4 signature — present in the signed Authorization/
    # SignedHeaders set, not just tacked on after signing.
    assert "x-onca-resolve-contract-version" in captured["headers"]["Authorization"].lower()


def test_resolve_contract_version_is_a_stable_non_empty_string():
    assert isinstance(resolver.RESOLVE_CONTRACT_VERSION, str)
    assert resolver.RESOLVE_CONTRACT_VERSION
