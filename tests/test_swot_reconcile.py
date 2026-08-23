import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import embeddings, swot_reconcile, swot_store


# --- fakes ------------------------------------------------------------------
def _fake_embed(vectors):
    """Return an embed_fn that maps known texts to fixed vectors, else a zero-ish vec."""
    def embed_fn(texts):
        out = {}
        for t in texts:
            out[t] = vectors.get(t, [0.01, 0.01, 0.01])
        return out
    return embed_fn


def _news(entity, text, *, date, nid, threat=0.6):
    return {"id": nid, "kind": "entity_fusion", "entity": entity, "run_date": date,
            "narrative": text, "threat_score": threat, "lenses": ["news"]}


def _belief(entity, bullets):
    return {"entity": entity, "label": entity.title(),
            "bullets": [{"id": f"{entity}:{bk}:{dim}", "dimension": dim, "source_key": bk,
                         "text": txt, "status": "active"} for bk, dim, txt in bullets]}


# --- candidate selection ----------------------------------------------------
def test_select_candidates_only_todays_freetext_entities():
    narr = [
        _news("nubank", "Nubank enfrenta processo por vazamento de dados de clientes.",
              date="2026-08-23", nid="n1"),
        _news("itau", "Itaú anuncia expansão internacional relevante nesta data.",
              date="2026-08-22", nid="n2"),            # not today
        {"id": "d1", "axis": "silence", "entity": "stone", "run_date": "2026-08-23",
         "narrative": "Stone sem sinais recentes rastreáveis por vários dias seguidos.",
         "mode": "derived"},                            # derived axis excluded
        {"id": "n3", "kind": "news", "entity": None, "run_date": "2026-08-23",
         "narrative": "Mercado reage a decisão do Copom sobre juros nesta semana."},  # no entity
    ]
    got = swot_reconcile.select_candidates(narr, "2026-08-23")
    assert [n["id"] for n in got] == ["n1"]


def test_select_candidates_orders_by_threat_and_caps():
    narr = [_news("a", "Texto suficientemente longo para passar do piso." + str(i),
                  date="2026-08-23", nid=f"n{i}", threat=i / 10) for i in range(10)]
    got = swot_reconcile.select_candidates(narr, "2026-08-23", max_candidates=3)
    assert [n["id"] for n in got] == ["n9", "n8", "n7"]


# --- reconcile core ---------------------------------------------------------
def test_reinforce_auto_applies_as_evidence_record():
    claim = "Nubank supera concorrentes em rentabilidade no trimestre."
    beliefs = {"nubank": _belief("nubank", [
        ("peer:banking", "S", "Supera os pares em banking — força competitiva.")])}
    narr = [_news("nubank", claim, date="2026-08-23", nid="n1")]
    # claim vector == bullet vector -> cosine 1.0, so it's the top-k near bullet
    vec = [1.0, 0.0, 0.0]
    embed_fn = _fake_embed({claim: vec,
                            "Supera os pares em banking — força competitiva.": vec})

    def stance_fn(label, c, bullets):
        return {"matches": [{"i": 0, "stance": "reinforces", "confidence": 0.9}],
                "new_bullet": None}

    reinf, props = swot_reconcile.reconcile(
        narr, beliefs, run_date="2026-08-23", embed_fn=embed_fn, stance_fn=stance_fn)
    assert props == []
    assert len(reinf) == 1
    assert reinf[0]["base_key"] == "peer:banking"
    assert reinf[0]["dimension"] == "S"
    assert reinf[0]["narrative_id"] == "n1"


