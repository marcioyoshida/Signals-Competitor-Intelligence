import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import porter, swot_reconcile

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
    return _belief(entity, [
        _bullet("S", "Líder em pagamentos digitais no Brasil."),
        _bullet("W", "Alta dependência de receita de interchange."),
        _bullet("O", "Open banking amplia base de clientes potenciais."),
        _bullet("T", "Regulação do Pix pode comprimir margens."),
    ])


def _narratives(entity, n=5):
    return [
        {
            "id": f"n-{entity}-{i}",
            "entity": entity,
            "narrative": f"Narrative claim {i} about {entity} competitive dynamics in the Brazilian market.",
            "axis": "comparative",
            "threat_score": 0.5 + i * 0.05,
            "as_of": RUN,
        }
        for i in range(n)
    ]


def _draft_ok(label, industries, swot_bullets, evidence):
    """A well-formed draft returning assessments for 3 forces."""
    out = []
    if evidence:
        out.append({"force": "rivalry", "text": f"{label} enfrenta intensa concorrência.",
                    "intensity": "high", "confidence": 0.85, "evidence": [0]})
        out.append({"force": "new_entrants", "text": f"Fintechs ameaçam a posição de {label}.",
                    "intensity": "medium", "confidence": 0.7, "evidence": [1 % len(evidence)]})
        out.append({"force": "buyer_power", "text": "Clientes têm poder moderado de barganha.",
                    "intensity": "medium", "confidence": 0.6, "evidence": [0]})
    return out


def _draft_empty(label, industries, swot_bullets, evidence):
    return []


def _draft_low_conf(label, industries, swot_bullets, evidence):
    return [
        {"force": "rivalry", "text": "Low confidence rivalry.", "intensity": "low",
         "confidence": 0.2, "evidence": [0]},
    ]


def _draft_many(label, industries, swot_bullets, evidence):
    out = []
    for i in range(5):
        out.append({"force": "rivalry", "text": f"Rivalry assessment {i}.",
                    "intensity": "high", "confidence": 0.9, "evidence": [0]})
    return out


# --- analyze_forces ---

def test_analyze_forces_produces_proposals():
    beliefs = {"acme": _swot_beliefs("acme")}
    narr = _narratives("acme", 5)
    industries = {"acme": ["banking", "fintech"]}
    result = porter.analyze_forces(beliefs, narr, industries,
                                    run_date=RUN, draft_fn=_draft_ok,
                                    min_evidence=3)
    assert len(result) >= 2
    for p in result:
        assert p["framework"] == "porter"
        assert p["kind"] == "porter"
        assert p["entity"] == "acme"
        assert p["dimension"] in porter.DIMENSIONS
        assert p["status"] == "pending"
        assert p["evidence"]


def test_evidence_inherited():
    beliefs = {"acme": _swot_beliefs("acme")}
    narr = _narratives("acme", 5)
    result = porter.analyze_forces(beliefs, narr, {},
                                    run_date=RUN, draft_fn=_draft_ok,
                                    min_evidence=3)
    for p in result:
        assert all(isinstance(eid, str) for eid in p["evidence"])
        assert p["narrative_id"] == p["evidence"][0]


def test_entity_without_enough_signal_skipped():
    beliefs = {"thin": _belief("thin", [_bullet("S", "One bullet only.")])}
    narr = _narratives("thin", 1)
    result = porter.analyze_forces(beliefs, narr, {},
                                    run_date=RUN, draft_fn=_draft_ok,
                                    min_evidence=5)
    assert result == []


def test_already_proposed_skipped():
    beliefs = {"acme": _swot_beliefs("acme")}
    narr = _narratives("acme", 5)
    result = porter.analyze_forces(beliefs, narr, {},
                                    run_date=RUN, draft_fn=_draft_ok,
                                    already_proposed=frozenset({"acme"}),
                                    min_evidence=3)
    assert result == []


def test_max_entities_cap():
    beliefs = {}
    narr = []
    for i in range(12):
        ent = f"ent_{i}"
        beliefs[ent] = _swot_beliefs(ent)
        narr.extend(_narratives(ent, 5))
    result = porter.analyze_forces(beliefs, narr, {},
                                    run_date=RUN, draft_fn=_draft_ok,
                                    max_entities=3, min_evidence=3)
    entities = set(p["entity"] for p in result)
    assert len(entities) <= 3


def test_low_confidence_dropped():
    beliefs = {"acme": _swot_beliefs("acme")}
    narr = _narratives("acme", 5)
    result = porter.analyze_forces(beliefs, narr, {},
                                    run_date=RUN, draft_fn=_draft_low_conf,
                                    min_evidence=3, min_conf=0.5)
    assert result == []


def test_empty_draft_no_proposals():
    beliefs = {"acme": _swot_beliefs("acme")}
    narr = _narratives("acme", 5)
    result = porter.analyze_forces(beliefs, narr, {},
                                    run_date=RUN, draft_fn=_draft_empty,
                                    min_evidence=3)
    assert result == []


