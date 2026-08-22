import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.synth.synthesize import _strip_dangling_opener


def test_strips_leading_alem_disso_and_recapitalizes():
    out = _strip_dangling_opener(
        "Além disso, o fundo Kinea Multifamily está listado na CVM [1]."
    )
    assert out == "O fundo Kinea Multifamily está listado na CVM [1]."


def test_strips_other_connectors_only_with_comma():
    assert _strip_dangling_opener("Também, houve um novo registro.") == "Houve um novo registro."
    assert _strip_dangling_opener("Ademais, a oferta foi aprovada.") == "A oferta foi aprovada."
    assert _strip_dangling_opener("Por fim, o banco reduziu agências.") == "O banco reduziu agências."


def test_leaves_subordinating_uses_intact():
    # No comma right after -> not a dangling discourse marker; keep as-is.
    assert _strip_dangling_opener("Ainda que o lucro fosse recorde, houve cortes.") \
        == "Ainda que o lucro fosse recorde, houve cortes."
    assert _strip_dangling_opener("Assim como Itaú, o Bradesco cortou agências.") \
        == "Assim como Itaú, o Bradesco cortou agências."


def test_leaves_normal_openers_intact():
    text = "O Bradesco anunciou o fechamento de 324 agências [1]."
    assert _strip_dangling_opener(text) == text


def test_handles_empty_and_leading_space():
    assert _strip_dangling_opener("") == ""
    assert _strip_dangling_opener("   Além disso, X ocorreu.") == "X ocorreu."