def test_contradict_and_new_become_proposals_not_applied():
    claim = "Nubank perde participação e recua frente aos pares no período."
    beliefs = {"nubank": _belief("nubank", [
        ("peer:banking", "S", "Supera os pares em banking — força competitiva.")])}
    narr = [_news("nubank", claim, date="2026-08-23", nid="n1")]
    vec = [1.0, 0.0, 0.0]
    embed_fn = _fake_embed({claim: vec,
                            "Supera os pares em banking — força competitiva.": vec})

    def stance_fn(label, c, bullets):
        return {"matches": [{"i": 0, "stance": "contradicts", "confidence": 0.85}],
                "new_bullet": None}

    reinf, props = swot_reconcile.reconcile(
        narr, beliefs, run_date="2026-08-23", embed_fn=embed_fn, stance_fn=stance_fn)
    assert reinf == []
    assert len(props) == 1 and props[0]["kind"] == "challenge"
    assert props[0]["target_bullet_id"] == "nubank:peer:banking:S"
    assert props[0]["status"] == "pending"


def test_new_bullet_proposed_when_unrelated():
    claim = "Nubank sofre ataque cibernético com vazamento de dados de clientes."
    beliefs = {"nubank": _belief("nubank", [
        ("peer:banking", "S", "Supera os pares em banking.")])}
    narr = [_news("nubank", claim, date="2026-08-23", nid="n1")]
    embed_fn = _fake_embed({claim: [1.0, 0.0, 0.0],
                            "Supera os pares em banking.": [0.0, 1.0, 0.0]})  # orthogonal

    def stance_fn(label, c, bullets):
        return {"matches": [{"i": 0, "stance": "unrelated", "confidence": 0.2}] if bullets else [],
                "new_bullet": {"dimension": "W", "text": "Exposta a falhas de segurança de dados.",
                               "confidence": 0.8}}

    reinf, props = swot_reconcile.reconcile(
        narr, beliefs, run_date="2026-08-23", embed_fn=embed_fn, stance_fn=stance_fn)
    assert reinf == []
    assert len(props) == 1 and props[0]["kind"] == "new"
    assert props[0]["dimension"] == "W"
    assert props[0]["id"] == "new:nubank:n1"


def test_low_confidence_is_ignored():
    claim = "Nubank supera concorrentes em rentabilidade no trimestre."
    beliefs = {"nubank": _belief("nubank", [("peer:banking", "S", "Supera os pares.")])}
    narr = [_news("nubank", claim, date="2026-08-23", nid="n1")]
    vec = [1.0, 0.0, 0.0]
    embed_fn = _fake_embed({claim: vec, "Supera os pares.": vec})

    def stance_fn(label, c, bullets):
        return {"matches": [{"i": 0, "stance": "reinforces", "confidence": 0.3}],
                "new_bullet": None}

    reinf, props = swot_reconcile.reconcile(
        narr, beliefs, run_date="2026-08-23", embed_fn=embed_fn, stance_fn=stance_fn)
    assert reinf == [] and props == []


# --- stance JSON parsing ----------------------------------------------------
def test_parse_stance_defensive():
    raw = 'noise {"matches":[{"i":0,"stance":"reinforces","confidence":0.9},' \
          '{"i":5,"stance":"contradicts","confidence":0.9}],' \
          '"new_bullet":{"dimension":"O","text":"Janela de mercado.","confidence":0.7}} tail'
    out = swot_reconcile._parse_stance(raw, n_bullets=1)
    assert out["matches"] == [{"i": 0, "stance": "reinforces", "confidence": 0.9}]  # i=5 dropped
    assert out["new_bullet"]["dimension"] == "O"
    assert swot_reconcile._parse_stance("not json", 3) == {"matches": [], "new_bullet": None}
    assert swot_reconcile._parse_stance(None, 3) == {"matches": [], "new_bullet": None}


# --- idempotent stores ------------------------------------------------------
def test_merge_proposals_dedupes_and_preserves_status():
    existing = [{"id": "new:a:n1", "kind": "new", "status": "accepted", "created": "2026-08-20",
                 "date": "2026-08-20"}]
    fresh = [{"id": "new:a:n1", "kind": "new", "status": "pending", "created": "2026-08-23",
              "date": "2026-08-23"},
             {"id": "new:a:n2", "kind": "new", "status": "pending", "created": "2026-08-23",
              "date": "2026-08-23"}]
    out = swot_reconcile.merge_proposals(existing, fresh)
    by_id = {p["id"]: p for p in out}
    assert len(out) == 2
    assert by_id["new:a:n1"]["status"] == "accepted"       # human decision preserved
    assert by_id["new:a:n1"]["created"] == "2026-08-20"    # original created preserved
    assert by_id["new:a:n2"]["status"] == "pending"


