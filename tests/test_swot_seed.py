import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import swot_seed

RUN = "2026-08-23"


def _narr(entity, text, *, nid, threat=0.6, date=RUN, axis=None, mode=None):
    n = {"id": nid, "entity": entity, "narrative": text, "threat_score": threat,
         "run_date": date, "lenses": ["news"]}
    if axis:
        n["axis"] = axis
    if mode:
        n["mode"] = mode
    return n


def _history(entity, n, *, threat=0.6):
    return [_narr(entity, f"{entity.title()} anunciou o movimento competitivo número {i} "
                          f"no mercado brasileiro de serviços financeiros.", nid=f"{entity}-{i}",
                  threat=threat)
            for i in range(n)]


def _draft_ok(label, dossier, evidence):
    # A well-formed, evidence-linked draft: one S grounded in items 0 and 1.
    return [{"dimension": "S", "text": f"{label} mostra força competitiva sustentada.",
             "confidence": 0.8, "evidence": [0, 1]},
            {"dimension": "T", "text": f"{label} enfrenta pressão regulatória crescente.",
             "confidence": 0.7, "evidence": [0]}]


def test_seeds_thin_grounded_entity():
    narrs = _history("nubank", 5)
    props = swot_seed.seed_entities(narrs, {}, {}, run_date=RUN, draft_fn=_draft_ok)
    assert {p["dimension"] for p in props} == {"S", "T"}
    assert all(p["kind"] == "seed" and p["status"] == "pending" for p in props)
    assert all(p["entity"] == "nubank" for p in props)
    # evidence-linked to real narrative ids
    assert all(p["evidence"] and set(p["evidence"]) <= {f"nubank-{i}" for i in range(5)}
               for p in props)


def test_thin_history_is_not_grounded():
    # fewer than MIN_HISTORY activity narratives -> no draft (no hallucination)
    narrs = _history("stone", swot_seed.MIN_HISTORY - 1)
    assert swot_seed.seed_entities(narrs, {}, {}, run_date=RUN, draft_fn=_draft_ok) == []


def test_well_covered_entity_is_skipped():
    # a belief store already above the cold-start floor -> left to bottom-up growth
    narrs = _history("itau", 6)
    beliefs = {"itau": {"bullets": [{"status": "active"} for _ in range(swot_seed.MAX_EXISTING + 1)]}}
    assert swot_seed.seed_entities(narrs, beliefs, {}, run_date=RUN, draft_fn=_draft_ok) == []


def test_already_seeded_entity_is_not_redrafted():
    narrs = _history("picpay", 5)
    props = swot_seed.seed_entities(narrs, {}, {}, run_date=RUN, draft_fn=_draft_ok,
                                    already_seeded=frozenset({"picpay"}))
    assert props == []


def test_derived_axis_cards_do_not_ground_a_seed():
    # inference/derived cards are not activity -> don't count toward min_history
    narrs = [_narr("inter", "Ausência de sinais recentes.", nid=f"s-{i}",
                   axis="silence", mode="derived") for i in range(6)]
    assert swot_seed.seed_entities(narrs, {}, {}, run_date=RUN, draft_fn=_draft_ok) == []


def test_bullets_without_valid_evidence_are_dropped():
    def draft_no_evidence(label, dossier, evidence):
        return [{"dimension": "S", "text": "Alegação sem base.", "confidence": 0.9,
                 "evidence": [999]}]  # out-of-range index -> no valid evidence
    narrs = _history("c6", 5)
    assert swot_seed.seed_entities(narrs, {}, {}, run_date=RUN, draft_fn=draft_no_evidence) == []


def test_low_confidence_drafts_are_dropped():
    def draft_low(label, dossier, evidence):
        return [{"dimension": "W", "text": "Fraqueza incerta.", "confidence": 0.2,
                 "evidence": [0]}]
    narrs = _history("caixa", 5)
    assert swot_seed.seed_entities(narrs, {}, {}, run_date=RUN, draft_fn=draft_low) == []


def test_max_entities_cap():
    narrs = []
    for k in range(swot_seed.MAX_ENTITIES + 3):
        narrs += _history(f"ent{k}", 4)
    props = swot_seed.seed_entities(narrs, {}, {}, run_date=RUN, draft_fn=_draft_ok)
    assert len({p["entity"] for p in props}) == swot_seed.MAX_ENTITIES


def test_most_grounded_entities_win_the_cap():
    # deepest histories should be the ones drafted when capped
    narrs = _history("shallow", swot_seed.MIN_HISTORY)
    for k in range(swot_seed.MAX_ENTITIES):
        narrs += _history(f"deep{k}", 8)
    props = swot_seed.seed_entities(narrs, {}, {}, run_date=RUN, draft_fn=_draft_ok)
    assert "shallow" not in {p["entity"] for p in props}


def test_max_per_dimension_enforced_in_parse():
    raw = ('{"bullets":[' +
           ','.join('{"dimension":"S","text":"forca %d","confidence":0.8,"evidence":[0]}' % i
                    for i in range(5)) + ']}')
    parsed = swot_seed._parse_draft(raw, n_evidence=3)
    assert len(parsed) == swot_seed.MAX_PER_DIM


def test_parse_draft_defensive_on_garbage():
    assert swot_seed._parse_draft(None, 3) == []
    assert swot_seed._parse_draft("not json", 3) == []
    assert swot_seed._parse_draft('{"bullets":[{"dimension":"X","text":"t","evidence":[0]}]}', 3) == []


def test_stable_id_dedup_across_runs():
    narrs = _history("mercadopago", 5)
    a = swot_seed.seed_entities(narrs, {}, {}, run_date=RUN, draft_fn=_draft_ok)
    b = swot_seed.seed_entities(narrs, {}, {}, run_date="2026-08-24", draft_fn=_draft_ok)
    merged = swot_reconcile_merge(a, b)
    # same texts -> same ids -> no growth on the second run
    assert len(merged) == len(a)


def swot_reconcile_merge(a, b):
    from src.synth import swot_reconcile
    return swot_reconcile.merge_proposals(a, b)


def test_merge_preserves_human_status():
    from src.synth import swot_reconcile
    narrs = _history("bmg", 5)
    first = swot_seed.seed_entities(narrs, {}, {}, run_date=RUN, draft_fn=_draft_ok)
    accepted = [{**first[0], "status": "approved"}]
    merged = swot_reconcile.merge_proposals(accepted, first)
    kept = next(p for p in merged if p["id"] == first[0]["id"])
    assert kept["status"] == "approved"


def test_collect_evidence_dedups_and_caps():
    narrs = ([_narr("x", "Mesma manchete repetida diariamente no mercado.", nid=f"d-{i}")
              for i in range(20)] +
             [_narr("x", "Uma segunda manchete distinta sobre a empresa.", nid="u-1")])
    ev = swot_seed.collect_evidence(narrs, max_claims=5)
    assert len(ev) == 2  # deduped by claim text
