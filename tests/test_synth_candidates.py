import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth.candidates import extract_candidates


def test_extract_regulatory_and_competitor_candidates():
    digest = {
        "regulatory": {
            "items": [
                {
                    "id": "bcb:1",
                    "doc_type": "Resolução",
                    "subject": "Pix payment institution rules",
                    "url": "https://www.bcb.gov.br/r1",
                    "is_new": True,
                }
            ],
            "context": [],
        },
        "ofertas": {
            "items": [],
            "context": [
                {
                    "id": "of:1",
                    "issuer": "Demo payment company",
                    "security": "Debêntures",
                    "url": "https://dados.cvm.gov.br/o1",
                }
            ],
        },
        "pix_moves": {"items": [], "context": []},
    }
    cands = extract_candidates(digest, max_candidates=5)
    assert cands
    assert cands[0]["sources"]


def test_entity_fusion_from_context_when_items_empty():
    """Real digests after seeding: items=[] but context has samples."""
    digest = {
        "regulatory": {"items": [], "context": [], "count": 49, "new_count": 0},
        "sec_filings": {
            "items": [],
            "context": [
                {
                    "id": "sec:nu:1",
                    "ticker": "NU",
                    "form": "6-K",
                    "company": "Nu Holdings Ltd.",
                    "url": "https://www.sec.gov/nu",
                    "source": "SEC-EDGAR",
                },
                {
                    "id": "sec:nu:pix",
                    "ticker": "NU",
                    "form": "6-K",
                    "company": "Nu Holdings Ltd.",
                    "url": "https://www.sec.gov/nu2",
                    "source": "SEC-EDGAR",
                },
            ],
            "count": 117,
            "new_count": 0,
        },
        "ofertas": {
            "items": [],
            "context": [
                {
                    "id": "of:btg:1",
                    "issuer": "Some Issuer SA",
                    "leader": "BTG PACTUAL INVESTMENT BANKING",
                    "security": "Debêntures",
                    "url": "https://dados.cvm.gov.br/of",
                    "source": "CVM-Ofertas",
                }
            ],
            "count": 91,
            "new_count": 0,
        },
        "market": {
            "items": [
                {"institution": "ITAU", "value": 1e12, "share_pct": 14.8},
                {"institution": "NU PAGAMENTOS", "value": 1e11, "share_pct": 2.0},
            ]
        },
        "inf_diario_moves": {
            "items": [],
            "context": [
                {
                    "id": "cvm-inf:1:2026-07-16",
                    "fund_name": "ITAÚ SOBERANO RF",
                    "admin": "ITAU UNIBANCO S.A.",
                    "pl": 5e10,
                    "url": "https://dados.cvm.gov.br/dataset/fi-doc-inf_diario",
                    "cnpj": "123",
                }
            ],
            "as_of": "2026-07-16",
        },
        "pix_moves": {
            "items": [],
            "context": [
                {
                    "id": "pix:nu",
                    "institution": "NU PAGAMENTOS - IP",
                    "ispb": "18236120",
                    "tx_value": 1e9,
                }
            ],
        },
        "juros_moves": {"items": [], "context": []},
        "new_entrants": {"items": [], "context": []},
        "competitor": {"items": [], "context": []},
    }
    cands = extract_candidates(digest, max_candidates=10, min_lenses=2, min_score=0.4)
    assert cands, "context-only multi-lens digest must yield candidates"
    # Quality bar: multi-lens entity fusions
    assert any(c.get("kind") == "entity_fusion" for c in cands)
    assert all(len(c.get("lenses") or []) >= 2 or c.get("is_alert") for c in cands)
    entities = {e for c in cands for e in (c.get("entities") or [])}
    assert entities & {"itau", "btg", "nubank"}
    assert any(c.get("as_of") == "2026-07-16" for c in cands)


def test_quality_gate_drops_single_lens_context_noise():
    digest = {
        "sec_filings": {
            "context": [
                {
                    "id": "sec:only",
                    "ticker": "XP",
                    "form": "6-K",
                    "company": "XP Inc",
                    "url": "https://sec/xp",
                }
            ]
        },
        "market": {"items": []},
        "ofertas": {"context": []},
        "regulatory": {"items": [], "context": []},
        "pix_moves": {"items": [], "context": []},
        "juros_moves": {"items": [], "context": []},
        "inf_diario_moves": {"items": [], "context": []},
        "competitor": {"items": [], "context": []},
        "new_entrants": {"items": [], "context": []},
    }
    cands = extract_candidates(digest, max_candidates=10, min_lenses=2, min_score=0.45)
    # Single-lens non-alert context should not pass the quality gate
    assert cands == []


def test_max_candidates_cap():
    digest = {
        "regulatory": {
            "items": [
                {
                    "id": f"bcb:{i}",
                    "subject": f"topic unique{i}zzzz",
                    "url": f"https://x/{i}",
                    "is_new": True,
                }
                for i in range(20)
            ]
        }
    }
    cands = extract_candidates(digest, max_candidates=3)
    assert len(cands) == 3


def test_multi_lens_entity_scores_higher_than_single():
    digest = {
        "sec_filings": {
            "context": [
                {
                    "id": "sec:stne",
                    "ticker": "STNE",
                    "form": "6-K",
                    "company": "StoneCo",
                    "url": "https://sec/stne",
                    "is_new": True,
                }
            ]
        },
        "market": {
            "items": [
                # won't match stone
                {"institution": "ITAU", "share_pct": 10, "value": 1}
            ]
        },
        "ofertas": {
            "context": [
                {
                    "id": "of:stone",
                    "issuer": "Stone Sociedade de Crédito",
                    "leader": "XP INVESTIMENTOS",
                    "security": "Debêntures",
                    "url": "https://cvm/stone",
                }
            ]
        },
    }
    cands = extract_candidates(digest, max_candidates=5)
    stone = next((c for c in cands if c.get("entity") == "stone" or "stone" in (c.get("entities") or [])), None)
    assert stone is not None
    assert len(stone.get("lenses") or []) >= 2
    assert stone["threat_score"] >= 0.4
