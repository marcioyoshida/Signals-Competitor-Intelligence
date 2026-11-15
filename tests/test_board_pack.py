"""#323 Shared Context Pack: frozen, hashed, every fact with source and as_of."""
import json

import pytest

from src.board import pack as P


def _bank(eid, label, share, basel=15.0, npl=2.5):
    return {"entity": eid, "label": label, "industries": ["banking"], "market_share_pct": share,
            "pilar3_km1": {"basileia_pct": basel, "leverage_pct": 7.0, "lcr_pct": 150.0,
                           "nsfr_pct": 120.0, "trimestre": "2026-3"},
            "soundness": {"indice_basileia": basel, "capital_principal": 11.0, "band": "sólido",
                          "base_date": 202606},
            "inadimplencia": {"npl_total": npl, "band": "baixa", "base_date": 202606}}


def feed():
    return {
        "generated_at": "2026-11-14T20:48:10+00:00", "as_of": "2026-11-14",
        "entities": [_bank("alfa", "Banco Alfa", 10), _bank("beta", "Banco Beta", 8),
                     _bank("gama", "Banco Gama", 5), {"entity": "loja", "label": "Loja",
                                                       "industries": ["retail"]}],
        "market_structure": {"banking": {"constituents": [
            {"entity": "beta"}, {"entity": "ghost"}, {"entity": "alfa"}, {"entity": "gama"}]}},
        "distress": [
            {"entity": "gama", "kind": "liquidacao_extrajudicial", "label": "Liquidação Extrajudicial",
             "confidence": "regulatory", "role": "self", "last_seen": "2026-11-12",
             "latest_title": "BCB decreta liquidação do Banco Gama", "sources": [{"source": "BCB-Regimes"}]},
            {"entity": "beta", "kind": "liquidacao_extrajudicial", "label": "Correspondente em liquidação",
             "confidence": "corroborated", "role": "correspondent", "correspondent": "AGENTE Z LTDA",
             "id": "d1:corr:beta", "last_seen": "2026-11-12", "latest_title": "t", "sources": []},
            {"entity": "alfa", "kind": "recuperacao_judicial", "confidence": "reported", "role": "self",
             "last_seen": "2026-11-01", "latest_title": "boato", "sources": []},
        ],
        "macro": {"selic": {"current": 13.75, "as_of": "2026-11-14",
                            "last_decision": {"date": "2026-09-17", "previous": 14.0, "value": 13.75}}},
        "feed": [{"id": f"n{i}", "kind": "competitor:news", "entities": ["alfa"], "date": f"2026-11-0{i}",
                  "narrative": "x" * 900} for i in range(1, 9)]
                + [{"id": "fus", "kind": "entity_fusion", "entities": ["alfa"], "date": "2026-11-09",
                    "narrative": "bookkeeping"}],
    }


def test_peers_are_ranked_same_industry_entities_that_exist():
    assert P.choose_peers(feed(), "alfa") == ["beta", "gama"]  # "ghost" is not in entities
    assert P.choose_peers(feed(), "nobody") == []


def test_unknown_target_is_refused():
    with pytest.raises(ValueError):
        P.build_pack(feed(), "ghost")


def test_every_fact_has_id_source_as_of_and_unique_ids():
    p = P.build_pack(feed(), "alfa")
    ids = [f["id"] for f in p["facts"]]
    assert len(ids) == len(set(ids))
    for f in p["facts"]:
        assert f["id"].startswith("f-") and f["source"] and f["text"]
    assert {"f-km1-alfa", "f-soundness-beta", "f-npl-gama", "f-macro-selic"} <= set(ids)


def test_distress_trust_gate_and_third_party_wording():
    p = P.build_pack(feed(), "alfa")
    by = {f["id"]: f for f in p["facts"]}
    assert "f-distress-gama-liquidacao-extrajudicial" in by          # official regime: kept
    tp = [f for f in p["facts"] if f["kind"] == "third_party"]
    assert len(tp) == 1 and "Não é um regime sobre beta" in tp[0]["text"]
    assert not any("boato" in f["text"] for f in p["facts"])         # single-outlet report dropped
    assert any("fonte única" in g for g in p["gaps"])


def test_narratives_are_capped_trimmed_and_skip_bookkeeping():
    p = P.build_pack(feed(), "alfa")
    nar = [f for f in p["facts"] if f["kind"] == "narrative"]
    assert len(nar) == P.MAX_NARRATIVES and all(len(f["text"]) <= P.NARRATIVE_CHARS + 1 for f in nar)
    assert not any(f["id"] == "f-narrative-fus" for f in nar)


def test_size_cap_drops_lowest_priority_first_and_says_so():
    p = P.build_pack(feed(), "alfa", max_chars=1500)
    kinds = {f["kind"] for f in p["facts"]}
    assert "narrative" not in kinds and "km1" in kinds
    assert len(P.render(p["facts"])) <= 1500
    assert any(g.startswith("omitido por limite") for g in p["gaps"])


def test_seal_is_deterministic_and_put_get_round_trips():
    a, b = P.seal(P.build_pack(feed(), "alfa")), P.seal(P.build_pack(feed(), "alfa"))
    assert a["sha256"] == b["sha256"] and len(a["sha256"]) == 64
    assert P.fact_ids(a) == {f["id"] for f in a["facts"]}

    class S3:
        store: dict = {}

        def put_object(self, Bucket, Key, Body, ContentType, IfNoneMatch):
            if Key in self.store:
                err = Exception("exists")
                err.response = {"Error": {"Code": "PreconditionFailed"}}
                raise err
            self.store[Key] = Body

        def get_object(self, Bucket, Key):
            body = self.store[Key]
            return {"Body": type("B", (), {"read": lambda self: body})()}

    s3 = S3()
    key = P.put_pack(s3, "bkt", a)
    assert key == f"board/packs/{a['sha256']}.json"
    assert P.put_pack(s3, "bkt", a) == key                     # immutable: second put is a no-op
    assert P.get_pack(s3, "bkt", a["sha256"]) == a
    s3.store[key] = s3.store[key].replace(b"Banco Alfa", b"Banco Xyz")
    with pytest.raises(ValueError):
        P.get_pack(s3, "bkt", a["sha256"])


def test_unsealed_or_tampered_pack_is_not_stored():
    p = P.build_pack(feed(), "alfa")
    with pytest.raises(ValueError):
        P.put_pack(object(), "bkt", p)
    with pytest.raises(ValueError):
        P.put_pack(object(), "bkt", {**P.seal(p), "target": "beta"})


def test_context_text_lists_gaps_and_citable_lines():
    p = P.seal(P.build_pack(feed(), "alfa"))
    txt = P.context_text(p)
    assert txt.startswith(f"PACOTE DE CONTEXTO {p['sha256']}") and "LACUNAS:" in txt
    assert "[f-km1-alfa] (BCB Pilar 3 (KM1), 2026-3)" in txt
    json.dumps(p)  # JSON-serialisable