def test_merge_reinforcements_dedupes():
    existing = [{"narrative_id": "n1", "bullet_id": "b1", "date": "2026-08-20"}]
    fresh = [{"narrative_id": "n1", "bullet_id": "b1", "date": "2026-08-23"},   # dup key
             {"narrative_id": "n2", "bullet_id": "b1", "date": "2026-08-23"}]
    out = swot_reconcile.merge_reinforcements(existing, fresh)
    assert len(out) == 2
    n1 = [r for r in out if r["narrative_id"] == "n1"][0]
    assert n1["date"] == "2026-08-23"  # fresh wins


# --- fold into the belief store ---------------------------------------------
def test_swot_store_folds_reinforcements_as_news_evidence():
    # A comparative axis asserts the S bullet; a reinforcement adds news evidence.
    comp = {"id": "comparative-nubank", "axis": "comparative", "entity": "nubank",
            "run_date": "2026-08-22", "entity_label": "Nubank",
            "swot_hint": {"dimension": "S", "cohort": "banking"}}
    reinf = [{"entity": "nubank", "base_key": "peer:banking", "dimension": "S",
              "narrative_id": "news-1", "date": "2026-08-23", "stance_conf": 0.9}]
    beliefs = swot_store.build_beliefs([comp], as_of="2026-08-23", reinforcements=reinf)
    bullets = beliefs["nubank"]["bullets"]
    s = [b for b in bullets if b["dimension"] == "S"][0]
    assert s["evidence_count"] == 2
    assert s["news_evidence"] == 1
    assert s["latest"] == "2026-08-23"


def test_reinforcement_without_matching_bullet_is_ignored():
    comp = {"id": "comparative-nubank", "axis": "comparative", "entity": "nubank",
            "run_date": "2026-08-22", "swot_hint": {"dimension": "S", "cohort": "banking"}}
    reinf = [{"entity": "nubank", "base_key": "theme:pix", "dimension": "O",
              "narrative_id": "news-1", "date": "2026-08-23", "stance_conf": 0.9}]
    beliefs = swot_store.build_beliefs([comp], as_of="2026-08-23", reinforcements=reinf)
    # no O bullet created from a stray reinforcement
    assert all(b["dimension"] != "O" for b in beliefs["nubank"]["bullets"])


# --- embeddings helpers -----------------------------------------------------
def test_cosine_and_top_k():
    assert swot_reconcile.embeddings is embeddings
    assert embeddings.cosine([1, 0], [1, 0]) == 1.0
    assert embeddings.cosine([1, 0], [0, 1]) == 0.0
    assert embeddings.cosine([1, 0], None) == 0.0
    ranked = embeddings.top_k([1, 0], [("a", [1, 0]), ("b", [0, 1]), ("c", [0.9, 0.1])],
                              k=2, min_sim=0.1)
    assert [item for item, _ in ranked] == ["a", "c"]


def test_embed_texts_uses_cache(monkeypatch):
    calls = []

    def fake_embed(text, *, model_id=None):
        calls.append(text)
        return [float(len(text)), 1.0]

    monkeypatch.setattr(embeddings, "embed", fake_embed)
    cache = {}
    out1 = embeddings.embed_texts(["alpha", "beta"], cache=cache)
    out2 = embeddings.embed_texts(["alpha", "beta"], cache=cache)  # served from cache
    assert set(out1) == {"alpha", "beta"} and out1 == out2
    assert sorted(calls) == ["alpha", "beta"]  # embedded once each