def test_max_per_force_enforced_by_parse():
    """MAX_PER_FORCE is enforced at the _parse_draft level (not analyze_forces)."""
    import json
    forces = [
        {"force": "rivalry", "text": f"Rivalry assessment {i} in this market.",
         "intensity": "high", "confidence": 0.8, "evidence": [0]}
        for i in range(5)
    ]
    parsed = porter._parse_draft(json.dumps({"forces": forces}), 3)
    assert len(parsed) == porter.MAX_PER_FORCE


# --- _parse_draft ---

def test_parse_draft_valid():
    raw = '{"forces":[{"force":"rivalry","text":"Intensa concorrência.","intensity":"high","confidence":0.8,"evidence":[0,1]},{"force":"substitutes","text":"Fintechs substituem.","intensity":"medium","confidence":0.7,"evidence":[2]}]}'
    result = porter._parse_draft(raw, 3)
    assert len(result) == 2
    assert result[0]["force"] == "rivalry"
    assert result[1]["force"] == "substitutes"


def test_parse_draft_none():
    assert porter._parse_draft(None, 5) == []


def test_parse_draft_garbage():
    assert porter._parse_draft("not json at all", 5) == []


def test_parse_draft_invalid_force():
    raw = '{"forces":[{"force":"unknown_force","text":"Something.","intensity":"high","confidence":0.8,"evidence":[0]}]}'
    result = porter._parse_draft(raw, 3)
    assert result == []


def test_parse_draft_no_evidence():
    raw = '{"forces":[{"force":"rivalry","text":"Something.","intensity":"high","confidence":0.8,"evidence":[]}]}'
    result = porter._parse_draft(raw, 3)
    assert result == []


def test_parse_draft_out_of_bounds_evidence():
    raw = '{"forces":[{"force":"rivalry","text":"Something.","intensity":"high","confidence":0.8,"evidence":[99]}]}'
    result = porter._parse_draft(raw, 3)
    assert result == []


def test_parse_draft_max_per_force():
    forces = [
        {"force": "rivalry", "text": f"Assessment {i}.", "intensity": "high",
         "confidence": 0.8, "evidence": [0]}
        for i in range(5)
    ]
    import json
    raw = json.dumps({"forces": forces})
    result = porter._parse_draft(raw, 3)
    assert len(result) == porter.MAX_PER_FORCE


def test_parse_draft_normalizes_intensity():
    raw = '{"forces":[{"force":"rivalry","text":"Something.","intensity":"EXTREME","confidence":0.8,"evidence":[0]}]}'
    result = porter._parse_draft(raw, 3)
    assert result[0]["intensity"] == "medium"


# --- eligible_entities ---

def test_eligible_entities_sorted_by_coverage():
    beliefs = {
        "few": _belief("few", [_bullet("S", "One.")]),
        "many": _swot_beliefs("many"),
    }
    narr_by_ent = {
        "few": _narratives("few", 4),
        "many": _narratives("many", 10),
    }
    result = porter.eligible_entities(beliefs, narr_by_ent, min_evidence=3)
    assert result[0] == "many"


def test_eligible_entities_excludes_already_proposed():
    beliefs = {"a": _swot_beliefs("a")}
    narr_by_ent = {"a": _narratives("a", 5)}
    result = porter.eligible_entities(beliefs, narr_by_ent,
                                       already_proposed=frozenset({"a"}),
                                       min_evidence=3)
    assert result == []


# --- _collect_evidence_ids ---

def test_collect_evidence_deduplicates():
    narr = [
        {"id": "n1", "narrative": "Same narrative claim about competition in the Brazilian banking sector.", "threat_score": 0.9, "as_of": RUN},
        {"id": "n2", "narrative": "Same narrative claim about competition in the Brazilian banking sector.", "threat_score": 0.8, "as_of": RUN},
        {"id": "n3", "narrative": "A completely different claim about new market entry from fintechs.", "threat_score": 0.7, "as_of": RUN},
    ]
    result = porter._collect_evidence_ids(narr)
    ids = [e["id"] for e in result]
    assert len(ids) == 2
    assert "n1" in ids
    assert "n3" in ids


# --- stable id ---

def test_stable_id():
    id1 = porter._bullet_id("acme", "rivalry", "Some text here.")
    id2 = porter._bullet_id("acme", "rivalry", "Some text here.")
    id3 = porter._bullet_id("acme", "rivalry", "Different text.")
    assert id1 == id2
    assert id1 != id3
    assert id1.startswith("porter:acme:rivalry:")


# --- merge preserves human status ---

def test_merge_preserves_human_decision():
    prev = [{"id": "p1", "status": "approved", "entity": "acme"}]
    new = [{"id": "p1", "status": "pending", "entity": "acme"}]
    merged = swot_reconcile.merge_proposals(prev, new)
    assert merged[0]["status"] == "approved"


# --- _active_swot ---

def test_active_swot_filters():
    belief = _belief("x", [
        _bullet("S", "Active S.", status="active"),
        _bullet("W", "Challenged W.", status="challenged"),
        _bullet("O", "Active O.", status="active"),
    ])
    result = porter._active_swot(belief)
    assert len(result) == 2
    assert all(b["status"] == "active" for b in result)
