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
                }
            ]
        },
        "ofertas": {
            "items": [
                {
                    "id": "of:1",
                    "issuer": "Demo payment company",
                    "security": "Debêntures",
                    "url": "https://dados.cvm.gov.br/o1",
                }
            ]
        },
        "pix_moves": {"items": []},
    }
    cands = extract_candidates(digest, max_candidates=5)
    assert cands
    assert cands[0]["kind"] in ("regulatory", "regulatory_fusion")
    assert cands[0]["sources"]


def test_max_candidates_cap():
    digest = {
        "regulatory": {
            "items": [
                {"id": f"bcb:{i}", "subject": f"topic {i}", "url": f"https://x/{i}"}
                for i in range(20)
            ]
        }
    }
    cands = extract_candidates(digest, max_candidates=3)
    assert len(cands) == 3
