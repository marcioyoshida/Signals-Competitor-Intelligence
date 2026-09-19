import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest import watchlist_qsa as wq

NOW = dt.datetime(2026, 8, 23, 12, 0, tzinfo=dt.timezone.utc)


def _payload(qsa):
    return {"qsa": qsa}


def _socio(nome, qual, doc, ident="2"):
    return {"nome_socio": nome, "qualificacao_socio": qual,
            "cnpj_cpf_do_socio": doc, "identificador_de_socio": ident}


# --- extract_socios ---------------------------------------------------------
def test_extracts_pf_socios_only():
    data = _payload([
        _socio("MARIA SILVA", "Diretor", "***265018**", "2"),
        _socio("ITAUSA HOLDING S.A.", "Sócio", "60872504000123", "1"),  # PJ -> dropped
    ])
    out = wq.extract_socios(data)
    assert [s["name"] for s in out] == ["MARIA SILVA"]
    assert out[0]["doc_mask"] == "***265018**" and out[0]["role"] == "diretor"


def test_role_mapping():
    data = _payload([
        _socio("A B", "Conselheiro de Administração", "***111111**"),
        _socio("C D", "Diretor Presidente", "***222222**"),
        _socio("E F", "Sócio-Administrador", "***333333**"),
    ])
    roles = {s["name"]: s["role"] for s in wq.extract_socios(data)}
    assert roles == {"A B": "conselheiro", "C D": "diretor", "E F": "sócio"}


def test_control_roles_sorted_first_and_capped():
    qsa = [_socio(f"DIR {i}", "Diretor", f"***{i:06d}**") for i in range(5)]
    qsa += [_socio("OWNER X", "Sócio", "***999999**")]
    out = wq.extract_socios(_payload(qsa), max_persons=3)
    assert len(out) == 3
    assert out[0]["role"] == "sócio"          # control role prioritized


def test_dedup_by_name_and_mask():
    data = _payload([_socio("MARIA SILVA", "Diretor", "***265018**"),
                     _socio("MARIA SILVA", "Diretor", "***265018**")])
    assert len(wq.extract_socios(data)) == 1


# --- safe masking (never persist a full CPF) --------------------------------
def test_safe_mask_passes_through_masked():
    assert wq._safe_mask("***265018**") == "***265018**"


def test_safe_mask_remasks_a_leaked_full_cpf():
    # an unmasked 11-digit CPF must never be stored -> re-masked to the middle six
    assert wq._safe_mask("12345678901") == "***456789**"


def test_mask_digits_extracts_middle_six():
    assert wq.mask_digits("***265018**") == "265018"
    assert wq.mask_digits(None) == ""


def test_extract_never_emits_full_cpf():
    data = _payload([_socio("JOHN DOE", "Sócio", "12345678901")])  # leaked full CPF
    doc = wq.extract_socios(data)[0]["doc_mask"]
    assert "*" in doc and "12345678901" not in doc


# --- refresh: TTL + bounded ------------------------------------------------
def _fetch_ok(cnpj):
    return _payload([_socio("MARIA SILVA", "Sócio", "***265018**")])


def test_refresh_fetches_and_writes_slice():
    ents = [{"entity": "itau", "cnpj": "60701190"}]
    out = wq.refresh(ents, {}, fetch=_fetch_ok, now=NOW)
    rec = out["entities"]["itau"]
    assert rec["socios"][0]["name"] == "MARIA SILVA"
    assert rec["cnpj"].startswith("60701190") and rec["fetched_at"]
    assert out["refreshed"] == 1


def test_refresh_skips_fresh_cache():
    prev = {"entities": {"itau": {"cnpj": "60701190000104", "fetched_at": NOW.isoformat(),
                                  "socios": [{"name": "OLD"}]}}}
    called = {"n": 0}
    def fetch(c): called["n"] += 1; return _fetch_ok(c)
    out = wq.refresh([{"entity": "itau", "cnpj": "60701190"}], prev,
                     fetch=fetch, now=NOW, ttl_days=30)
    assert called["n"] == 0 and out["refreshed"] == 0        # cache still fresh
    assert out["entities"]["itau"]["socios"][0]["name"] == "OLD"


