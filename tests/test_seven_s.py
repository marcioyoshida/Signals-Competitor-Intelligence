import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import seven_s

RUN = "2026-08-23"


def _belief(entity, bullets):
    return {"entity": entity, "label": entity.title(), "bullets": bullets}


def _bullet(dim, text, *, status="active", evidence=None):
    return {
        "id": f"{dim}:{text[:8]}", "dimension": dim, "text": text,
        "status": status, "source_key": f"key:{dim}",
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
        {"id": f"n-{entity}-{i}", "entity": entity,
         "narrative": f"Narrative claim {i} about {entity} organizational structure in the Brazilian market.",
         "axis": "comparative", "threat_score": 0.5 + i * 0.05, "as_of": RUN}
        for i in range(n)
    ]


def _draft_ok(label, industries, swot_bullets, evidence):
    out = []
    if evidence:
        out.append({"element": "structure", "text": f"{label} opera com estrutura holding.",
                    "confidence": 0.85, "evidence": [0]})
        out.append({"element": "systems", "text": "Plataforma digital proprietária consolidada.",
                    "confidence": 0.7, "evidence": [1 % len(evidence)]})
    return out


def _draft_empty(label, industries, swot_bullets, evidence):
    return []


def test_analyze_visible_produces_proposals():
    beliefs = {"acme": _swot_beliefs("acme")}
    narr = _narratives("acme", 5)
    result = seven_s.analyze_visible(beliefs, narr, {},
                                      run_date=RUN, draft_fn=_draft_ok, min_evidence=3)
    assert len(result) >= 2
    for p in result:
        assert p["framework"] == "seven_s"
        assert p["kind"] == "seven_s"
        assert p["dimension"] in seven_s.DIMENSIONS
        assert p["status"] == "pending"


def test_entity_without_enough_signal_skipped():
    beliefs = {"thin": _belief("thin", [_bullet("S", "One.")])}
    narr = _narratives("thin", 1)
    result = seven_s.analyze_visible(beliefs, narr, {},
                                      run_date=RUN, draft_fn=_draft_ok, min_evidence=5)
    assert result == []


def test_already_proposed_skipped():
    beliefs = {"acme": _swot_beliefs("acme")}
    narr = _narratives("acme", 5)
    result = seven_s.analyze_visible(beliefs, narr, {},
                                      run_date=RUN, draft_fn=_draft_ok,
                                      already_proposed=frozenset({"acme"}), min_evidence=3)
    assert result == []


def test_empty_draft_no_proposals():
    beliefs = {"acme": _swot_beliefs("acme")}
    narr = _narratives("acme", 5)
    result = seven_s.analyze_visible(beliefs, narr, {},
                                      run_date=RUN, draft_fn=_draft_empty, min_evidence=3)
    assert result == []


def test_parse_draft_valid():
    raw = '{"elements":[{"element":"structure","text":"Holding.","confidence":0.8,"evidence":[0]}]}'
    result = seven_s._parse_draft(raw, 3)
    assert len(result) == 1
    assert result[0]["element"] == "structure"


def test_parse_draft_none():
    assert seven_s._parse_draft(None, 5) == []


def test_parse_draft_garbage():
    assert seven_s._parse_draft("not json", 5) == []


def test_parse_draft_rejects_hidden_dimensions():
    """7S guardrail: shared_values, style, skills, staff must be dropped."""
    raw = '{"elements":[{"element":"shared_values","text":"X.","confidence":0.9,"evidence":[0]},{"element":"style","text":"Y.","confidence":0.9,"evidence":[0]},{"element":"skills","text":"Z.","confidence":0.9,"evidence":[0]},{"element":"structure","text":"Holding.","confidence":0.8,"evidence":[0]}]}'
    result = seven_s._parse_draft(raw, 3)
    assert len(result) == 1
    assert result[0]["element"] == "structure"


def test_parse_draft_max_per_dim():
    elements = [{"element": "structure", "text": f"Elem {i}.", "confidence": 0.8, "evidence": [0]}
                for i in range(5)]
    raw = json.dumps({"elements": elements})
    result = seven_s._parse_draft(raw, 3)
    assert len(result) == seven_s.MAX_PER_DIM


def test_stable_id():
    id1 = seven_s._bullet_id("acme", "structure", "Some text.")
    id2 = seven_s._bullet_id("acme", "structure", "Some text.")
    id3 = seven_s._bullet_id("acme", "structure", "Different.")
    assert id1 == id2
    assert id1 != id3
    assert id1.startswith("seven_s:acme:structure:")
