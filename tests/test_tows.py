import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import tows, swot_reconcile

RUN = "2026-08-23"


def _belief(entity, bullets):
    return {
        "entity": entity,
        "label": entity.title(),
        "bullets": bullets,
    }


def _bullet(dim, text, *, status="active", bid=None, evidence=None):
    return {
        "id": bid or f"{dim}:{text[:8]}",
        "dimension": dim,
        "text": text,
        "status": status,
        "source_key": f"key:{dim}",
        "evidence": evidence or [{"id": f"ev-{dim}-1", "date": RUN}],
    }


def _swot_beliefs(entity):
    """A minimal SWOT belief set: one S, one W, one O, one T."""
    return _belief(entity, [
        _bullet("S", "Líder em pagamentos digitais no Brasil."),
        _bullet("W", "Alta dependência de receita de interchange."),
        _bullet("O", "Open banking amplia base de clientes potenciais."),
        _bullet("T", "Regulação do Pix pode comprimir margens."),
    ])


def _draft_ok(label, by_dim):
    """A well-formed draft: one SO and one ST posture, referencing correct indices."""
    s_bullets = by_dim.get("S", [])
    o_bullets = by_dim.get("O", [])
    t_bullets = by_dim.get("T", [])
    out = []
    # S is index 0, W is index 1, O is index 2, T is index 3 (in the all_bullets list)
    if s_bullets and o_bullets:
        out.append({"quadrant": "SO",
                    "text": f"{label} pode alavancar pagamentos para capturar open banking.",
                    "confidence": 0.8, "pairs": [0, 2]})
    if s_bullets and t_bullets:
        out.append({"quadrant": "ST",
                    "text": f"{label} usa escala em pagamentos para absorver pressão regulatória.",
                    "confidence": 0.75, "pairs": [0, 3]})
    return out


def test_pair_postures_produces_proposals():
    beliefs = {"nubank": _swot_beliefs("nubank")}
    props = tows.pair_postures(beliefs, run_date=RUN, draft_fn=_draft_ok)
    assert len(props) == 2
    assert {p["dimension"] for p in props} == {"SO", "ST"}
    assert all(p["kind"] == "tows" for p in props)
    assert all(p["framework"] == "tows" for p in props)
    assert all(p["status"] == "pending" for p in props)
    assert all(p["entity"] == "nubank" for p in props)


def test_tows_inherits_evidence():
    beliefs = {"nubank": _swot_beliefs("nubank")}
    props = tows.pair_postures(beliefs, run_date=RUN, draft_fn=_draft_ok)
    so = next(p for p in props if p["dimension"] == "SO")
    assert "ev-S-1" in so["evidence"]
    assert "ev-O-1" in so["evidence"]
    assert len(so["paired_beliefs"]) >= 2


def test_entity_without_internal_bullets_is_skipped():
    beliefs = {"ent": _belief("ent", [
        _bullet("O", "Oportunidade."),
        _bullet("T", "Ameaça."),
    ])}
    props = tows.pair_postures(beliefs, run_date=RUN, draft_fn=_draft_ok)
    assert props == []


def test_entity_without_external_bullets_is_skipped():
    beliefs = {"ent": _belief("ent", [
        _bullet("S", "Força."),
        _bullet("W", "Fraqueza."),
    ])}
    props = tows.pair_postures(beliefs, run_date=RUN, draft_fn=_draft_ok)
    assert props == []


def test_challenged_bullets_are_excluded():
    beliefs = {"ent": _belief("ent", [
        _bullet("S", "Força contestada.", status="challenged"),
        _bullet("O", "Oportunidade."),
    ])}
    assert tows.eligible_entities(beliefs) == []


def test_already_proposed_entity_is_skipped():
    beliefs = {"nubank": _swot_beliefs("nubank")}
    props = tows.pair_postures(beliefs, run_date=RUN, draft_fn=_draft_ok,
                               already_proposed=frozenset({"nubank"}))
    assert props == []


def test_max_entities_cap():
    beliefs = {}
    for i in range(tows.MAX_ENTITIES + 3):
        beliefs[f"ent{i}"] = _swot_beliefs(f"ent{i}")
    props = tows.pair_postures(beliefs, run_date=RUN, draft_fn=_draft_ok)
    assert len({p["entity"] for p in props}) == tows.MAX_ENTITIES


def test_low_confidence_postures_dropped():
    def draft_low(label, by_dim):
        return [{"quadrant": "SO", "text": "Postura incerta.",
                 "confidence": 0.2, "pairs": [0, 2]}]
    beliefs = {"x": _swot_beliefs("x")}
    assert tows.pair_postures(beliefs, run_date=RUN, draft_fn=draft_low) == []


def test_posture_with_fewer_than_two_pairs_dropped():
    def draft_single_pair(label, by_dim):
        return [{"quadrant": "SO", "text": "Postura mal referenciada.",
                 "confidence": 0.8, "pairs": [0]}]
    beliefs = {"x": _swot_beliefs("x")}
    assert tows.pair_postures(beliefs, run_date=RUN, draft_fn=draft_single_pair) == []


def test_parse_draft_defensive():
    assert tows._parse_draft(None, 4) == []
    assert tows._parse_draft("not json", 4) == []
    assert tows._parse_draft('{"postures":[{"quadrant":"X","text":"t","pairs":[0,1]}]}', 4) == []


def test_parse_draft_valid():
    raw = ('{"postures":[{"quadrant":"SO","text":"postura","confidence":0.8,'
           '"pairs":[0,2]},{"quadrant":"WT","text":"evitar","confidence":0.7,'
           '"pairs":[1,3]}]}')
    parsed = tows._parse_draft(raw, 4)
    assert len(parsed) == 2
    assert parsed[0]["quadrant"] == "SO"
    assert parsed[1]["quadrant"] == "WT"


def test_max_per_quadrant_enforced():
    raw = ('{"postures":[' +
           ','.join('{"quadrant":"SO","text":"postura %d","confidence":0.8,"pairs":[0,2]}' % i
                    for i in range(5)) + ']}')
    parsed = tows._parse_draft(raw, 4)
    assert len(parsed) == tows.MAX_PER_QUAD


def test_stable_id_dedup():
    beliefs = {"nubank": _swot_beliefs("nubank")}
    a = tows.pair_postures(beliefs, run_date=RUN, draft_fn=_draft_ok)
    b = tows.pair_postures(beliefs, run_date="2026-08-24", draft_fn=_draft_ok)
    merged = swot_reconcile.merge_proposals(a, b)
    assert len(merged) == len(a)


def test_merge_preserves_human_status():
    beliefs = {"nubank": _swot_beliefs("nubank")}
    first = tows.pair_postures(beliefs, run_date=RUN, draft_fn=_draft_ok)
    approved = [{**first[0], "status": "approved"}]
    merged = swot_reconcile.merge_proposals(approved, first)
    kept = next(p for p in merged if p["id"] == first[0]["id"])
    assert kept["status"] == "approved"


def test_eligible_entities_sorted_by_coverage():
    beliefs = {
        "thin": _belief("thin", [_bullet("S", "S"), _bullet("O", "O")]),
        "rich": _swot_beliefs("rich"),
    }
    elig = tows.eligible_entities(beliefs)
    assert elig[0] == "rich"


def test_collect_evidence_deduplicates():
    bullets = [
        {"evidence": [{"id": "a"}, {"id": "b"}]},
        {"evidence": [{"id": "b"}, {"id": "c"}]},
    ]
    assert tows._collect_evidence(bullets) == ["a", "b", "c"]
