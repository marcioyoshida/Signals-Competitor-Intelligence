"""#261: CCO risk-register reputation lines never print '#None'."""
from src.synth.executive import _rep_detail


def test_bcb_ranked_row_keeps_rank():
    assert _rep_detail({"rank": 3, "index": 41.2, "source": "BCB"}) == "#3 reclamações · índice 41.2"


def test_ans_row_without_rank_shows_source_and_index():
    d = _rep_detail({"rank": None, "index": 12.5, "source": "ANS"})
    assert "None" not in d and d == "ANS · índice 12.5"


def test_withheld_ans_row():
    d = _rep_detail({"rank": None, "index": None, "source": "ANS", "index_withheld": True})
    assert "None" not in d and d == "ANS · índice — (base pequena)"
