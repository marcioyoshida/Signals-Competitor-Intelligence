import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import four_corners

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
         "narrative": f"Narrative claim {i} about {entity} competitive behavior in the Brazilian market.",
         "axis": "comparative", "threat_score": 0.5 + i * 0.05, "as_of": RUN}
        for i in range(n)
    ]


def _draft_ok(label, industries, swot_bullets, evidence):
    out = []
    if evidence:
        out.append({"corner": "drivers", "text": f"{label} busca liderança em pagamentos.",
                    "confidence": 0.85, "evidence": [0]})
        out.append({"corner": "current_strategy", "text": "Expansão agressiva em open banking.",
                    "confidence": 0.7, "evidence": [1 % len(evidence)]})
        out.append({"corner": "response_profile", "text": "Provável expansão para crédito digital.",
                    "confidence": 0.6, "evidence": [0]})
    return out


def _draft_empty(label, industries, swot_bullets, evidence):
    return []


def test_analyze_corners_produces_proposals():
    beliefs = {"acme": _swot_beliefs("acme")}
    narr = _narratives("acme", 5)
    result = four_corners.analyze_corners(beliefs, narr, {},
                                           run_date=RUN, draft_fn=_draft_ok, min_evidence=3)
    assert len(result) >= 2
    for p in result:
        assert p["framework"] == "four_corners"
        assert p["kind"] == "four_corners"
        assert p["dimension"] in four_corners.DIMENSIONS
        assert p["status"] == "pending"


def test_response_profile_is_inference():
    beliefs = {"acme": _swot_beliefs("acme")}
    narr = _narratives("acme", 5)
    result = four_corners.analyze_corners(beliefs, narr, {},
                                           run_date=RUN, draft_fn=_draft_ok, min_evidence=3)
    resp = [p for p in result if p["dimension"] == "response_profile"]
    assert len(resp) >= 1
    assert resp[0]["is_inference"] is True
    drivers = [p for p in result if p["dimension"] == "drivers"]
    assert drivers[0].get("is_inference") is False


def test_entity_without_enough_signal_skipped():
    beliefs = {"thin": _belief("thin", [_bullet("S", "One.")])}
    narr = _narratives("thin", 1)
    result = four_corners.analyze_corners(beliefs, narr, {},
                                           run_date=RUN, draft_fn=_draft_ok, min_evidence=5)
    assert result == []


def test_already_proposed_skipped():
    beliefs = {"acme": _swot_beliefs("acme")}
    narr = _narratives("acme", 5)
    result = four_corners.analyze_corners(beliefs, narr, {},
                                           run_date=RUN, draft_fn=_draft_ok,
                                           already_proposed=frozenset({"acme"}), min_evidence=3)
    assert result == []


def test_empty_draft_no_proposals():
    beliefs = {"acme": _swot_beliefs("acme")}
    narr = _narratives("acme", 5)
    result = four_corners.analyze_corners(beliefs, narr, {},
                                           run_date=RUN, draft_fn=_draft_empty, min_evidence=3)
    assert result == []


def test_parse_draft_valid():
    raw = '{"corners":[{"corner":"drivers","text":"Busca liderança.","confidence":0.8,"evidence":[0]}]}'
    result = four_corners._parse_draft(raw, 3)
    assert len(result) == 1
    assert result[0]["corner"] == "drivers"


def test_parse_draft_none():
    assert four_corners._parse_draft(None, 5) == []


def test_parse_draft_garbage():
    assert four_corners._parse_draft("not json", 5) == []


def test_parse_draft_invalid_corner():
    raw = '{"corners":[{"corner":"invalid","text":"X.","confidence":0.8,"evidence":[0]}]}'
    assert four_corners._parse_draft(raw, 3) == []


def test_parse_draft_max_per_dim():
    corners = [{"corner": "drivers", "text": f"Assessment {i}.", "confidence": 0.8, "evidence": [0]}
               for i in range(5)]
    raw = json.dumps({"corners": corners})
    result = four_corners._parse_draft(raw, 3)
    assert len(result) == four_corners.MAX_PER_DIM


def test_stable_id():
    id1 = four_corners._bullet_id("acme", "drivers", "Some text.")
    id2 = four_corners._bullet_id("acme", "drivers", "Some text.")
    id3 = four_corners._bullet_id("acme", "drivers", "Different.")
    assert id1 == id2
    assert id1 != id3
    assert id1.startswith("four_corners:acme:drivers:")
