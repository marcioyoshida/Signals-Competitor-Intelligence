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
