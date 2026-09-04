"""ADR 009 §2 — versioned-document section diff."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth import reg_diff as D

V1 = ("O CONSELHO MONETÁRIO NACIONAL resolve:\n"
      "Art. 1º O limite é de R$ 100,00.\n"
      "Art. 2º Esta Resolução entra em vigor em 1º de dezembro de 2026.\n"
      "Art. 3º Fica revogada a Resolução X.")
# v2: modify Art. 1 (new limit), add Art. 4, remove Art. 3
V2 = ("O CONSELHO MONETÁRIO NACIONAL resolve:\n"
      "Art. 1º O limite é de R$ 250,00.\n"
      "Art. 2º Esta Resolução entra em vigor em 1º de dezembro de 2026.\n"
      "Art. 4º Disposição nova.")


def test_segment_splits_preamble_and_articles():
    keys = [u["key"] for u in D.segment(V1)]
    assert keys == ["preamble", "art-1", "art-2", "art-3"]
    # anexo becomes its own unit
    keys2 = [u["key"] for u in D.segment("Art. 1º x\nANEXO I\nTabela.")]
    assert "anexo-i" in keys2


def test_no_headers_is_a_single_preamble():
    assert [u["key"] for u in D.segment("texto corrido sem artigos")] == ["preamble"]


def test_diff_detects_added_removed_modified():
    diff = D.diff_versions(V1, V2)
    assert diff["summary"] == {"added": 1, "removed": 1, "modified": 1, "units_new": 4}
    assert diff["modified"] == ["art-1"] and diff["added"] == ["art-4"] and diff["removed"] == ["art-3"]
    art1 = next(s for s in diff["sections"] if s["key"] == "art-1")
    assert art1["status"] == "modified" and any("250" in ln for ln in art1["diff"])


def test_identical_versions_have_no_structural_change():
    diff = D.diff_versions(V1, V1)
    assert diff["summary"]["added"] == 0 == diff["summary"]["removed"] == diff["summary"]["modified"]
    assert D.summarize_diff(diff) == "sem alteração estrutural"


def test_summarize_diff_is_readable():
    s = D.summarize_diff(D.diff_versions(V1, V2))
    assert "modifica Art. 1" in s and "inclui Art. 4" in s and "revoga Art. 3" in s
