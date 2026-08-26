import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import pestle, swot_reconcile

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
         "narrative": f"Narrative claim {i} about {entity} macro factors in the Brazilian market.",
         "axis": "comparative", "lenses": ["regulatory", "juros", "pix", "dou", "market"],
         "threat_score": 0.5 + i * 0.05, "as_of": RUN}
        for i in range(n)
    ]


def _draft_ok(label, industries, swot_bullets, evidence):
    out = []
    if evidence:
        out.append({"factor": "legal", "text": f"Novas regulações impactam {label}.",
                    "confidence": 0.85, "evidence": [0]})
        out.append({"factor": "economic", "text": "Juros altos comprimem margens.",
                    "confidence": 0.7, "evidence": [1 % len(evidence)]})
        out.append({"factor": "technological", "text": "Open banking acelera digitalização.",
                    "confidence": 0.6, "evidence": [0]})
    return out


def _draft_empty(label, industries, swot_bullets, evidence):
    return []


def _draft_low_conf(label, industries, swot_bullets, evidence):
    return [{"factor": "political", "text": "Low conf.", "confidence": 0.2, "evidence": [0]}]


def test_analyze_factors_produces_proposals():
    beliefs = {"acme": _swot_beliefs("acme")}
    narr = _narratives("acme", 5)
    result = pestle.analyze_factors(beliefs, narr, {},
                                     run_date=RUN, draft_fn=_draft_ok, min_evidence=3)
    assert len(result) >= 2
    for p in result:
        assert p["framework"] == "pestle"
        assert p["kind"] == "pestle"
        assert p["dimension"] in pestle.DIMENSIONS
        assert p["status"] == "pending"


def test_entity_without_enough_signal_skipped():
    beliefs = {"thin": _belief("thin", [_bullet("S", "One.")])}
    narr = _narratives("thin", 1)
    result = pestle.analyze_factors(beliefs, narr, {},
                                     run_date=RUN, draft_fn=_draft_ok, min_evidence=5)
    assert result == []


def test_already_proposed_skipped():
    beliefs = {"acme": _swot_beliefs("acme")}
    narr = _narratives("acme", 5)
    result = pestle.analyze_factors(beliefs, narr, {},
                                     run_date=RUN, draft_fn=_draft_ok,
                                     already_proposed=frozenset({"acme"}), min_evidence=3)
    assert result == []


def test_low_confidence_dropped():
    beliefs = {"acme": _swot_beliefs("acme")}
    narr = _narratives("acme", 5)
    result = pestle.analyze_factors(beliefs, narr, {},
                                     run_date=RUN, draft_fn=_draft_low_conf,
                                     min_evidence=3, min_conf=0.5)
    assert result == []


def test_empty_draft_no_proposals():
    beliefs = {"acme": _swot_beliefs("acme")}
    narr = _narratives("acme", 5)
    result = pestle.analyze_factors(beliefs, narr, {},
                                     run_date=RUN, draft_fn=_draft_empty, min_evidence=3)
    assert result == []


def test_parse_draft_valid():
    raw = '{"factors":[{"factor":"legal","text":"Nova lei.","confidence":0.8,"evidence":[0]}]}'
    result = pestle._parse_draft(raw, 3)
    assert len(result) == 1
    assert result[0]["factor"] == "legal"


def test_parse_draft_none():
    assert pestle._parse_draft(None, 5) == []


def test_parse_draft_garbage():
    assert pestle._parse_draft("not json", 5) == []


def test_parse_draft_invalid_factor():
    raw = '{"factors":[{"factor":"invalid","text":"X.","confidence":0.8,"evidence":[0]}]}'
    assert pestle._parse_draft(raw, 3) == []


def test_parse_draft_no_evidence():
    raw = '{"factors":[{"factor":"legal","text":"X.","confidence":0.8,"evidence":[]}]}'
    assert pestle._parse_draft(raw, 3) == []


def test_parse_draft_max_per_dim():
    factors = [{"factor": "legal", "text": f"Assessment {i}.", "confidence": 0.8, "evidence": [0]}
               for i in range(5)]
    raw = json.dumps({"factors": factors})
    result = pestle._parse_draft(raw, 3)
    assert len(result) == pestle.MAX_PER_DIM


def test_stable_id():
    id1 = pestle._bullet_id("acme", "legal", "Some text.")
    id2 = pestle._bullet_id("acme", "legal", "Some text.")
    id3 = pestle._bullet_id("acme", "legal", "Different.")
    assert id1 == id2
    assert id1 != id3
    assert id1.startswith("pestle:acme:legal:")


def test_eligible_entities_sorted():
    beliefs = {
        "few": _belief("few", [_bullet("S", "One.")]),
        "many": _swot_beliefs("many"),
    }
    narr_by_ent = {"few": _narratives("few", 4), "many": _narratives("many", 10)}
    result = pestle.eligible_entities(beliefs, narr_by_ent, min_evidence=3)
    assert result[0] == "many"