def test_refresh_refetches_stale_cache():
    old = (NOW - dt.timedelta(days=40)).isoformat()
    prev = {"entities": {"itau": {"cnpj": "x", "fetched_at": old, "socios": []}}}
    out = wq.refresh([{"entity": "itau", "cnpj": "60701190"}], prev,
                     fetch=_fetch_ok, now=NOW, ttl_days=30)
    assert out["refreshed"] == 1
    assert out["entities"]["itau"]["socios"][0]["name"] == "MARIA SILVA"


def test_refresh_bounded_per_run():
    ents = [{"entity": f"e{i}", "cnpj": f"{i:08d}"} for i in range(20)]
    out = wq.refresh(ents, {}, fetch=_fetch_ok, now=NOW, max_lookups=5)
    assert out["refreshed"] == 5                              # spreads across runs


# --- #143 capital social rides the QSA fetch --------------------------------
def test_refresh_reads_capital_social_from_the_payload_it_already_fetched():
    def fetch(c):
        return {"qsa": [_socio("MARIA SILVA", "Diretor", "***265018**")],
                "capital_social": "45.000.000,00"}
    out = wq.refresh([{"entity": "itau", "cnpj": "60701190"}], {}, fetch=fetch, now=NOW)
    assert out["entities"]["itau"]["capital_social"] == 45_000_000.0
    assert out["entities"]["itau"]["socios"]           # QSA unaffected


def test_refresh_reports_which_entities_were_refetched():
    # The caller persists capital only for these — walking the whole watchlist would
    # mean a registry read per entity, the cost that timed out resolve_by_cnpj.
    ents = [{"entity": f"e{i}", "cnpj": f"{i:08d}"} for i in range(20)]
    out = wq.refresh(ents, {}, fetch=_fetch_ok, now=NOW, max_lookups=3)
    assert out["refreshed_entities"] == ["e0", "e1", "e2"]
    assert len(out["refreshed_entities"]) == out["refreshed"]


def test_refresh_tolerates_a_payload_with_no_capital():
    out = wq.refresh([{"entity": "itau", "cnpj": "60701190"}], {}, fetch=_fetch_ok, now=NOW)
    assert out["entities"]["itau"]["capital_social"] is None


def test_persist_capital_only_touches_entities_refetched_this_run(monkeypatch):
    # NB patch the function on the real module, not sys.modules: `from src.synth import
    # entity_registry` resolves via the already-imported package attribute, so a
    # sys.modules stub is bypassed whenever another test imported src.synth first.
    from src.synth import entity_registry
    seen = []

    def _rec(ent, value, source="enrich"):
        seen.append((ent, value))
        return {"entity": ent, "value": value, "previous": 1_000_000.0}

    monkeypatch.setattr(entity_registry, "record_capital_social", _rec)
    slice_ = {"refreshed_entities": ["a"],
              "entities": {"a": {"capital_social": 50_000_000.0},
                           "b": {"capital_social": 99_000_000.0}}}  # cached, not refetched
    writes, moves = wq._persist_capital(slice_)
    assert seen == [("a", 50_000_000.0)]
    assert writes == 1 and [m["entity"] for m in moves] == ["a"]


def test_persist_capital_never_breaks_the_qsa_run(monkeypatch):
    from src.synth import entity_registry

    def _boom(ent, value, source="enrich"):
        raise RuntimeError("registry down")

    monkeypatch.setattr(entity_registry, "record_capital_social", _boom)
    slice_ = {"refreshed_entities": ["a"], "entities": {"a": {"capital_social": 5e7}}}
    assert wq._persist_capital(slice_) == (0, [])


def test_persist_capital_reports_only_material_moves(monkeypatch):
    from src.synth import entity_registry

    def _rec(ent, value, source="enrich"):
        # a real write, but a trivial one: 1% on a large base
        return {"entity": ent, "value": 101_000_000.0, "previous": 100_000_000.0}

    monkeypatch.setattr(entity_registry, "record_capital_social", _rec)
    slice_ = {"refreshed_entities": ["a"], "entities": {"a": {"capital_social": 1.01e8}}}
    writes, moves = wq._persist_capital(slice_)
    assert writes == 1 and moves == []   # persisted, but not a signal
