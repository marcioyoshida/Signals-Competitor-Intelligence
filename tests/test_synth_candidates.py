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
                }
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
        },
        "juros_moves": {"items": [], "context": []},
        "pix_moves": {"items": [], "context": []},
        "new_entrants": {"items": [], "context": []},
        "competitor": {"items": [], "context": []},
    }
    cands = extract_candidates(digest, max_candidates=10)
    assert cands, "context-only digest must still yield candidates"
    kinds = {c["kind"] for c in cands}
    assert "entity_fusion" in kinds or any(
        c.get("entities") for c in cands
    ), cands
    # Multi-lens itau cluster (market + inf_diario) and/or btg/nubank clusters
    entities = {e for c in cands for e in (c.get("entities") or [])}
    assert entities & {"itau", "btg", "nubank"}


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
